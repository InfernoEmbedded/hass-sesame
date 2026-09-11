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
COGNITO_IDENTITY_POOL_ID = "ap-northeast-1:e5a1e548-564f-4681-bd5c-ec746ec9c684"
API_GATEWAY_HOST = "app.candyhouse.co"
API_KEY = "FK57aBCuLc7eTLGQLNFDV58p045azBk19Qeo3DnO"
API_URL_TEMPLATE = "https://app.candyhouse.co/prod/device/v1/firmwareZipUrl"

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


def get_cognito_credentials(region: str = COGNITO_REGION, pool_id: str = COGNITO_IDENTITY_POOL_ID) -> dict[str, str]:
    """Retrieves temporary AWS credentials from the unauthenticated Cognito identity pool."""
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
) -> dict | None:
    """Queries the Candy House cloud API for the latest firmware URL of a productType."""
    url = f"{API_URL_TEMPLATE}?productType={product_type}"
    if firmware_dir != "prod":
        url += f"&firmwareDir={quote(firmware_dir)}"

    headers = {"x-api-key": API_KEY}
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
) -> dict | None:
    """Checks and downloads firmware for a given model."""
    try:
        info = query_firmware_info(product_type, creds, firmware_dir=firmware_dir)
    except Exception as err:
        print(f"[{model_name}] Error querying API: {err}", file=sys.stderr)
        return None

    if not info:
        print(f"[{model_name}] No firmware available (productType={product_type})")
        return None

    zip_url = info.get("zipUrl")
    file_name = info.get("fileName") or f"{model_name}.zip"
    firmware_name = info.get("firmwareName", "")

    print(f"[{model_name}] Latest firmware: {firmware_name} ({file_name})")

    if dry_run:
        return info

    model_dir = os.path.join(output_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)

    zip_dest = os.path.join(model_dir, file_name)

    # Check if file already downloaded
    if os.path.exists(zip_dest) and not force:
        print(f"[{model_name}] Archive already exists at {zip_dest} (use --force to re-download)")
    else:
        print(f"[{model_name}] Downloading from {zip_url} ...")
        download_file(zip_url, zip_dest)
        print(f"[{model_name}] Downloaded to {zip_dest}")

    # Extract archive if requested
    if extract:
        try:
            with zipfile.ZipFile(zip_dest, "r") as zf:
                zf.extractall(model_dir)
            print(f"[{model_name}] Extracted contents to {model_dir}/")
        except Exception as e:
            print(f"[{model_name}] Failed to extract zip: {e}", file=sys.stderr)

    # Save metadata
    meta_path = os.path.join(model_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
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

    return info


def main():
    parser = argparse.ArgumentParser(
        description="Download latest Candy House Sesame firmware updates into the firmware directory.",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="all",
        help="Target device model (e.g. sesame6_pro, sesame_touch_2_pro, or 'all'). Default: all",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="firmware",
        help="Output directory path (default: 'firmware')",
    )
    parser.add_argument(
        "--env",
        default="prod",
        choices=["prod", "dev"],
        help="Firmware channel ('prod' or 'dev'). Default: prod",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Do not extract the firmware zip archive",
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
        # Direct match or integer lookup
        if req_name in MODEL_CATALOG:
            target_models = {req_name: MODEL_CATALOG[req_name]}
        else:
            # Check if integer ID was provided
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

    print("Authenticating with AWS Cognito...")
    try:
        creds = get_cognito_credentials()
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
        )
        if result:
            success_count += 1

    print("-" * 50)
    print(f"Done! {success_count}/{len(target_models)} models processed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
