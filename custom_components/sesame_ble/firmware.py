"""Firmware client for querying Candy House cloud endpoints for latest Sesame firmware releases."""

import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

try:
    from .sesame_client import ProductModels
except (ImportError, ValueError):
    from custom_components.sesame_ble.sesame_client import ProductModels

logger = logging.getLogger(__name__)

# AWS Cognito and API Gateway constants for Candy House app
COGNITO_REGION = "ap-northeast-1"
API_GATEWAY_HOST = "app.candyhouse.co"
API_URL_TEMPLATE = "https://app.candyhouse.co/prod/device/v1/firmwareZipUrl"

# Cache settings
CREDENTIALS_CACHE_TTL = 3000  # 50 minutes (Cognito temporary credentials expire in 1 hour)
FIRMWARE_CACHE_TTL = 3600     # 1 hour

DEFAULT_CREDENTIALS_FILENAMES = (
    "sesame_credentials.json",
    "sesame_firmware_credentials.json",
    ".sesame_credentials.json",
)

DEFAULT_LOCAL_FIRMWARE_DIRS = (
    "/config/sesame_firmware",
    "/share/sesame_firmware",
    "sesame_firmware",
    "firmware",
)

_cached_credentials: dict[str, Any] | None = None
_firmware_version_cache: dict[int, dict[str, Any]] = {}

# Additional older accessories/models mapping not in ProductModels IntEnum
MODEL_PRODUCT_TYPE_MAP: dict[str, int] = {
    "SESAME2": 0,
    "SESAME_BOT1": 2,
    "SESAME_BIKE1": 3,
    "SESAME4": 4,
    "OPENSENSOR1": 8,
    "BLE_CONNECTOR": 11,
    "REMOTE": 14,
    "REMOTE_NANO": 15,
    "SESAME_BOT2": 17,
    "OPENSENSOR2": 24,
    "SESAME_MIWA": 29,
}


class FirmwareFetchError(Exception):
    """Raised when fetching firmware metadata from Candy House fails."""


class FirmwareCredentialsMissingError(FirmwareFetchError):
    """Raised when Candy House cloud credentials (API Key or Cognito Pool ID) are not configured."""


def get_product_type_id(model: str | int | ProductModels | None) -> int | None:
    """Resolve model identifier to its integer productType ID."""
    if model is None:
        return None
    if isinstance(model, ProductModels):
        return model.value
    if isinstance(model, int):
        return model

    model_str = str(model).strip()
    if model_str.isdigit():
        return int(model_str)

    norm = model_str.upper().replace("-", "_").replace(" ", "_")
    if norm in ProductModels.__members__:
        return ProductModels[norm].value
    if norm in MODEL_PRODUCT_TYPE_MAP:
        return MODEL_PRODUCT_TYPE_MAP[norm]

    # Try removing leading/trailing common prefixes
    for k, v in ProductModels.__members__.items():
        if k in norm or norm in k:
            return v.value

    return None


def load_credentials_from_file(config_dir: str | None = None) -> dict[str, str]:
    """Search standard locations for a JSON credentials file."""
    search_paths = []
    if config_dir:
        for fname in DEFAULT_CREDENTIALS_FILENAMES:
            search_paths.append(os.path.join(config_dir, fname))

    # Standard HA, local working dir, and home config locations
    for base in ("/config", ".", os.path.expanduser("~/.config/sesame")):
        for fname in DEFAULT_CREDENTIALS_FILENAMES:
            search_paths.append(os.path.join(base, fname))

    env_file = os.environ.get("SESAME_CREDENTIALS_FILE")
    if env_file:
        search_paths.insert(0, env_file)

    for p in search_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as err:
                logger.warning("Failed to read credentials file at %s: %s", p, err)
    return {}


