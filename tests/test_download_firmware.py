"""Unit tests for the download_firmware script."""

import io
import json
import os
import tempfile
import zipfile
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

import download_firmware


def test_sign_aws_v4() -> None:
    """Test AWS SigV4 request signer generates required headers."""
    creds = {
        "access_key": "AKIAEXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "session_token": "TOKEN123",
    }
    url = "https://app.candyhouse.co/prod/device/v1/firmwareZipUrl?productType=21"
    headers = {"x-api-key": "TESTKEY"}

    signed_headers = download_firmware.sign_aws_v4("GET", url, headers, creds)

    assert "Authorization" in signed_headers
    assert "x-amz-date" in signed_headers
    assert "x-amz-security-token" in signed_headers
    assert signed_headers["x-amz-security-token"] == "TOKEN123"
    assert signed_headers["host"] == "app.candyhouse.co"
    assert signed_headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "Signature=" in signed_headers["Authorization"]


def test_get_cognito_credentials() -> None:
    """Test retrieving temporary credentials via Cognito identity pool."""
    mock_id_resp = io.BytesIO(json.dumps({"IdentityId": "ap-northeast-1:test-id"}).encode("utf-8"))
    mock_creds_resp = io.BytesIO(
        json.dumps(
            {
                "Credentials": {
                    "AccessKeyId": "ASIA12345",
                    "SecretKey": "SECRET6789",
                    "SessionToken": "TOKENABC",
                }
            }
        ).encode("utf-8")
    )

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [mock_id_resp, mock_creds_resp]
        creds = download_firmware.get_cognito_credentials()

        assert creds["access_key"] == "ASIA12345"
        assert creds["secret_key"] == "SECRET6789"
        assert creds["session_token"] == "TOKENABC"
        assert mock_urlopen.call_count == 2


def test_query_firmware_info_success() -> None:
    """Test query_firmware_info returns parsed response on success."""
    creds = {
        "access_key": "AKIAEXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "session_token": "TOKEN123",
    }
    mock_data = {
        "ok": True,
        "productType": 21,
        "fileName": "sesame6pro_30_21_956bb2.zip",
        "firmwareName": "sesame6pro_30_21_956bb2",
        "zipUrl": "https://firmware.candyhouse.co/sesame6pro_30_21_956bb2.zip",
    }
    mock_resp = io.BytesIO(json.dumps(mock_data).encode("utf-8"))

    with patch("urllib.request.urlopen", return_value=mock_resp):
        info = download_firmware.query_firmware_info(21, creds, firmware_dir="prod")
        assert info is not None
        assert info["fileName"] == "sesame6pro_30_21_956bb2.zip"
        assert info["productType"] == 21


def test_query_firmware_info_not_found() -> None:
    """Test query_firmware_info returns None when API returns ok=False or 404."""
    creds = {
        "access_key": "AKIAEXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "session_token": "TOKEN123",
    }
    mock_resp = io.BytesIO(json.dumps({"ok": False, "message": "No firmware"}).encode("utf-8"))

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert download_firmware.query_firmware_info(1, creds) is None

    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(None, 404, "Not Found", None, None)):
        assert download_firmware.query_firmware_info(999, creds) is None


def test_process_model_dry_run() -> None:
    """Test process_model in dry_run mode does not download or create files."""
    creds = {"access_key": "A", "secret_key": "B", "session_token": "C"}
    mock_info = {
        "ok": True,
        "productType": 21,
        "fileName": "test.zip",
        "firmwareName": "test_fw",
        "zipUrl": "https://example.com/test.zip",
    }

    with patch("download_firmware.query_firmware_info", return_value=mock_info):
        with tempfile.TemporaryDirectory() as tmpdir:
            res = download_firmware.process_model(
                "test_model",
                21,
                creds,
                tmpdir,
                dry_run=True,
            )
            assert res == mock_info
            assert not os.path.exists(os.path.join(tmpdir, "test_model"))


def test_process_model_download_and_extract() -> None:
    """Test process_model downloads, extracts archive, and writes metadata."""
    creds = {"access_key": "A", "secret_key": "B", "session_token": "C"}
    mock_info = {
        "ok": True,
        "productType": 21,
        "fileName": "test.zip",
        "firmwareName": "test_fw_v1",
        "zipUrl": "https://example.com/test.zip",
    }

    # Create an in-memory zip file
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("manifest.json", '{"manifest": {"bin_file": "firmware.bin"}}')
        zf.writestr("firmware.bin", b"\x00\x01\x02\x03")

    def fake_download(url, dest_path):
        with open(dest_path, "wb") as f:
            f.write(zip_buffer.getvalue())

    with patch("download_firmware.query_firmware_info", return_value=mock_info), \
         patch("download_firmware.download_file", side_effect=fake_download):
        with tempfile.TemporaryDirectory() as tmpdir:
            res = download_firmware.process_model(
                "sesame6_pro",
                21,
                creds,
                tmpdir,
                extract=True,
            )
            assert res == mock_info
            model_dir = os.path.join(tmpdir, "sesame6_pro")
            assert os.path.isfile(os.path.join(model_dir, "test.zip"))
            assert os.path.isfile(os.path.join(model_dir, "manifest.json"))
            assert os.path.isfile(os.path.join(model_dir, "firmware.bin"))
            assert os.path.isfile(os.path.join(model_dir, "metadata.json"))

            with open(os.path.join(model_dir, "metadata.json")) as f:
                meta = json.load(f)
                assert meta["model"] == "sesame6_pro"
                assert meta["product_type"] == 21
                assert meta["firmware_name"] == "test_fw_v1"


def test_main_list_models(capsys) -> None:
    """Test --list-models flag prints available models and exits cleanly."""
    with patch("sys.argv", ["download_firmware.py", "--list-models"]):
        exit_code = download_firmware.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Available models:" in captured.out
        assert "sesame6_pro" in captured.out
        assert "sesame_touch_2_pro" in captured.out
