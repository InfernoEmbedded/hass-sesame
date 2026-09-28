"""Unit tests for Candy House firmware fetcher module."""

import io
import json
import os
import tempfile
import urllib.error
import zipfile
from unittest.mock import MagicMock, patch
import pytest

from custom_components.sesame_ble.firmware import (
    FirmwareFetchError,
    FirmwareCredentialsMissingError,
    extract_version_from_zip,
    fetch_latest_firmware_version,
    find_local_firmware_zip,
    get_cognito_credentials,
    get_product_type_id,
    load_credentials_from_file,
    parse_version_from_firmware_name,
    query_cloud_firmware_info,
    resolve_credentials,
    sign_aws_v4,
    _firmware_version_cache,
    _cached_credentials,
)
from pysesame_ble import ProductModels


def test_get_product_type_id():
    """Verify mapping of model representations to integer productType IDs."""
    assert get_product_type_id(ProductModels.SESAME_FACE_PRO_AI) == 22
    assert get_product_type_id("SESAME_FACE_PRO_AI") == 22
    assert get_product_type_id("Sesame Face Pro AI") == 22
    assert get_product_type_id("SESAME6_PRO") == 21
    assert get_product_type_id("sesame6_pro") == 21
    assert get_product_type_id("SESAME5") == 5
    assert get_product_type_id("sesame2") == 0
    assert get_product_type_id(22) == 22
    assert get_product_type_id("9999") == 9999
    assert get_product_type_id(None) is None
    assert get_product_type_id("UNKNOWN_DEVICE_XYZ") is None


def test_parse_version_from_firmware_name():
    """Test parsing logic for firmwareName strings."""
    assert parse_version_from_firmware_name("sesameface1proai_30_22_e877d5", 22) == "3.0-22-e877d5"
    assert parse_version_from_firmware_name("sesame6pro_30_21_956bb2", 21) == "3.0-21-956bb2"
    assert parse_version_from_firmware_name("sesamebike1_21_3_d7162a", 3) == "2.1-3-d7162a"
    assert parse_version_from_firmware_name("sesame_221_0_8c080c", 0) == "2.1-0-8c080c"
    assert parse_version_from_firmware_name("invalid_name", 22) is None
    assert parse_version_from_firmware_name("", 22) is None


def test_sign_aws_v4():
    """Verify AWS SigV4 header calculation."""
    creds = {
        "access_key": "ASIAEXAMPLEKEY",
        "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "session_token": "AQoDYXdzEJr1...token...",
    }
    headers = {"x-api-key": "testkey"}
    signed = sign_aws_v4("GET", "https://app.candyhouse.co/prod/device/v1/firmwareZipUrl?productType=22", headers, creds)
    assert "Authorization" in signed
    assert signed["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIAEXAMPLEKEY/")
    assert "x-amz-date" in signed
    assert signed["x-amz-security-token"] == creds["session_token"]
    assert signed["host"] == "app.candyhouse.co"


def test_extract_version_from_zip():
    """Verify extracting version string from in-memory zip containing firmware.bin."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("firmware.dat", b"\x00" * 32)
        zf.writestr("firmware.bin", b"\xaa" * 100 + b"3.0-22-e877d5" + b"\xbb" * 100)
    zip_bytes = buf.getvalue()

    mock_resp = MagicMock()
    mock_resp.read.return_value = zip_bytes
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        ver = extract_version_from_zip("https://firmware.candyhouse.co/test.zip")
        assert ver == "3.0-22-e877d5"


def test_resolve_credentials():
    """Test resolution of credentials from arguments, environment, and file."""
    # 1. Explicit arguments
    ak, pi = resolve_credentials(api_key="arg_key", pool_id="arg_pool")
    assert ak == "arg_key"
    assert pi == "arg_pool"

    # 2. Environment variables
    with patch.dict(os.environ, {"SESAME_API_KEY": "env_key", "SESAME_COGNITO_POOL_ID": "env_pool"}):
        ak, pi = resolve_credentials()
        assert ak == "env_key"
        assert pi == "env_pool"

    # 3. Credentials file
    with tempfile.TemporaryDirectory() as tmpdir:
        creds_file = os.path.join(tmpdir, "sesame_credentials.json")
        with open(creds_file, "w", encoding="utf-8") as f:
            json.dump({
                "api_key": "file_key",
                "cognito_identity_pool_id": "file_pool"
            }, f)

        with patch.dict(os.environ, {}, clear=True):
            ak, pi = resolve_credentials(config_dir=tmpdir)
            assert ak == "file_key"
            assert pi == "file_pool"

    # 4. Missing credentials raises FirmwareCredentialsMissingError
    with patch.dict(os.environ, {}, clear=True):
        with patch("custom_components.sesame_ble.firmware.load_credentials_from_file", return_value={}):
            with pytest.raises(FirmwareCredentialsMissingError):
                resolve_credentials()


def test_find_local_firmware_zip():
    """Verify local offline firmware archive detection and version extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "sesameface1proai_30_22_e877d5.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("firmware.bin", b"\x00" * 32 + b"3.0-22-e877d5" + b"\x00" * 32)
        with open(zip_path, "wb") as f:
            f.write(buf.getvalue())

        # Match by productType 22
        res = find_local_firmware_zip(22, model_name="SESAME_FACE_PRO_AI", search_dirs=[tmpdir])
        assert res is not None
        version, data = res
        assert version == "3.0-22-e877d5"
        assert len(data) > 0

        # Non-matching productType
        assert find_local_firmware_zip(99, model_name="UNKNOWN", search_dirs=[tmpdir]) is None