def resolve_credentials(
    api_key: str | None = None,
    pool_id: str | None = None,
    config_dir: str | None = None,
) -> tuple[str, str]:
    """Resolve API Key and Cognito Identity Pool ID from args, env, or credentials file."""
    resolved_api_key = api_key or ""
    resolved_pool_id = pool_id or ""

    # Check environment variables
    if not resolved_api_key:
        resolved_api_key = os.environ.get("SESAME_API_KEY", "")
    if not resolved_pool_id:
        resolved_pool_id = os.environ.get("SESAME_COGNITO_POOL_ID", "")

    # Check credentials file
    if not resolved_api_key or not resolved_pool_id:
        file_creds = load_credentials_from_file(config_dir)
        if not resolved_api_key:
            resolved_api_key = (
                file_creds.get("api_key")
                or file_creds.get("API_KEY")
                or ""
            )
        if not resolved_pool_id:
            resolved_pool_id = (
                file_creds.get("cognito_identity_pool_id")
                or file_creds.get("cognito_pool_id")
                or file_creds.get("COGNITO_IDENTITY_POOL_ID")
                or ""
            )

    if not resolved_api_key or not resolved_pool_id:
        missing = []
        if not resolved_api_key:
            missing.append("API Key (SESAME_API_KEY)")
        if not resolved_pool_id:
            missing.append("Cognito Identity Pool ID (SESAME_COGNITO_POOL_ID)")
        raise FirmwareCredentialsMissingError(
            f"Candy House cloud credentials not configured: missing {', '.join(missing)}. "
            "Please configure credentials via Sesame BLE integration options in Home Assistant, "
            "environment variables, /config/sesame_credentials.json, or extract them from the "
            "Candy House Sesame Android APK using tools/extract_apk_credentials.py. "
            "Alternatively, place offline firmware .zip files in /config/sesame_firmware/."
        )

    return resolved_api_key.strip(), resolved_pool_id.strip()


def find_local_firmware_zip(
    product_type: int,
    model_name: str | None = None,
    config_dir: str | None = None,
    search_dirs: list[str] | None = None,
) -> tuple[str, bytes] | None:
    """Check local firmware directories for an offline DFU firmware package.

    Returns (version_string, zip_bytes) if a matching archive is found, or None.
    """
    dirs_to_check = []
    if search_dirs:
        dirs_to_check.extend(search_dirs)
    if config_dir:
        dirs_to_check.append(os.path.join(config_dir, "sesame_firmware"))
    dirs_to_check.extend(DEFAULT_LOCAL_FIRMWARE_DIRS)

    # Candidate filename stems to match
    match_stems = [f"prod_{product_type}", f"_{product_type}_", f"/{product_type}.zip"]
    if model_name:
        clean_model = model_name.lower().replace("-", "").replace("_", "").replace(" ", "")
        match_stems.append(clean_model)

    for d in dirs_to_check:
        if not os.path.isdir(d):
            continue
        try:
            for entry in os.listdir(d):
                if not entry.endswith(".zip"):
                    continue
                full_path = os.path.join(d, entry)
                lower_entry = entry.lower().replace("-", "").replace("_", "").replace(" ", "")

                # Check if entry matches product type or model name
                is_match = False
                if f"_{product_type}_" in entry or entry == f"{product_type}.zip":
                    is_match = True
                elif model_name and clean_model in lower_entry:
                    is_match = True

                if is_match and os.path.isfile(full_path):
                    with open(full_path, "rb") as f:
                        data = f.read()

                    # Extract version tag from binary inside zip
                    version = None
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            for name in zf.namelist():
                                if name.endswith(".bin"):
                                    bdata = zf.read(name)
                                    matches = re.findall(rb"\d+\.\d+-\d+-[0-9a-fA-F]+", bdata)
                                    if matches:
                                        version = matches[0].decode("ascii")
                                        break
                    except Exception as err:
                        logger.debug("Failed reading zip binary in %s: %s", full_path, err)

                    if not version:
                        version = parse_version_from_firmware_name(entry.replace(".zip", ""), product_type)

                    if not version:
                        version = f"local-{product_type}"

                    logger.info("Found matching local firmware archive for productType %d: %s (%s)", product_type, full_path, version)
                    return version, data
        except Exception as err:
            logger.debug("Error scanning local firmware directory %s: %s", d, err)

    return None


