"""Unit tests for the APK credential extractor tool."""

import json
import os
import struct
import tempfile
import zipfile
import pytest

from tools.extract_apk_credentials import (
    DexParser,
    extract_from_apk_file,
    extract_from_dex,
)


def create_fake_dex_with_credentials(api_key: str, pool_id: str) -> bytes:
    """Build a minimal valid DEX structure containing test credentials."""
    # DEX format header (112 bytes)
    # 0x00: magic (8 bytes): "dex\n035\0"
    # 0x38: string_ids_size (4 bytes uint)
    # 0x3C: string_ids_off (4 bytes uint)
    magic = b"dex\n035\x00"

    strings = [
        api_key,
        "ap-northeast-1",
        f"Default Pool: {pool_id}",
    ]
    # Sort strings lexicographically as required by DEX format
    strings_sorted = sorted(strings)

    # Encode strings into data items
    string_data_items = []
    for s in strings_sorted:
        s_bytes = s.encode("utf-8")
        # Length in ULEB128 (assuming < 128 chars for simplicity)
        item = bytes([len(s_bytes)]) + s_bytes + b"\x00"
        string_data_items.append(item)

    header_size = 112
    string_ids_size = len(strings_sorted)
    string_ids_off = header_size

    # Position string data items after string_ids table (each entry is 4 bytes)
    current_data_off = string_ids_off + string_ids_size * 4
    string_data_offsets = []
    for item in string_data_items:
        string_data_offsets.append(current_data_off)
        current_data_off += len(item)

    # Assemble header
    header = bytearray(112)
    header[0:8] = magic
    struct.pack_into("<II", header, 56, string_ids_size, string_ids_off)

    # Assemble string_ids
    string_ids = bytearray()
    for off in string_data_offsets:
        string_ids.extend(struct.pack("<I", off))

    # Assemble string data
    string_data = b"".join(string_data_items)

    # Add dummy bytecode referencing ap-northeast-1 followed by api_key
    region_idx = strings_sorted.index("ap-northeast-1")
    api_key_idx = strings_sorted.index(api_key)

    # const-string v1, region_idx
    # const-string v2, api_key_idx
    bytecode = bytes([
        0x1A, 0x01, region_idx & 0xFF, (region_idx >> 8) & 0xFF,
        0x1A, 0x02, api_key_idx & 0xFF, (api_key_idx >> 8) & 0xFF,
    ])

    return bytes(header + string_ids + string_data + bytecode)


def test_extract_from_dex_synthetic():
    """Test extracting credentials from synthetic DEX binary."""
    test_key = "K123456789aAbBcCdDeEfFgGhHiIjJkKlLmMnNoO"
    test_pool = "ap-northeast-1:11111111-2222-3333-4444-555555555555"

    dex = create_fake_dex_with_credentials(test_key, test_pool)
    res = extract_from_dex(dex)

    assert res.get("api_key") == test_key
    assert res.get("cognito_identity_pool_id") == test_pool


def test_extract_from_apk_synthetic():
    """Test extracting credentials from a zip/apk archive containing DEX."""
    test_key = "X123456789aAbBcCdDeEfFgGhHiIjJkKlLmMnNoO"
    test_pool = "ap-northeast-1:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    dex = create_fake_dex_with_credentials(test_key, test_pool)

    with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "w") as zf:
            zf.writestr("AndroidManifest.xml", b"<manifest/>")
            zf.writestr("classes.dex", dex)

        res = extract_from_apk_file(tmp_path)
        assert res.get("api_key") == test_key
        assert res.get("cognito_identity_pool_id") == test_pool
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_extract_from_real_sesame_apk():
    """Test extracting credentials from actual Sesame APK if present in workspace."""
    apk_path = "sesame_apk/Sesame_Open_Sesame_3.0.266_APKPure.apk"
    if not os.path.isfile(apk_path):
        pytest.skip("Sesame APK not found in workspace")

    res = extract_from_apk_file(apk_path)
    api_key = res.get("api_key")
    pool_id = res.get("cognito_identity_pool_id")
    assert api_key and len(api_key) == 40
    assert pool_id and pool_id.startswith("ap-northeast-1:") and len(pool_id) > 20

