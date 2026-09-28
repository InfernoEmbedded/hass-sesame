"""Tests for keypad battery optimization and on-demand connection model."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from pysesame_ble import is_keypad_model
from sesame_ble import (
    DOMAIN,
    SesameDeviceWrapper,
    async_setup_entry,
)
from sesame_ble.const import CONF_DEVICE_UUID, CONF_MODEL, CONF_SECRET_KEY
from sesame_ble.sensor import SENSOR_DESCRIPTIONS_COMMON, SesameSensor
from sesame_ble.update import SesameFirmwareUpdateEntity
from sesame_ble.views import SesameKeypadSyncView


@pytest.fixture
def mock_keypad_entry():
    """Create a mock config entry for a Sesame Touch keypad."""
    entry = MagicMock()
    entry.entry_id = "test_keypad_entry_id"
    entry.unique_id = "test_keypad_unique_id"
    entry.title = "Sesame Touch"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
        CONF_DEVICE_UUID: str(UUID("11200509-0108-0802-b700-6500ffffffff")),
    }
    return entry


@pytest.mark.asyncio
async def test_keypad_model_detection():
    """Verify that Touch, Touch Pro, Face, and Face AI are identified as keypad models."""
    assert is_keypad_model("SESAME_TOUCH") is True
    assert is_keypad_model("SESAME_TOUCH_PRO") is True
    assert is_keypad_model("SESAME_TOUCH_2_PRO") is True
    assert is_keypad_model("SESAME_FACE") is True
    assert is_keypad_model("SESAME_FACE_AI") is True
    assert is_keypad_model("SESAME_FACE_2") is True
    assert is_keypad_model("SESAME5") is False
    assert is_keypad_model("SESAME5_PRO") is False
    assert is_keypad_model("SESAME_BOT") is False


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_keypad_setup_does_not_start_persistent_connect_or_scheduler(
    mock_bluetooth, mock_keypad_entry
):
    """Test that keypad setup avoids background connect loops and 60-second polling scheduler."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
    mock_ble_device.name = "Sesame Touch"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mock_bluetooth.async_register_callback.return_value = MagicMock()

    with (
        patch.object(
            SesameDeviceWrapper, "_async_connect_background"
        ) as mock_connect_bg,
        patch.object(SesameDeviceWrapper, "_scheduler_loop") as mock_scheduler,
        patch.object(
            SesameDeviceWrapper, "_keypad_battery_check_loop"
        ) as mock_battery_loop,
    ):
        setup_ok = await async_setup_entry(hass, mock_keypad_entry)
        assert setup_ok is True

        # Keypad must NOT start persistent background connection or 60s credential scheduler loop
        mock_connect_bg.assert_not_called()
        mock_scheduler.assert_not_called()

        # Keypad SHOULD start the 24h battery loop and BLE advertisement tracking
        mock_battery_loop.assert_called_once()
        mock_bluetooth.async_register_callback.assert_called_once()

        wrapper = hass.data[DOMAIN][mock_keypad_entry.entry_id]
        # Reconnect attempts should be 0 on keypad to prevent pysesame_ble auto reconnect loops
        assert getattr(wrapper.device, "_reconnect_limit", None) == 0


