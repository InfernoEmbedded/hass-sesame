import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import struct
from uuid import UUID

from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import EntityCategory
import sesame_ble
import sesame_ble.lock
import sesame_ble.sensor
import sesame_ble.button
import sesame_ble.number
import sesame_ble.select
from sesame_ble.const import DOMAIN, CONF_SECRET_KEY, CONF_MODEL, CONF_DEVICE_UUID
from sesame_ble.sesame_client import COMPANY_ID, ProductModels, SesameAdData

TEST_UUID = UUID("01234567-89ab-cdef-0123-456789abcdef")


class MockAdvertisementData:
    def __init__(self, mfg_data):
        self.manufacturer_data = {COMPANY_ID: mfg_data}


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_calibration_buttons_setup_and_press(mock_bluetooth) -> None:
    """Tests that lock position calibration buttons set up correctly and trigger correct commands."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    # Mock bluetooth discovery
    mock_ble_device = MagicMock()
    mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    # Mock config entry
    entry = MagicMock()
    entry.entry_id = "test_entry_calibration"
    entry.unique_id = "test_mac_calibration"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    # 1. Trigger entry setup
    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # 2. Test Button Setup
    async_add_entities = MagicMock()
    await sesame_ble.button.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    buttons = async_add_entities.call_args[0][0]
    assert len(buttons) == 3

    locked_btn = next(b for b in buttons if b.unique_id == "test_mac_calibration_set_locked_position")
    unlocked_btn = next(b for b in buttons if b.unique_id == "test_mac_calibration_set_unlocked_position")
    calibrate_btn = next(b for b in buttons if b.unique_id == "test_mac_calibration_calibrate_magnet")

    assert locked_btn.name == "Set Locked Position"
    assert unlocked_btn.name == "Set Unlocked Position"
    assert calibrate_btn.name == "Calibrate Magnet"

    # Test Calibrate Magnet Button Press
    wrapper.device.is_logged_in = True
    wrapper.device.calibrate_magnet = AsyncMock()
    await calibrate_btn.async_press()
    wrapper.device.calibrate_magnet.assert_called_once()

    # 3. Test Button Availability
    wrapper.device.is_logged_in = False
    assert locked_btn.available is False

    wrapper.device.is_logged_in = True
    assert locked_btn.available is True

    # 4. Mock device angles and trigger configure_lock_position
    wrapper.device.current_angle = 120
    wrapper.device.unlock_position = 500
    wrapper.device.configure_lock_position = AsyncMock()

    # Press Set Locked Position Button
    await locked_btn.async_press()
    wrapper.device.configure_lock_position.assert_called_once_with(120, 500)

    # Press Set Unlocked Position Button
    wrapper.device.current_angle = 450
    wrapper.device.lock_position = 100
    wrapper.device.configure_lock_position = AsyncMock()

    await unlocked_btn.async_press()
    wrapper.device.configure_lock_position.assert_called_once_with(100, 450)


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_calibration_button_exceptions(mock_bluetooth) -> None:
    """Tests error handling for button press when offline or current angle is unknown."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_exceptions"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
    }

    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    async_add_entities = MagicMock()
    await sesame_ble.button.async_setup_entry(hass, entry, async_add_entities)
    buttons = async_add_entities.call_args[0][0]
    locked_btn = buttons[0]

    # 1. Error: Not logged in
    wrapper.device.is_logged_in = False
    with pytest.raises(HomeAssistantError, match="Device is not connected"):
        await locked_btn.async_press()

    # 2. Error: Current angle is unknown
    wrapper.device.is_logged_in = True
    wrapper.device.current_angle = None
    with pytest.raises(HomeAssistantError, match="Current position/angle is unknown"):
        await locked_btn.async_press()


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_lock_entity_calibration_attributes(mock_bluetooth) -> None:
    """Tests that lock entity exposes calibration parameters in its extra state attributes."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_attrs"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
    }

    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # Setup Lock Entity
    async_add_entities = MagicMock()
    await sesame_ble.lock.async_setup_entry(hass, entry, async_add_entities)
    lock_entity = async_add_entities.call_args[0][0][0]

    # Populate angles
    wrapper.device.current_angle = 123
    wrapper.device.lock_position = 100
    wrapper.device.unlock_position = 500

    attrs = lock_entity.extra_state_attributes
    assert attrs["current_angle"] == 123
    assert attrs["lock_position"] == 100
    assert attrs["unlock_position"] == 500


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_touch_device_skips_buttons(mock_bluetooth) -> None:
    """Verify button platform setup is skipped on Touch/Keypad devices."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 10, 1, TEST_UUID.bytes) # Model 10 = SESAME_TOUCH
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_touch"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
    }

    await sesame_ble.async_setup_entry(hass, entry)

    async_add_entities = MagicMock()
    await sesame_ble.button.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_not_called()


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_diagnostic_angle_sensors(mock_bluetooth) -> None:
    """Tests that lock diagnostic angle sensors are registered and show correct values."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_sensors"
    entry.unique_id = "test_mac_sensors"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
    }

    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # Setup Sensor Platform
    async_add_entities = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]

    # Verify battery sensor + 3 angle sensors + RSSI + Connection
    assert len(entities) == 6

    locked_sensor = next(s for s in entities if s.unique_id == "test_mac_sensors_locked_position")
    unlocked_sensor = next(s for s in entities if s.unique_id == "test_mac_sensors_unlocked_position")
    current_sensor = next(s for s in entities if s.unique_id == "test_mac_sensors_current_angle")



    # Populate angles on wrapper device
    wrapper.device.lock_position = 180
    wrapper.device.unlock_position = 90
    wrapper.device.current_angle = 120

    assert locked_sensor.native_value == 180
    assert unlocked_sensor.native_value == 90
    assert current_sensor.native_value == 120

    assert locked_sensor._attr_native_unit_of_measurement == "°"
    assert unlocked_sensor._attr_native_unit_of_measurement == "°"
    assert current_sensor._attr_native_unit_of_measurement == "°"

    assert locked_sensor._attr_icon == "mdi:lock"
    assert unlocked_sensor._attr_icon == "mdi:lock-open"
    assert current_sensor._attr_icon == "mdi:rotate-right"


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_auto_lock_number_entity(mock_bluetooth) -> None:
    """Tests that auto lock seconds configuration number entity sets up correctly and sets value."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_number"
    entry.unique_id = "test_mac_number"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
    }

    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # Setup Number Platform
    async_add_entities = MagicMock()
    await sesame_ble.number.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 2
    num_entity = next(e for e in entities if e.unique_id == "test_mac_number_auto_lock_delay")
    ops_entity = next(e for e in entities if e.unique_id == "test_mac_number_ops_auto_lock_delay")

    assert num_entity.name == "Auto Lock Delay"
    assert num_entity._attr_native_unit_of_measurement == "s"
    assert ops_entity.name == "OpenSensor Auto Lock Delay"
    assert ops_entity._attr_native_unit_of_measurement == "s"

    # Test Value
    wrapper.device.auto_lock_second = 15
    assert num_entity.native_value == 15.0

    # Test Set Value
    wrapper.device.is_logged_in = True
    wrapper.device.set_auto_lock_second = AsyncMock()

    await num_entity.async_set_native_value(30.0)
    wrapper.device.set_auto_lock_second.assert_called_once_with(30)

    # Test OpenSensor Value
    wrapper.device.ops_lock_second = 30
    assert ops_entity.native_value == 30.0

    # Test Set OpenSensor Value
    wrapper.device.is_logged_in = True
    wrapper.device.set_ops_lock_second = AsyncMock()

    await ops_entity.async_set_native_value(60.0)
    wrapper.device.set_ops_lock_second.assert_called_once_with(60)


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_touch_device_skips_number(mock_bluetooth) -> None:
    """Verify number platform setup is skipped on Touch/Keypad devices."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 10, 1, TEST_UUID.bytes) # Model 10 = SESAME_TOUCH
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_touch_num"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
    }

    await sesame_ble.async_setup_entry(hass, entry)

    async_add_entities = MagicMock()
    await sesame_ble.number.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_not_called()


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_keypad_paired_locks_sensor_and_services(mock_bluetooth) -> None:
    """Verify that Touch keypads register the paired locks sensor and that the pair/unpair services work."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    # Mock devices
    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 10, 1, TEST_UUID.bytes) # Model 10 = SESAME_TOUCH
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_keypad_services"
    entry.unique_id = "test_mac_keypad_services"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
    }

    # Setup config entry and register views/services
    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # 1. Verify Sensor Setup
    async_add_entities = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    
    # 7 sensors for keypad: Battery + Card + Fingerprint + Passcode + Paired Locks + RSSI + Connection = 7 entities!
    assert len(entities) == 7



    paired_sensor = next(s for s in entities if s.unique_id == "test_mac_keypad_services_paired_locks")
    assert paired_sensor.name == "Paired Locks"
    assert paired_sensor._attr_icon == "mdi:lock-link"

    # Populate paired locks
    wrapper.device.paired_locks = [
        {"uuid": "00000000-0000-0000-0000-000000000001", "status": 1}
    ]
    assert paired_sensor.native_value == 1
    assert paired_sensor.extra_state_attributes["paired_locks"] == wrapper.device.paired_locks

    # 2. Test Services Registration
    assert hass.services.async_register.call_count >= 5 # add, delete, update passcode, pair_lock, unpair_lock
    
    # Find service calls registered
    registered_services = {}
    for call_args in hass.services.async_register.call_args_list:
        domain = call_args[0][0]
        service_name = call_args[0][1]
        handler = call_args[0][2]
        if domain == DOMAIN:
            registered_services[service_name] = handler

    assert "pair_lock" in registered_services
    assert "unpair_lock" in registered_services

    # 3. Test Service Invocations
    # Set up mocks for device registry and wrappers
    mock_dev_reg = MagicMock()
    mock_dev_reg.async_get = MagicMock()
    
    # Keypad device entry
    mock_keypad_dev = MagicMock()
    mock_keypad_dev.config_entries = [entry.entry_id]
    
    # Lock device entry
    mock_lock_entry = MagicMock()
    mock_lock_entry.entry_id = "test_entry_lock_id"
    mock_lock_entry.unique_id = "test_mac_lock_unique"
    mock_lock_entry.adv_data = MagicMock()
    mock_lock_entry.adv_data.device_uuid = TEST_UUID
    mock_lock_entry.secret_key = "0123456789abcdef0123456789abcdef"
    
    mock_lock_dev = MagicMock()
    mock_lock_dev.config_entries = [mock_lock_entry.entry_id]

    hass.data[DOMAIN][mock_lock_entry.entry_id] = mock_lock_entry

    def side_effect(dev_id):
        if dev_id == "keypad_dev_id":
            return mock_keypad_dev
        if dev_id == "lock_dev_id":
            return mock_lock_dev
        return None
        
    mock_dev_reg.async_get.side_effect = side_effect

    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg, create=True):
        # Call pair_lock service
        pair_call = MagicMock()
        pair_call.data = {
            "device_id": "keypad_dev_id",
            "lock_device_id": "lock_dev_id",
        }
        
        wrapper.device.is_logged_in = True
        wrapper.device.add_paired_lock = AsyncMock()
        
        await registered_services["pair_lock"](pair_call)
        wrapper.device.add_paired_lock.assert_called_once_with(TEST_UUID, bytes.fromhex("0123456789abcdef0123456789abcdef"))

        # Call unpair_lock service
        unpair_call = MagicMock()
        unpair_call.data = {
            "device_id": "keypad_dev_id",
            "lock_uuid": str(TEST_UUID),
        }
        wrapper.device.remove_paired_lock = AsyncMock()
        
        await registered_services["unpair_lock"](unpair_call)
        wrapper.device.remove_paired_lock.assert_called_once_with(TEST_UUID)


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_keypad_pairing_select_entities(mock_bluetooth) -> None:
    """Verify select entities setup, options generation, and select action for lock pairing."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    # Keypad Device
    mock_ble_device = MagicMock()
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
    mfg_data = struct.pack("<HB16s", 10, 1, TEST_UUID.bytes) # Model 10 = SESAME_TOUCH
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_keypad_select"
    entry.unique_id = "test_mac_keypad_select"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
    }

    await sesame_ble.async_setup_entry(hass, entry)
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # Setup Select Platform
    async_add_entities = MagicMock()
    await sesame_ble.select.async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 2

    pair_select = next(s for s in entities if s.unique_id == "test_mac_keypad_select_pair_lock")
    unpair_select = next(s for s in entities if s.unique_id == "test_mac_keypad_select_unpair_lock")

    assert pair_select.name == "Pair Lock"
    assert pair_select._attr_icon == "mdi:lock-plus"
    assert unpair_select.name == "Unpair Lock"
    assert unpair_select._attr_icon == "mdi:lock-minus"

    # Setup configured locks in HA to test options
    # Lock 1: Unpaired
    mock_lock_wrapper = MagicMock()
    mock_lock_wrapper.model_name = "SESAME5"
    mock_lock_wrapper.entry.title = "Front Door Lock"
    mock_lock_wrapper.adv_data.device_uuid = UUID("00000000-0000-0000-0000-000000000002")
    mock_lock_wrapper.secret_key = "0102030405060708090a0b0c0d0e0f10"
    
    # Lock 2: Already Paired
    mock_paired_lock_wrapper = MagicMock()
    mock_paired_lock_wrapper.model_name = "SESAME5"
    mock_paired_lock_wrapper.entry.title = "Back Door Lock"
    mock_paired_lock_wrapper.adv_data.device_uuid = UUID("00000000-0000-0000-0000-000000000003")

    hass.data[DOMAIN]["lock_unpaired"] = mock_lock_wrapper
    hass.data[DOMAIN]["lock_paired"] = mock_paired_lock_wrapper

    wrapper.device.is_logged_in = True
    wrapper.device.paired_locks = [
        {"uuid": "00000000-0000-0000-0000-000000000003", "status": 1}
    ]

    # Test pair_select options
    assert "Front Door Lock" in pair_select.options
    assert "Back Door Lock" not in pair_select.options

    # Test unpair_select options
    assert "Back Door Lock" in unpair_select.options
    assert "Front Door Lock" not in unpair_select.options

    # Test Pair lock execution
    wrapper.device.add_paired_lock = AsyncMock()
    await pair_select.async_select_option("Front Door Lock")
    wrapper.device.add_paired_lock.assert_called_once_with(
        UUID("00000000-0000-0000-0000-000000000002"),
        bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
    )

    # Test Unpair lock execution
    wrapper.device.remove_paired_lock = AsyncMock()
    await unpair_select.async_select_option("Back Door Lock")
    wrapper.device.remove_paired_lock.assert_called_once_with(
        UUID("00000000-0000-0000-0000-000000000003")
    )
