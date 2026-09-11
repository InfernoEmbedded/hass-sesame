"""Unit tests for sesame_client library helpers and sesame_cli."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from sesame_ble.sesame_client import (
    ProductModels,
    SesameAdData,
    SesameLock,
    SesameKeypad,
    is_keypad_model,
    create_sesame_device,
    scan_sesame_devices,
    find_sesame_device,
)
import sesame_cli


def test_is_keypad_model() -> None:
    """Test model detection for keypads and locks."""
    # None and empty
    assert is_keypad_model(None) is False
    assert is_keypad_model("") is False

    # Locks (string, int, enum)
    assert is_keypad_model("SESAME5") is False
    assert is_keypad_model("SESAME5_PRO") is False
    assert is_keypad_model("SESAME6") is False
    assert is_keypad_model("SESAME6_PRO") is False
    assert is_keypad_model(ProductModels.SESAME5) is False
    assert is_keypad_model(ProductModels.SESAME5.value) is False

    # Keypads & Touch devices (string, int, enum)
    assert is_keypad_model("SESAME_TOUCH") is True
    assert is_keypad_model("SESAME_TOUCH_PRO") is True
    assert is_keypad_model("SESAME_TOUCH_2") is True
    assert is_keypad_model("SESAME_TOUCH_2_PRO") is True
    assert is_keypad_model("SESAME_KEYPAD") is True
    assert is_keypad_model("SESAME_FACE_PRO") is True
    assert is_keypad_model("SESAME_FACE_2_AI") is True
    assert is_keypad_model(ProductModels.SESAME_TOUCH) is True
    assert is_keypad_model(ProductModels.SESAME_TOUCH_PRO) is True
    assert is_keypad_model(ProductModels.SESAME_TOUCH_2_PRO) is True
    assert is_keypad_model(ProductModels.SESAME_TOUCH_PRO.value) is True


def test_create_sesame_device() -> None:
    """Test factory creates correct class based on advertisement data."""
    mock_ble_device = MagicMock()
    test_uuid = UUID("11111111-2222-3333-4444-555555555555")

    # Lock creation
    lock_ad = SesameAdData(
        model_id=ProductModels.SESAME5.value,
        is_registered=True,
        device_uuid=test_uuid,
    )
    lock_device = create_sesame_device(mock_ble_device, lock_ad, secret_key="00" * 16)
    assert isinstance(lock_device, SesameLock)
    assert not isinstance(lock_device, SesameKeypad)

    # Keypad creation
    keypad_ad = SesameAdData(
        model_id=ProductModels.SESAME_TOUCH_PRO.value,
        is_registered=True,
        device_uuid=test_uuid,
    )
    keypad_device = create_sesame_device(mock_ble_device, keypad_ad, secret_key="00" * 16)
    assert isinstance(keypad_device, SesameKeypad)


@pytest.mark.asyncio
async def test_scan_sesame_devices() -> None:
    """Test scan_sesame_devices invokes scanner and processes Sesame manufacturer data."""
    test_uuid = UUID("11111111-2222-3333-4444-555555555555")
    # 0x055A company ID payload: model_id (2 bytes LE) + flags (1 byte) + uuid (16 bytes)
    import struct
    raw_mfg = struct.pack("<HB16s", ProductModels.SESAME5.value, 1, test_uuid.bytes)

    mock_scanner_instance = MagicMock()
    mock_scanner_instance.start = AsyncMock()
    mock_scanner_instance.stop = AsyncMock()

    with patch("sesame_ble.sesame_client.device.BleakScanner") as mock_scanner_cls:
        mock_scanner_cls.return_value = mock_scanner_instance

        cb_calls = []

        async def run_scan():
            return await scan_sesame_devices(
                timeout=0.01,
                callback=lambda dev, ad: cb_calls.append((dev, ad)),
            )

        task = asyncio.create_task(run_scan())
        await asyncio.sleep(0.005)

        # Simulate detection callback invocation
        detection_callback = mock_scanner_cls.call_args.kwargs.get("detection_callback")
        mock_dev = MagicMock()
        mock_dev.address = "AA:BB:CC:DD:EE:FF"
        mock_adv = MagicMock()
        mock_adv.manufacturer_data = {0x055A: raw_mfg}

        detection_callback(mock_dev, mock_adv)

        found = await task
        assert "AA:BB:CC:DD:EE:FF" in found
        dev, ad = found["AA:BB:CC:DD:EE:FF"]
        assert ad.model_id == ProductModels.SESAME5.value
        assert ad.device_uuid == test_uuid
        assert len(cb_calls) == 1


@pytest.mark.asyncio
async def test_find_sesame_device() -> None:
    """Test find_sesame_device finds matching address and parses advertisement."""
    test_uuid = UUID("22222222-3333-4444-5555-666666666666")
    import struct
    raw_mfg = struct.pack("<HB16s", ProductModels.SESAME_TOUCH_PRO.value, 1, test_uuid.bytes)

    mock_dev = MagicMock()
    mock_dev.address = "11:22:33:44:55:66"
    mock_adv = MagicMock()
    mock_adv.manufacturer_data = {0x055A: raw_mfg}

    mock_scanner_instance = MagicMock()
    mock_scanner_instance.start = AsyncMock()
    mock_scanner_instance.stop = AsyncMock()
    mock_scanner_instance.discovered_devices_and_advertisement_data = {
        "11:22:33:44:55:66": (mock_dev, mock_adv),
    }

    with patch("sesame_ble.sesame_client.device.BleakScanner") as mock_scanner_cls:
        mock_scanner_cls.return_value = mock_scanner_instance
        result = await find_sesame_device("11:22:33:44:55:66", timeout=0.1)

        assert result is not None
        found_dev, found_ad = result
        assert found_dev == mock_dev
        assert found_ad is not None
        assert found_ad.model_id == ProductModels.SESAME_TOUCH_PRO.value


@pytest.mark.asyncio
async def test_sesame_cli_scan() -> None:
    """Test sesame_cli scan_devices() output."""
    mock_dev = MagicMock()
    mock_dev.name = "My Sesame"
    mock_dev.address = "AA:BB:CC:DD:EE:FF"
    mock_ad = SesameAdData(
        model_id=ProductModels.SESAME5.value,
        is_registered=True,
        device_uuid=UUID("00000000-0000-0000-0000-000000000001"),
    )

    with patch("sesame_cli.scan_sesame_devices", new_callable=AsyncMock) as mock_scan:
        async def fake_scan(timeout=5.0, callback=None):
            if callback:
                callback(mock_dev, mock_ad)
            return {"AA:BB:CC:DD:EE:FF": (mock_dev, mock_ad)}

        mock_scan.side_effect = fake_scan
        await sesame_cli.scan_devices()
        mock_scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_sesame_cli_run_client_lock() -> None:
    """Test sesame_cli run_client() with lock action."""
    mock_dev = MagicMock()
    mock_dev.address = "AA:BB:CC:DD:EE:FF"
    mock_ad = SesameAdData(
        model_id=ProductModels.SESAME5.value,
        is_registered=True,
        device_uuid=UUID("00000000-0000-0000-0000-000000000001"),
    )

    args = MagicMock()
    args.qr_url = None
    args.address = "AA:BB:CC:DD:EE:FF"
    args.secret = "00112233445566778899aabbccddeeff"
    args.register = False
    args.lock = True
    args.unlock = False
    args.get_passcodes = False
    args.add_passcode = None
    args.delete_passcode = None

    mock_device_instance = MagicMock()
    mock_device_instance.connect = AsyncMock()
    mock_device_instance.login = AsyncMock(return_value=12345678)
    mock_device_instance.mech_status = b"\x00" * 7
    mock_device_instance.lock = AsyncMock()
    mock_device_instance.disconnect = AsyncMock()

    with patch("sesame_cli.find_sesame_device", new_callable=AsyncMock) as mock_find, \
         patch("sesame_cli.create_sesame_device", return_value=mock_device_instance) as mock_create, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_find.return_value = (mock_dev, mock_ad)

        ret = await sesame_cli.run_client(args)
        assert ret == 0
        mock_create.assert_called_once()
        mock_device_instance.connect.assert_awaited_once()
        mock_device_instance.login.assert_awaited_once()
        mock_device_instance.lock.assert_awaited_once()
        mock_device_instance.disconnect.assert_awaited_once()