@pytest.mark.asyncio
async def test_keypad_async_connect_background_exits_immediately():
    """Verify that _async_connect_background exits immediately for keypads."""
    wrapper = MagicMock(spec=SesameDeviceWrapper)
    wrapper.model_name = "SESAME_TOUCH"
    wrapper.device = MagicMock()
    wrapper.device.connect = AsyncMock()

    # Call the actual _async_connect_background method on the wrapper
    await SesameDeviceWrapper._async_connect_background(wrapper)

    # Should exit immediately without attempting connection
    wrapper.device.connect.assert_not_called()


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_keypad_entity_availability_from_advertisements(mock_bluetooth):
    """Verify keypad entity is available when disconnected if advertisements are seen."""
    hass = MagicMock()
    wrapper = MagicMock()
    wrapper.hass = hass
    wrapper.model_name = "SESAME_TOUCH"
    wrapper.ble_device.address = "AA:BB:CC:DD:EE:FF"
    wrapper.entry.unique_id = "test_mac_keypad"
    wrapper.entry.entry_id = "test_entry"

    # Device is disconnected and not logged in
    mock_device = MagicMock()
    mock_device.is_connected = False
    mock_device.is_logged_in = False
    mock_device.battery_percentage = None
    mock_device.mech_status = None
    wrapper.device = mock_device
    wrapper.battery_percentage = 95
    wrapper.battery_voltage = 3.1

    # Bind real is_available property
    wrapper.is_available = SesameDeviceWrapper.is_available.fget(wrapper)

    # When no BLE device is seen by bluetooth, is_available is False
    mock_bluetooth.async_ble_device_from_address.return_value = None
    mock_bluetooth.async_last_service_info.return_value = None
    assert SesameDeviceWrapper.is_available.fget(wrapper) is False

    # When BLE advertisements are seen by bluetooth, is_available becomes True
    mock_bluetooth.async_ble_device_from_address.return_value = MagicMock()
    wrapper.is_available = SesameDeviceWrapper.is_available.fget(wrapper)
    assert wrapper.is_available is True

    # Test SesameSensor availability
    battery_desc = next(d for d in SENSOR_DESCRIPTIONS_COMMON if d.key == "battery")
    sensor = SesameSensor(wrapper, battery_desc)
    assert sensor.available is True
    assert sensor.native_value == 95

    # Test update entity availability
    update_entity = SesameFirmwareUpdateEntity(wrapper)
    assert update_entity.available is True


@pytest.mark.asyncio
async def test_on_demand_connection_lifecycle():
    """Verify on_demand_connection connects, yields device, and cleanly disconnects."""
    wrapper = MagicMock(spec=SesameDeviceWrapper)
    wrapper.model_name = "SESAME_TOUCH"
    wrapper.update_listeners = []
    wrapper._record_battery_status = MagicMock()

    mock_dev = MagicMock()
    mock_dev.is_logged_in = False
    mock_dev.is_connected = True
    wrapper.device = mock_dev

    wrapper.async_connect = AsyncMock()
    wrapper.async_disconnect_device = AsyncMock()

    async def fake_connect():
        mock_dev.is_logged_in = True

    wrapper.async_connect.side_effect = fake_connect

    # Run the real method
    async with SesameDeviceWrapper.async_on_demand_connection(
        wrapper, timeout=5.0
    ) as dev:
        assert dev is mock_dev
        wrapper.async_connect.assert_awaited_once()
        wrapper.async_disconnect_device.assert_not_called()

    # After exiting context, async_disconnect_device should be called cleanly
    wrapper.async_disconnect_device.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_keypad_credentials():
    """Verify async_sync_keypad_credentials fetches tables, updates battery, and saves store."""
    from pysesame_ble import BaseKeypad

    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    wrapper = MagicMock(spec=SesameDeviceWrapper)
    wrapper.hass = hass
    wrapper.model_name = "SESAME_TOUCH"
    wrapper.update_listeners = []
    wrapper._record_battery_status = MagicMock()
    wrapper.recently_deleted_passcodes = {}
    wrapper.recently_deleted_cards = {}
    wrapper.recently_deleted_fingerprints = {}
    wrapper.recently_deleted_faces = {}
    wrapper.recently_deleted_palms = {}
    wrapper.store = MagicMock()
    wrapper.store.async_save = AsyncMock()
    wrapper.logical_passcodes = {}
    wrapper.logical_cards = {}
    wrapper.logical_fingerprints = {}
    wrapper._sync_lock = asyncio.Lock()
    wrapper._handle_status_update = MagicMock()

    mock_dev = MagicMock(spec=BaseKeypad)
    mock_dev.is_logged_in = True
    mock_dev.is_connected = True
    mock_dev.get_passcodes = AsyncMock()
    mock_dev.get_cards = AsyncMock()
    mock_dev.get_fingerprints = AsyncMock()
    mock_dev.get_faces = AsyncMock()
    mock_dev.get_palms = AsyncMock()
    mock_dev.passcodes = {"1234": {"name": "Admin PIN", "code": "1234"}}
    mock_dev.cards = {}
    mock_dev.fingerprints = {}
    mock_dev.battery_percentage = 88
    mock_dev.battery_voltage = 3.0
    mock_dev.mech_status = MagicMock()
    wrapper.device = mock_dev

    wrapper.async_on_demand_connection = (
        SesameDeviceWrapper.async_on_demand_connection.__get__(
            wrapper, SesameDeviceWrapper
        )
    )
    wrapper._sync_and_apply_schedules = (
        SesameDeviceWrapper._sync_and_apply_schedules.__get__(
            wrapper, SesameDeviceWrapper
        )
    )
    wrapper.async_disconnect_device = AsyncMock()
    wrapper.async_connect = AsyncMock()

    # Run credentials sync
    await SesameDeviceWrapper.async_sync_keypad_credentials(wrapper)

    mock_dev.get_passcodes.assert_awaited_once()
    mock_dev.get_cards.assert_awaited_once()
    mock_dev.get_fingerprints.assert_awaited_once()
    wrapper.store.async_save.assert_awaited_once()
    wrapper._handle_status_update.assert_called()


