#!/usr/bin/env python3
"""Candy House Sesame Firmware Downloader.

Queries the Candy House cloud API to locate and download the latest firmware
updates for each device model into the 'firmware' directory.

Works with standard Python 3 library modules (zero external dependencies required).
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from urllib.parse import parse_qsl, quote, urlparse

# AWS Cognito and API Gateway constants from Candy House app
COGNITO_REGION = "ap-northeast-1"
API_GATEWAY_HOST = "app.candyhouse.co"
API_URL_TEMPLATE = "https://app.candyhouse.co/prod/device/v1/firmwareZipUrl"

DEFAULT_CREDENTIALS_FILENAMES = (
    "sesame_credentials.json",
    "sesame_firmware_credentials.json",
    "/config/sesame_credentials.json",
)

# Device model catalog: maps canonical model names to their integer productType IDs
MODEL_CATALOG = {
    # Locks
    "sesame2": 0,
    "sesame4": 4,
    "sesame5": 5,
    "sesame5_pro": 7,
    "sesame5_usa": 16,
    "sesame6": 20,
    "sesame6_pro": 21,
    "sesame6_pro_sliding_door": 32,
    # Keypads & Biometrics
    "sesame_touch": 10,
    "sesame_touch_pro": 9,
    "sesame_touch_2": 25,
    "sesame_touch_2_pro": 26,
    "sesame_face": 19,
    "sesame_face_pro": 18,
    "sesame_face_ai": 23,
    "sesame_face_pro_ai": 22,
    "sesame_face_2": 27,
    "sesame_face_2_pro": 28,
    "sesame_face_2_ai": 30,
    "sesame_face_2_pro_ai": 31,
    # Other Accessories
    "sesame_bot1": 2,
    "sesame_bot2": 17,
    "sesame_bike1": 3,
    "sesame_bike2": 6,
    "sesame_bike3": 33,
    "opensensor1": 8,
    "opensensor2": 24,
    "ble_connector": 11,
    "remote": 14,
    "remote_nano": 15,
    "sesame_miwa": 29,
}


def resolve_credentials(
    api_key: str | None = None,
    pool_id: str | None = None,
    credentials_file: str | None = None,
) -> tuple[str, str]:
    """Resolve API Key and Cognito Identity Pool ID from args, env, or credentials file."""
    resolved_api_key = api_key or os.environ.get("SESAME_API_KEY", "")
    resolved_pool_id = pool_id or os.environ.get("SESAME_COGNITO_POOL_ID", "")

    if not resolved_api_key or not resolved_pool_id:
        files_to_check = []
        if credentials_file:
            files_to_check.append(credentials_file)
        files_to_check.extend(DEFAULT_CREDENTIALS_FILENAMES)

        for fpath in files_to_check:
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if not resolved_api_key:
                            resolved_api_key = data.get("api_key") or data.get("API_KEY") or ""
                        if not resolved_pool_id:
                            resolved_pool_id = (
                                data.get("cognito_identity_pool_id")
                                or data.get("cognito_pool_id")
                                or data.get("COGNITO_IDENTITY_POOL_ID")
                                or ""
                            )
                except Exception:
                    pass

    if not resolved_api_key or not resolved_pool_id:
        raise ValueError("Candy House cloud credentials (API Key and Cognito Pool ID) not configured.")

    return resolved_api_key.strip(), resolved_pool_id.strip()


def get_cognito_credentials(region: str = COGNITO_REGION, pool_id: str | None = None) -> dict[str, str]:
    """Retrieves temporary AWS credentials from the unauthenticated Cognito identity pool."""
    if not pool_id:
        try:
            _, pool_id = resolve_credentials()
        except ValueError:
            pool_id = "ap-northeast-1:unconfigured"

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
        creds = json.loads(resp.read().decode("utf-8"))["Credentials"]

    return {
        "access_key": creds["AccessKeyId"],
        "secret_key": creds["SecretKey"],
        "session_token": creds["SessionToken"],
    }


def sign_aws_v4(
    method: str,
    url_str: str,
    headers: dict[str, str],
    creds: dict[str, str],
    region: str = COGNITO_REGION,
    service: str = "execute-api",
    payload: bytes = b"",
) -> dict[str, str]:
    """Signs an HTTP request using AWS Signature Version 4 (SigV4)."""
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


def query_firmware_info(
    product_type: int,
    creds: dict[str, str],
    firmware_dir: str = "prod",
    api_key: str | None = None,
) -> dict | None:
    """Queries the Candy House cloud API for the latest firmware URL of a productType."""
    if not api_key:
        try:
            api_key, _ = resolve_credentials()
        except ValueError:
            api_key = ""

    url = f"{API_URL_TEMPLATE}?productType={product_type}"
    if firmware_dir != "prod":
        url += f"&firmwareDir={quote(firmware_dir)}"

    headers = {"x-api-key": api_key}
    signed_headers = sign_aws_v4("GET", url, headers, creds)

    py_req = urllib.request.Request(url, headers=signed_headers)
    try:
        with urllib.request.urlopen(py_req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data
            return None
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def download_file(url: str, dest_path: str) -> None:
    """Downloads a file from a URL to the specified destination path."""
    req = urllib.request.Request(url, headers={"User-Agent": "Sesame-Firmware-Downloader/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
        while chunk := resp.read(65536):
            f.write(chunk)


def process_model(
    model_name: str,
    product_type: int,
    creds: dict[str, str],
    output_dir: str,
    firmware_dir: str = "prod",
    extract: bool = True,
    force: bool = False,
    dry_run: bool = False,
    api_key: str | None = None,
) -> dict | None:
    """Checks and downloads firmware for a specific model."""
    print(f"[{model_name}] Querying productType {product_type}...")
    try:
        info = query_firmware_info(product_type, creds, firmware_dir=firmware_dir, api_key=api_key)
    except Exception as err:
        print(f"  Failed to query metadata: {err}", file=sys.stderr)
        return None

    if not info:
        print("  No firmware release found.")
        return None

    zip_url = info.get("zipUrl")
    firmware_name = info.get("firmwareName", f"model_{product_type}")
    file_name = info.get("fileName", f"{firmware_name}.zip")

    print(f"  Latest firmware: {firmware_name} ({file_name})")
    print(f"  Download URL:    {zip_url}")

    if dry_run:
        return info

    model_dir = os.path.join(output_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)

    zip_dest = os.path.join(model_dir, file_name)

    # Save API metadata
    metadata_dest = os.path.join(model_dir, "metadata.json")
    with open(metadata_dest, "w", encoding="utf-8") as f:
        json.dump(
            {
                "model": model_name,
                "product_type": product_type,
                "firmware_name": firmware_name,
                "file_name": file_name,
                "zip_url": zip_url,
                "downloaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )

    if os.path.exists(zip_dest) and not force:
        print(f"  Archive {file_name} already exists. Use --force to re-download.")
    else:
        print(f"  Downloading {file_name}...")
        try:
            download_file(zip_url, zip_dest)
            print(f"  Saved to {zip_dest}")
        except Exception as err:
            print(f"  Download failed: {err}", file=sys.stderr)
            return None

    if extract and os.path.exists(zip_dest):
        print("  Unpacking DFU package...")
        try:
            with zipfile.ZipFile(zip_dest, "r") as zf:
                zf.extractall(model_dir)
            print(f"  Extracted package contents into {model_dir}")
        except Exception as err:
            print(f"  Failed to extract zip archive: {err}", file=sys.stderr)
            return None

    return info


def main() -> int:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Download latest firmware update files for Sesame BLE devices from Candy House cloud."
    )
    parser.add_argument(
        "--model",
        "-m",
        default="all",
        help="Device model name (e.g. sesame5, sesame_touch_pro, sesame_face_pro_ai) or 'all' (default: all)",
    )
    parser.add_argument(
        "--env",
        "-e",
        default="prod",
        help="Firmware channel directory ('prod' or 'test', default: prod)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="firmware",
        help="Target output directory to store downloaded firmware files (default: firmware)",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Do not automatically unzip downloaded archives",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force re-download even if archive already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only check for updates and print firmware info without downloading",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List all known device models and exit",
    )
    parser.add_argument(
        "--api-key",
        help="Candy House cloud API key (or set SESAME_API_KEY)",
    )
    parser.add_argument(
        "--pool-id",
        help="AWS Cognito Identity Pool ID (or set SESAME_COGNITO_POOL_ID)",
    )
    parser.add_argument(
        "--credentials-file",
        help="Path to JSON file containing credentials (api_key and cognito_identity_pool_id)",
    )

    args = parser.parse_args()

    if args.list_models:
        print("Available models:")
        for name, ptype in sorted(MODEL_CATALOG.items()):
            print(f"  {name:25s} (productType: {ptype})")
        return 0

    target_models = {}
    if args.model.lower() == "all":
        target_models = MODEL_CATALOG
    else:
        req_name = args.model.lower().strip()
        if req_name in MODEL_CATALOG:
            target_models = {req_name: MODEL_CATALOG[req_name]}
        else:
            try:
                val = int(req_name)
                found = False
                for k, v in MODEL_CATALOG.items():
                    if v == val:
                        target_models[k] = v
                        found = True
                        break
                if not found:
                    target_models[f"model_{val}"] = val
            except ValueError:
                print(f"Error: Unknown model '{args.model}'. Use --list-models to see available models.", file=sys.stderr)
                return 1

    try:
        api_key, pool_id = resolve_credentials(
            api_key=args.api_key,
            pool_id=args.pool_id,
            credentials_file=args.credentials_file,
        )
    except ValueError:
        print("Error: Candy House cloud credentials not configured.", file=sys.stderr)
        print("Please provide credentials via:", file=sys.stderr)
        print("  1. CLI arguments: --api-key <KEY> --pool-id <POOL_ID>", file=sys.stderr)
        print("  2. Environment variables: SESAME_API_KEY and SESAME_COGNITO_POOL_ID", file=sys.stderr)
        print("  3. Credentials file: sesame_credentials.json (or --credentials-file)", file=sys.stderr)
        print("You can extract them directly from the Sesame Android APK using:", file=sys.stderr)
        print("  python3 tools/extract_apk_credentials.py <path_to_apk> --out sesame_credentials.json", file=sys.stderr)
        return 1

    print("Authenticating with AWS Cognito...")
    try:
        creds = get_cognito_credentials(pool_id=pool_id)
    except Exception as err:
        print(f"Failed to obtain Cognito credentials: {err}", file=sys.stderr)
        return 1

    print(f"Checking firmware for {len(target_models)} model(s) (channel: {args.env})...\n" + "-" * 50)

    success_count = 0
    for model_name, product_type in sorted(target_models.items(), key=lambda x: x[1]):
        result = process_model(
            model_name=model_name,
            product_type=product_type,
            creds=creds,
            output_dir=args.output_dir,
            firmware_dir=args.env,
            extract=not args.no_extract,
            force=args.force,
            dry_run=args.dry_run,
            api_key=api_key,
        )
        if result:
            success_count += 1

    print("-" * 50)
    print(f"Done! {success_count}/{len(target_models)} models processed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