@patch("urllib.request.urlopen")
def test_get_cognito_credentials(mock_urlopen):
    """Test acquiring and caching Cognito credentials."""
    import custom_components.sesame_ble.firmware as fw_mod
    fw_mod._cached_credentials = None

    mock_id_resp = MagicMock()
    mock_id_resp.read.return_value = json.dumps({"IdentityId": "ap-northeast-1:12345"}).encode()
    mock_id_resp.__enter__.return_value = mock_id_resp

    mock_creds_resp = MagicMock()
    mock_creds_resp.read.return_value = json.dumps({
        "Credentials": {
            "AccessKeyId": "AKIA1111",
            "SecretKey": "SECRET2222",
            "SessionToken": "TOKEN3333",
        }
    }).encode()
    mock_creds_resp.__enter__.return_value = mock_creds_resp

    mock_urlopen.side_effect = [mock_id_resp, mock_creds_resp]

    creds = get_cognito_credentials(pool_id="ap-northeast-1:test-pool", force_refresh=True)
    assert creds["access_key"] == "AKIA1111"
    assert creds["secret_key"] == "SECRET2222"
    assert creds["session_token"] == "TOKEN3333"

    # Second call should use cached credentials
    creds2 = get_cognito_credentials(pool_id="ap-northeast-1:test-pool", force_refresh=False)
    assert creds2["access_key"] == "AKIA1111"
    assert mock_urlopen.call_count == 2  # No new network requests


@patch("custom_components.sesame_ble.firmware.get_cognito_credentials")
@patch("custom_components.sesame_ble.firmware.query_cloud_firmware_info")
@patch("custom_components.sesame_ble.firmware.extract_version_from_zip")
def test_fetch_latest_firmware_version_success(mock_extract, mock_query, mock_creds):
    """Test successful firmware version fetch and caching."""
    import custom_components.sesame_ble.firmware as fw_mod
    fw_mod._firmware_version_cache.clear()

    mock_creds.return_value = {"access_key": "k", "secret_key": "s", "session_token": "t"}
    mock_query.return_value = {
        "ok": True,
        "productType": 22,
        "firmwareName": "sesameface1proai_30_22_e877d5",
        "zipUrl": "https://firmware.candyhouse.co/sesameface1proai_30_22_e877d5.zip",
    }
    mock_extract.return_value = "3.0-22-e877d5"

    ver = fetch_latest_firmware_version(
        22,
        force_refresh=True,
        api_key="mock_key",
        pool_id="ap-northeast-1:mock_pool",
    )
    assert ver == "3.0-22-e877d5"

    # Next call uses cache
    ver_cached = fetch_latest_firmware_version(
        22,
        force_refresh=False,
        api_key="mock_key",
        pool_id="ap-northeast-1:mock_pool",
    )
    assert ver_cached == "3.0-22-e877d5"
    assert mock_query.call_count == 1


def test_fetch_latest_firmware_version_local_priority():
    """Verify that local offline firmware archive is prioritized over cloud calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "22.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("firmware.bin", b"3.0-22-localbuild")
        with open(zip_path, "wb") as f:
            f.write(buf.getvalue())

        with patch("custom_components.sesame_ble.firmware.find_local_firmware_zip", return_value=("3.0-22-localbuild", buf.getvalue())):
            ver = fetch_latest_firmware_version(22, force_refresh=True)
            assert ver == "3.0-22-localbuild"


@patch("custom_components.sesame_ble.firmware.get_cognito_credentials")
@patch("custom_components.sesame_ble.firmware.query_cloud_firmware_info")
def test_fetch_latest_firmware_version_failure_raises(mock_query, mock_creds):
    """Verify that fetch_latest_firmware_version raises FirmwareFetchError on server failure."""
    import custom_components.sesame_ble.firmware as fw_mod
    fw_mod._firmware_version_cache.clear()

    mock_creds.return_value = {"access_key": "k", "secret_key": "s", "session_token": "t"}
    mock_query.side_effect = FirmwareFetchError("HTTP 503 Service Unavailable")

    with pytest.raises(FirmwareFetchError):
        fetch_latest_firmware_version(
            22,
            force_refresh=True,
            api_key="mock_key",
            pool_id="ap-northeast-1:mock_pool",
        )