def get_cognito_credentials(
    region: str = COGNITO_REGION,
    pool_id: str | None = None,
    force_refresh: bool = False,
    config_dir: str | None = None,
) -> dict[str, str]:
    """Retrieves temporary AWS credentials from the unauthenticated Cognito identity pool."""
    global _cached_credentials
    now = time.time()
    if not force_refresh and _cached_credentials and now < _cached_credentials.get("expires_at", 0):
        return _cached_credentials["creds"]

    if not pool_id:
        _, pool_id = resolve_credentials(pool_id=pool_id, config_dir=config_dir)

    try:
        # 1. GetId
        req_id = urllib.request.Request(
            f"https://cognito-identity.{region}.amazonaws.com/",
            data=json.dumps({"IdentityPoolId": pool_id}).encode("utf-8"),
            headers={
                "X-Amz-Target": "AWSCognitoIdentityService.GetId",
                "Content-Type": "application/x-amz-json-1.1",
            },
        )
        with urllib.request.urlopen(req_id, timeout=15) as resp:
            identity_id = json.loads(resp.read().decode("utf-8"))["IdentityId"]

        # 2. GetCredentialsForIdentity
        req_creds = urllib.request.Request(
            f"https://cognito-identity.{region}.amazonaws.com/",
            data=json.dumps({"IdentityId": identity_id}).encode("utf-8"),
            headers={
                "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                "Content-Type": "application/x-amz-json-1.1",
            },
        )
        with urllib.request.urlopen(req_creds, timeout=15) as resp:
            creds_data = json.loads(resp.read().decode("utf-8"))["Credentials"]

        creds = {
            "access_key": creds_data["AccessKeyId"],
            "secret_key": creds_data["SecretKey"],
            "session_token": creds_data["SessionToken"],
        }
        _cached_credentials = {
            "creds": creds,
            "expires_at": now + CREDENTIALS_CACHE_TTL,
        }
        return creds
    except Exception as err:
        logger.error("Failed to acquire AWS Cognito credentials from Candy House pool: %s", err)
        raise FirmwareFetchError(f"AWS Cognito credential acquisition failed: {err}") from err


def sign_aws_v4(
    method: str,
    url_str: str,
    headers: dict[str, str],
    creds: dict[str, str],
    payload: bytes = b"",
    service: str = "execute-api",
    region: str = COGNITO_REGION,
) -> dict[str, str]:
    """Signs an HTTP request with AWS Signature Version 4 for AWS API Gateway."""
    t = datetime.datetime.now(datetime.timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    parsed = urlparse(url_str)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"

    query_params = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    canonical_querystr = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in query_params)

    req_headers = dict(headers)
    req_headers["host"] = host
    req_headers["x-amz-date"] = amz_date
    req_headers["x-amz-security-token"] = creds["session_token"]

    signed_headers_list = sorted([k.lower() for k in req_headers.keys()])
    signed_headers_str = ";".join(signed_headers_list)

    canonical_headers = "".join(f"{k}:{req_headers[k].strip()}\n" for k in signed_headers_list)
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystr}\n{canonical_headers}\n{signed_headers_str}\n{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + creds["secret_key"]).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={signature}"
    )
    req_headers["Authorization"] = auth_header
    return req_headers