@pytest.mark.asyncio
async def test_keypad_sync_api_view():
    """Test SesameKeypadSyncView API endpoint."""
    hass = MagicMock()
    entry_id = "test_keypad_entry"

    keypad_wrapper = MagicMock()
    keypad_wrapper.model_name = "SESAME_TOUCH"
    keypad_wrapper.async_sync_keypad_credentials = AsyncMock()

    hass.data = {DOMAIN: {entry_id: keypad_wrapper}}

    view = SesameKeypadSyncView(hass)
    req = MagicMock()
    req.json = AsyncMock(return_value={"entry_id": entry_id})

    resp = await view.post(req)
    assert resp.status == 200
    keypad_wrapper.async_sync_keypad_credentials.assert_awaited_once()


@pytest.mark.asyncio
async def test_lock_reactive_history_flush_on_status_update():
    """Verify lock status updates trigger debounced history flushing reactively."""
    wrapper = MagicMock(spec=SesameDeviceWrapper)
    wrapper.model_name = "SESAME5"
    wrapper._history_flush_task = None
    wrapper.update_listeners = []
    wrapper.fetch_and_flush_history = AsyncMock()

    mock_lock = MagicMock()
    mock_lock.is_logged_in = True
    mock_lock.fetch_and_flush_history = AsyncMock()
    mock_lock.battery_percentage = 90
    mock_lock.battery_voltage = 3.0

    mock_status = MagicMock()
    mock_status.is_moving = False

    # Bind real _trigger_reactive_history_flush and _handle_status_update
    wrapper._trigger_reactive_history_flush = (
        SesameDeviceWrapper._trigger_reactive_history_flush.__get__(
            wrapper, SesameDeviceWrapper
        )
    )
    wrapper._handle_status_update = SesameDeviceWrapper._handle_status_update.__get__(
        wrapper, SesameDeviceWrapper
    )

    wrapper._handle_status_update(mock_lock, mock_status)
    assert wrapper._history_flush_task is not None

    # Await the scheduled debounced task
    await wrapper._history_flush_task
    wrapper.fetch_and_flush_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_keypad_does_not_trigger_reactive_history_flush():
    """Verify keypad status updates do not trigger reactive history flushing."""
    wrapper = MagicMock(spec=SesameDeviceWrapper)
    wrapper.model_name = "SESAME_TOUCH"
    wrapper._history_flush_task = None
    wrapper.update_listeners = []
    wrapper.fetch_and_flush_history = AsyncMock()

    mock_keypad = MagicMock()
    mock_keypad.is_logged_in = True
    mock_keypad.battery_percentage = 85
    mock_keypad.battery_voltage = 2.9
    mock_status = MagicMock()

    wrapper._trigger_reactive_history_flush = (
        SesameDeviceWrapper._trigger_reactive_history_flush.__get__(
            wrapper, SesameDeviceWrapper
        )
    )
    wrapper._handle_status_update = SesameDeviceWrapper._handle_status_update.__get__(
        wrapper, SesameDeviceWrapper
    )

    wrapper._handle_status_update(mock_keypad, mock_status)
    assert wrapper._history_flush_task is None
    wrapper.fetch_and_flush_history.assert_not_called()
