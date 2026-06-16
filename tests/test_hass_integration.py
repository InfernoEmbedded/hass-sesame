import pytest
from unittest.mock import MagicMock, AsyncMock, patch, ANY
import struct
from uuid import UUID

# Import custom component hooks
import sesame_ble
import sesame_ble.lock
import sesame_ble.sensor
from sesame_ble.const import DOMAIN, CONF_SECRET_KEY, CONF_MODEL, CONF_DEVICE_UUID
from sesame_ble.sesame_client import COMPANY_ID, ProductModels, SesameAdData

TEST_UUID = UUID("01234567-89ab-cdef-0123-456789abcdef")


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_lock_setup_and_entities(mock_bluetooth) -> None:
    """Tests entry setup and entities creation for a Sesame Lock device (e.g. SESAME5)."""
    # 1. Setup mocks
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
    entry.entry_id = "test_entry_lock"
    entry.unique_id = "test_mac_lock"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    # 2. Trigger entry setup
    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True
    assert entry.entry_id in hass.data[DOMAIN]
    
    wrapper = hass.data[DOMAIN][entry.entry_id]
    assert wrapper.model_name == "SESAME5"
    assert wrapper.device is not None

    # 3. Test Lock Entity Setup
    async_add_entities = MagicMock()
    await sesame_ble.lock.async_setup_entry(hass, entry, async_add_entities)
    
    async_add_entities.assert_called_once()
    entities = async_add_entities.call_args[0][0]
    assert len(entities) == 1
    
    lock_entity = entities[0]
    assert lock_entity.name is None
    assert lock_entity.unique_id == "test_mac_lock_lock"
    
    # 4. Test Lock Entity State & Methods (exercises our dr.CONNECTION_BLUETOOTH fix!)
    wrapper.device.is_logged_in = True
    wrapper.device.is_locked = True
    
    assert lock_entity.available is True
    assert lock_entity.is_locked is True
    
    dev_info = lock_entity.device_info
    assert dev_info is not None
    assert dev_info["model"] == "SESAME5"
    assert dev_info["identifiers"] == {(DOMAIN, "test_mac_lock")}
    assert dev_info["connections"] == {("bluetooth", "AA:BB:CC:DD:EE:FF")}

    # Mock lock/unlock execution
    wrapper.device.lock = AsyncMock()
    wrapper.device.unlock = AsyncMock()
    
    await lock_entity.async_lock()
    wrapper.device.lock.assert_awaited_once_with(history_name="Home Assistant")
    
    await lock_entity.async_unlock()
    wrapper.device.unlock.assert_awaited_once_with(history_name="Home Assistant")

    # 5. Test Sensor Entity Setup (Battery sensor)
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 1
    
    battery_sensor = sensors[0]
    assert battery_sensor.name == "Battery"
    assert battery_sensor.unique_id == "test_mac_lock_battery"
    
    wrapper.device.battery_percentage = 95
    assert battery_sensor.native_value == 95


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_keypad_setup_and_entities(mock_bluetooth) -> None:
    """Tests entry setup and entities creation for a Sesame Touch keypad."""
    # 1. Setup mocks
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    # Mock bluetooth discovery
    mock_ble_device = MagicMock()
    mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    mfg_data = struct.pack("<HB16s", 10, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    # Mock config entry
    entry = MagicMock()
    entry.entry_id = "test_entry_keypad"
    entry.unique_id = "test_mac_keypad"
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    # 2. Setup entry
    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True
    
    wrapper = hass.data[DOMAIN][entry.entry_id]
    assert wrapper.model_name == "SESAME_TOUCH"

    # 3. Keypads should not add lock entities
    async_add_locks = MagicMock()
    await sesame_ble.lock.async_setup_entry(hass, entry, async_add_locks)
    async_add_locks.assert_not_called()

    # 4. Keypads should add battery + cards + fingerprints + passcodes sensors
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 4
    
    battery_sensor = sensors[0]
    card_sensor = sensors[1]
    fp_sensor = sensors[2]
    pw_sensor = sensors[3]

    assert battery_sensor.name == "Battery"
    assert battery_sensor.unique_id == "test_mac_keypad_battery"
    assert card_sensor.name == "Registered Cards"
    assert card_sensor.unique_id == "test_mac_keypad_registered_cards"
    assert fp_sensor.name == "Registered Fingerprints"
    assert fp_sensor.unique_id == "test_mac_keypad_registered_fingerprints"
    assert pw_sensor.name == "Registered Passcodes"
    assert pw_sensor.unique_id == "test_mac_keypad_registered_passcodes"

    # Set mock statuses
    wrapper.device.is_logged_in = True
    wrapper.device.battery_percentage = 80
    wrapper.device.cards_count = 2
    wrapper.device.fingerprints_count = 7
    wrapper.device.passcodes_count = 4

    assert battery_sensor.native_value == 80
    assert card_sensor.native_value == 2
    assert fp_sensor.native_value == 7
    assert pw_sensor.native_value == 4


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_touch_2_pro_setup_and_entities(mock_bluetooth) -> None:
    """Tests entry setup and entities creation for a Sesame Touch 2 Pro keypad (Model 26)."""
    # 1. Setup mocks
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    # Mock bluetooth discovery
    mock_ble_device = MagicMock()
    mock_ble_device.address = "DE:34:B7:06:2E:56"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    # Model ID 26 (SESAME_TOUCH_2_PRO)
    mfg_data = struct.pack("<HB16s", 26, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    # Mock config entry
    entry = MagicMock()
    entry.entry_id = "test_entry_touch_2_pro"
    entry.unique_id = "test_mac_touch_2_pro"
    entry.data = {
        "mac_address": "DE:34:B7:06:2E:56",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_TOUCH_2_PRO",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    # 2. Setup entry
    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True
    
    wrapper = hass.data[DOMAIN][entry.entry_id]
    assert wrapper.model_name == "SESAME_TOUCH_2_PRO"

    # 3. Keypads should not add lock entities
    async_add_locks = MagicMock()
    await sesame_ble.lock.async_setup_entry(hass, entry, async_add_locks)
    async_add_locks.assert_not_called()

    # 4. Keypads should add battery + cards + fingerprints + passcodes sensors
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 4
    
    battery_sensor = sensors[0]
    card_sensor = sensors[1]
    fp_sensor = sensors[2]
    pw_sensor = sensors[3]

    assert battery_sensor.name == "Battery"
    assert battery_sensor.unique_id == "test_mac_touch_2_pro_battery"
    assert card_sensor.name == "Registered Cards"
    assert card_sensor.unique_id == "test_mac_touch_2_pro_registered_cards"
    assert fp_sensor.name == "Registered Fingerprints"
    assert fp_sensor.unique_id == "test_mac_touch_2_pro_registered_fingerprints"
    assert pw_sensor.name == "Registered Passcodes"
    assert pw_sensor.unique_id == "test_mac_touch_2_pro_registered_passcodes"


class MockAdvertisementData:
    """Helper to mock advertisement data payload."""
    def __init__(self, mfg_data: bytes) -> None:
        self.manufacturer_data = {COMPANY_ID: mfg_data}