def query_cloud_firmware_info(
    product_type: int,
    creds: dict[str, str],
    api_key: str | None = None,
    firmware_dir: str = "prod",
    config_dir: str | None = None,
) -> dict[str, Any]:
    """Queries the Candy House cloud API for the latest firmware URL of a productType."""
    if not api_key:
        api_key, _ = resolve_credentials(api_key=api_key, config_dir=config_dir)

    url = f"{API_URL_TEMPLATE}?productType={product_type}"
    if firmware_dir != "prod":
        url += f"&firmwareDir={quote(firmware_dir)}"

    headers = {"x-api-key": api_key}
    signed_headers = sign_aws_v4("GET", url, headers, creds)

    req = urllib.request.Request(url, headers=signed_headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data
            raise FirmwareFetchError(f"Candy House API returned ok=false for productType={product_type}: {data}")
    except urllib.error.HTTPError as err:
        raise FirmwareFetchError(f"HTTP error querying Candy House API for productType={product_type}: {err}") from err
    except Exception as err:
        raise FirmwareFetchError(f"Network error querying Candy House API for productType={product_type}: {err}") from err


def parse_version_from_firmware_name(firmware_name: str, product_type: int) -> str | None:
    """Fallback parser to construct version string from firmwareName metadata."""
    if not firmware_name:
        return None
    # Matches patterns like _30_22_e877d5 or _221_0_8c080c
    m = re.search(r"_(\d+)_(\d+)_([0-9a-fA-F]+)$", firmware_name)
    if m:
        ver_code, p, h = m.groups()
        if len(ver_code) == 2:
            return f"{ver_code[0]}.{ver_code[1]}-{p}-{h}"
        if len(ver_code) == 3 and ver_code.endswith("1"):
            return f"2.1-{p}-{h}"
        return f"{ver_code}-{p}-{h}"
    return None


def extract_version_from_zip(zip_url: str) -> str | None:
    """Downloads DFU archive into memory and reads the authentic version string from the binary."""
    try:
        req = urllib.request.Request(zip_url, headers={"User-Agent": "Sesame-Firmware-Fetcher/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".bin"):
                    bdata = zf.read(name)
                    matches = re.findall(rb"\d+\.\d+-\d+-[0-9a-fA-F]+", bdata)
                    if matches:
                        return matches[0].decode("ascii")
    except Exception as err:
        logger.debug("Could not inspect zip binary at %s: %s", zip_url, err)
    return None


def fetch_latest_firmware_version(
    product_type: int,
    force_refresh: bool = False,
    api_key: str | None = None,
    pool_id: str | None = None,
    config_dir: str | None = None,
    model_name: str | None = None,
) -> str:
    """Fetch the latest firmware version for a given productType from local cache, disk, or cloud."""
    # 1. Check local offline firmware archives first
    local_firmware = find_local_firmware_zip(product_type, model_name=model_name, config_dir=config_dir)
    if local_firmware:
        version, _ = local_firmware
        return version

    global _firmware_version_cache
    now = time.time()
    if not force_refresh and product_type in _firmware_version_cache:
        cached = _firmware_version_cache[product_type]
        if now < cached.get("expires_at", 0):
            return cached["version"]

    # 2. Resolve credentials
    ak, pi = resolve_credentials(api_key=api_key, pool_id=pool_id, config_dir=config_dir)
    creds = get_cognito_credentials(pool_id=pi, force_refresh=force_refresh, config_dir=config_dir)
    info = query_cloud_firmware_info(product_type, creds, api_key=ak, config_dir=config_dir)

    zip_url = info.get("zipUrl")
    firmware_name = info.get("firmwareName", "")

    # Primary extraction: read authentic version tag directly from compiled firmware.bin
    version = None
    if zip_url:
        version = extract_version_from_zip(zip_url)

    # Secondary fallback: parse firmware_name metadata
    if not version and firmware_name:
        version = parse_version_from_firmware_name(firmware_name, product_type)

    if not version:
        raise FirmwareFetchError(
            f"Failed to determine authentic firmware version for productType={product_type} "
            f"(firmwareName={firmware_name})"
        )

    _firmware_version_cache[product_type] = {
        "version": version,
        "info": info,
        "expires_at": now + FIRMWARE_CACHE_TTL,
    }
    logger.info("Fetched latest firmware for productType %d from Candy House: %s", product_type, version)
    return version


def download_firmware_zip(
    product_type: int,
    force_refresh: bool = False,
    api_key: str | None = None,
    pool_id: str | None = None,
    config_dir: str | None = None,
    model_name: str | None = None,
) -> tuple[str, bytes]:
    """Downloads the official firmware zip from local files or Candy House cloud.

    Returns (version_string, zip_bytes).
    """
    # 1. Check local offline firmware archives first
    local_firmware = find_local_firmware_zip(product_type, model_name=model_name, config_dir=config_dir)
    if local_firmware:
        version, zip_bytes = local_firmware
        return version, zip_bytes

    # 2. Resolve cloud credentials
    ak, pi = resolve_credentials(api_key=api_key, pool_id=pool_id, config_dir=config_dir)
    creds = get_cognito_credentials(pool_id=pi, force_refresh=force_refresh, config_dir=config_dir)
    info = query_cloud_firmware_info(product_type, creds, api_key=ak, config_dir=config_dir)
    zip_url = info.get("zipUrl")
    if not zip_url:
        raise FirmwareFetchError(f"No zipUrl found in Candy House API response for productType={product_type}")

    req = urllib.request.Request(zip_url, headers={"User-Agent": "Sesame-Firmware-Fetcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as err:
        raise FirmwareFetchError(f"Failed to download firmware zip from {zip_url}: {err}") from err

    # Extract version tag from downloaded data
    version = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".bin"):
                    bdata = zf.read(name)
                    matches = re.findall(rb"\d+\.\d+-\d+-[0-9a-fA-F]+", bdata)
                    if matches:
                        version = matches[0].decode("ascii")
                        break
    except Exception:
        pass

    if not version and info.get("firmwareName"):
        version = parse_version_from_firmware_name(info["firmwareName"], product_type)

    if not version:
        version = f"prod-{product_type}"

    return version, data
