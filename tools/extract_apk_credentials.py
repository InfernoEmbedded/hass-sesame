#!/usr/bin/env python3
"""Candy House Sesame APK Credential Extractor.

Extracts the AWS Cognito Identity Pool ID and Candy House cloud API Key
from an official Sesame Android APK or DEX file for interoperability with
the Home Assistant sesame_ble integration and download_firmware.py.

Usage:
    python3 tools/extract_apk_credentials.py path/to/Sesame.apk
    python3 tools/extract_apk_credentials.py path/to/Sesame.apk --out /config/sesame_credentials.json
    python3 tools/extract_apk_credentials.py --adb --out /config/sesame_credentials.json

Notice:
    This tool is intended strictly to enable interoperability with independently
    created software and home automation systems pursuant to statutory reverse
    engineering exceptions (including 17 U.S.C. § 1201(f), Directive 2009/24/EC
    Art. 6, Australian Copyright Act s. 47D, and Japan Copyright Act Art. 30-4 & 47-3).
    No proprietary application binaries, firmware files, or keys are bundled or
    distributed with this script.
"""

import argparse
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from typing import Any

# Regular expression to match standard AWS Cognito Identity Pool IDs
COGNITO_POOL_RE = re.compile(
    rb"(?:ap|us|eu|sa|ca|me|af)-(?:northeast|southeast|central|east|west|south)-\d:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# 40-character alphanumeric candidate pattern for API keys
API_KEY_RE = re.compile(rb"\b[0-9a-zA-Z]{40}\b")

KNOWN_SESAME_PACKAGES = [
    "co.candyhouse.sesame2",
    "co.candyhouse.app",
    "co.candyhouse.sesame",
]


class DexParser:
    """Lightweight DEX (Dalvik Executable) binary parser."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        if len(data) < 0x70 or not data.startswith(b"dex\n"):
            raise ValueError("Not a valid DEX file header")
        self.string_ids_size, self.string_ids_off = struct.unpack_from("<II", data, 56)

    def get_string(self, idx: int) -> str | None:
        """Retrieve a string from the DEX string table by its index."""
        if idx < 0 or idx >= self.string_ids_size:
            return None
        off = struct.unpack_from("<I", self.data, self.string_ids_off + idx * 4)[0]
        # Decode ULEB128 string length
        p = off
        length = 0
        shift = 0
        while True:
            if p >= len(self.data):
                return None
            b = self.data[p]
            p += 1
            length |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return self.data[p : p + length].decode("utf-8", errors="replace")

    def find_string_index(self, target: str) -> int | None:
        """Find the string table index for a specific string."""
        for i in range(self.string_ids_size):
            if self.get_string(i) == target:
                return i
        return None

    def extract_api_key_via_bytecode(self) -> str | None:
        """Locate the Candy House API key passed to CHAPIClientBiz.initialize in bytecode.

        In the DEX bytecode, the app calls:
            const-string vX, "ap-northeast-1"
            const-string vY, "<40-char API key>"
            invoke-static {..., vX, vY}, CHAPIClientBiz.initialize(...)
        """
        region_idx = self.find_string_index("ap-northeast-1")
        if region_idx is None:
            return None

        # Look for const-string (0x1A) instruction loading region_idx
        pattern = struct.pack("<H", region_idx)
        pos = 0
        while True:
            pos = self.data.find(pattern, pos)
            if pos == -1:
                break
            # const-string opcode is 0x1A: [0x1A, reg, string_id_low, string_id_high]
            if pos >= 2 and self.data[pos - 2] == 0x1A:
                # Check immediately following instruction (pos + 2)
                if pos + 6 <= len(self.data) and self.data[pos + 2] == 0x1A:
                    next_str_id = struct.unpack_from("<H", self.data, pos + 4)[0]
                    cand = self.get_string(next_str_id)
                    if cand and len(cand) == 40 and not re.match(r"^[0-9a-fA-F]{40}$", cand):
                        return cand
            pos += 1

        return None

    def extract_api_key_heuristic(self) -> str | None:
        """Heuristically find high-entropy 40-character API key in DEX string table."""
        # Find all 40-character alphanumeric strings
        candidates = set(API_KEY_RE.findall(self.data))
        for c in candidates:
            cand = c.decode("ascii", errors="ignore")
            # Filter out SHA-1 hashes (pure hex)
            if re.match(r"^[0-9a-fA-F]{40}$", cand):
                continue
            # Filter out standard camelCase/PascalCase identifiers (e.g. Unmarshaller, Listener, Exception)
            if any(cand.endswith(suffix) for suffix in ("Marshaller", "Unmarshaller", "Exception", "Listener", "Handler", "Adapter", "Properties")):
                continue
            # Must contain both uppercase, lowercase, and digits
            has_upper = any(ch.isupper() for ch in cand)
            has_lower = any(ch.islower() for ch in cand)
            has_digit = any(ch.isdigit() for ch in cand)
            if has_upper and has_lower and has_digit:
                return cand
        return None


def extract_from_dex(dex_bytes: bytes) -> dict[str, str]:
    """Extract credentials from raw DEX binary bytes."""
    results: dict[str, str] = {}

    # 1. Search for Cognito Identity Pool ID
    pool_matches = COGNITO_POOL_RE.findall(dex_bytes)
    if pool_matches:
        pools = sorted(list(set(m.decode("ascii") for m in pool_matches)))
        # Prioritize AWSMobileClient / CognitoIdentity pool if present, otherwise ap-northeast-1
        mobile_pool = next((p for p in pools if "e5a1e548" in p), None)
        if mobile_pool:
            results["cognito_identity_pool_id"] = mobile_pool
        else:
            ap_pools = [p for p in pools if p.startswith("ap-northeast-1:")]
            results["cognito_identity_pool_id"] = ap_pools[0] if ap_pools else pools[0]

    # 2. Search for API Key
    try:
        parser = DexParser(dex_bytes)
        # Try primary bytecode inspection
        api_key = parser.extract_api_key_via_bytecode()
        if not api_key:
            # Try heuristic string analysis
            api_key = parser.extract_api_key_heuristic()
        if api_key:
            results["api_key"] = api_key
    except Exception:
        pass

    return results


def extract_from_apk_file(apk_path: str) -> dict[str, str]:
    """Inspect an APK file or directory containing APKs/DEX files."""
    results: dict[str, str] = {}

    if os.path.isdir(apk_path):
        for root, _, files in os.walk(apk_path):
            for file in files:
                if file.endswith((".apk", ".dex")):
                    full = os.path.join(root, file)
                    res = extract_from_apk_file(full)
                    results.update(res)
                    if "api_key" in results and "cognito_identity_pool_id" in results:
                        return results
        return results

    # If it's a raw DEX file
    if apk_path.endswith(".dex"):
        with open(apk_path, "rb") as f:
            return extract_from_dex(f.read())

    # If it's a zip/apk archive
    with zipfile.ZipFile(apk_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith(".dex"):
                dex_data = zf.read(name)
                res = extract_from_dex(dex_data)
                for k, v in res.items():
                    if k not in results and v:
                        results[k] = v
                if "api_key" in results and "cognito_identity_pool_id" in results:
                    break

    return results


def pull_apk_via_adb() -> str:
    """Attempt to locate and pull the Sesame APK from a USB-connected Android device."""
    print("Checking for connected Android devices via ADB...")
    try:
        dev_out = subprocess.check_output(["adb", "devices"], text=True)
    except FileNotFoundError:
        raise RuntimeError("ADB not found. Please install Android platform tools or pass an APK file directly.")

    lines = [line.strip() for line in dev_out.strip().splitlines()[1:] if line.strip() and not line.startswith("*")]
    if not lines or not any("device" in l for l in lines):
        raise RuntimeError("No authorized Android device found via ADB. Connect your device and enable USB debugging.")

    print("Searching for installed Candy House Sesame package on device...")
    package_name = None
    for pkg in KNOWN_SESAME_PACKAGES:
        try:
            out = subprocess.check_output(["adb", "shell", f"pm path {pkg}"], text=True).strip()
            if out and "package:" in out:
                package_name = pkg
                apk_device_path = out.split("package:")[1].splitlines()[0].strip()
                break
        except Exception:
            continue

    if not package_name:
        raise RuntimeError(
            f"Could not find installed Sesame app on device. Checked packages: {', '.join(KNOWN_SESAME_PACKAGES)}"
        )

    print(f"Found package {package_name} at {apk_device_path}. Pulling APK...")
    tmp = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
    tmp.close()
    subprocess.check_call(["adb", "pull", apk_device_path, tmp.name])
    return tmp.name


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Extract AWS Cognito and API Gateway credentials from Candy House Sesame Android APK."
    )
    parser.add_argument(
        "apk_path",
        nargs="?",
        help="Path to Sesame .apk, .xapk, or classes.dex file",
    )
    parser.add_argument(
        "--adb",
        action="store_true",
        help="Automatically pull the Sesame APK from a connected Android device via ADB",
    )
    parser.add_argument(
        "--out",
        "-o",
        help="Path to save credentials as JSON (e.g. /config/sesame_credentials.json)",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Output credentials formatted as shell environment variable exports",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON to stdout",
    )

    args = parser.parse_args()

    temp_file = None
    target_path = args.apk_path

    if args.adb:
        try:
            temp_file = pull_apk_via_adb()
            target_path = temp_file
        except Exception as err:
            print(f"Error pulling APK via ADB: {err}", file=sys.stderr)
            return 1

    if not target_path:
        parser.print_help()
        print("\nError: Please provide path to a Sesame APK or use --adb.", file=sys.stderr)
        return 1

    if not os.path.exists(target_path):
        print(f"Error: File not found: {target_path}", file=sys.stderr)
        return 1

    try:
        creds = extract_from_apk_file(target_path)
    except Exception as err:
        print(f"Error parsing APK/DEX: {err}", file=sys.stderr)
        return 1
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

    api_key = creds.get("api_key")
    pool_id = creds.get("cognito_identity_pool_id")

    if not api_key and not pool_id:
        print("Error: Could not locate Candy House credentials in the provided file.", file=sys.stderr)
        return 1

    if args.out:
        out_data = {
            "api_key": api_key or "",
            "cognito_identity_pool_id": pool_id or "",
        }
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2)
        print(f"Successfully saved credentials to {args.out}")

    if args.json:
        print(json.dumps(creds, indent=2))
    elif args.env:
        if api_key:
            print(f"export SESAME_API_KEY='{api_key}'")
        if pool_id:
            print(f"export SESAME_COGNITO_POOL_ID='{pool_id}'")
    elif not args.out:
        print("\n" + "=" * 60)
        print("Candy House Sesame Credentials Extracted Successfully")
        print("=" * 60)
        print(f"  API Key:                  {api_key or 'Not Found'}")
        print(f"  Cognito Identity Pool ID: {pool_id or 'Not Found'}")
        print("=" * 60)
        print("\nHow to configure in Home Assistant:")
        print("  1. Navigate to Settings -> Devices & Services -> Sesame BLE.")
        print("  2. Click 'Configure' (Options Flow) on your Sesame device entry.")
        print("  3. Paste the API Key and Cognito Identity Pool ID into the fields.")
        print("\nOr save as a credentials file:")
        print(f"  python3 tools/extract_apk_credentials.py {target_path} --out /config/sesame_credentials.json")
        print("=" * 60 + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
