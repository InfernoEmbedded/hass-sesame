import pytest
from unittest.mock import MagicMock, AsyncMock, patch, ANY
import struct
from uuid import UUID

# Import custom component hooks
import sesame_ble
import sesame_ble.lock
import sesame_ble.sensor
import sesame_ble.binary_sensor
import sesame_ble.button
import sesame_ble.number

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

    # 5. Test Sensor Entity Setup (Battery sensor and diagnostic angle sensors)
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 6



    
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

    # 4. Keypads should add battery + cards + fingerprints + passcodes + paired locks + RSSI + Connection sensors
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 7




    
    battery_sensor = sensors[0]
    card_sensor = sensors[1]
    fp_sensor = sensors[2]
    pw_sensor = sensors[3]
    paired_sensor = sensors[4]

    assert battery_sensor.name == "Battery"
    assert battery_sensor.unique_id == "test_mac_keypad_battery"
    assert card_sensor.name == "Registered Cards"
    assert card_sensor.unique_id == "test_mac_keypad_registered_cards"
    assert fp_sensor.name == "Registered Fingerprints"
    assert fp_sensor.unique_id == "test_mac_keypad_registered_fingerprints"
    assert pw_sensor.name == "Registered Passcodes"
    assert pw_sensor.unique_id == "test_mac_keypad_registered_passcodes"
    assert paired_sensor.name == "Paired Locks"
    assert paired_sensor.unique_id == "test_mac_keypad_paired_locks"

    # Set mock statuses
    wrapper.device.is_logged_in = True
    wrapper.device.battery_percentage = 80
    wrapper.device.cards_count = 2
    wrapper.device.fingerprints_count = 7
    wrapper.device.passcodes_count = 4
    wrapper.device.paired_locks = [{"uuid": "test_uuid", "status": 1}]

    assert battery_sensor.native_value == 80
    assert card_sensor.native_value == 2
    assert fp_sensor.native_value == 7
    assert pw_sensor.native_value == 4
    assert paired_sensor.native_value == 1


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

    # 4. Keypads should add battery + cards + fingerprints + passcodes + paired locks + RSSI + Connection sensors
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]
    assert len(sensors) == 7




    
    battery_sensor = sensors[0]
    card_sensor = sensors[1]
    fp_sensor = sensors[2]
    pw_sensor = sensors[3]
    paired_sensor = sensors[4]

    assert battery_sensor.name == "Battery"
    assert battery_sensor.unique_id == "test_mac_touch_2_pro_battery"
    assert card_sensor.name == "Registered Cards"
    assert card_sensor.unique_id == "test_mac_touch_2_pro_registered_cards"
    assert fp_sensor.name == "Registered Fingerprints"
    assert fp_sensor.unique_id == "test_mac_touch_2_pro_registered_fingerprints"
    assert pw_sensor.name == "Registered Passcodes"
    assert pw_sensor.unique_id == "test_mac_touch_2_pro_registered_passcodes"


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_entry_unique_id_migration(mock_bluetooth) -> None:
    """Tests that a config entry with no unique ID is automatically migrated during setup."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_update_entry = MagicMock()

    # Mock bluetooth discovery
    mock_ble_device = MagicMock()
    mock_ble_device.address = "AA:BB:CC:DD:EE:FF"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    # Mock config entry with None unique_id
    entry = MagicMock()
    entry.entry_id = "test_entry_migration"
    entry.unique_id = None
    entry.data = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is False

    # Assert that async_update_entry was called to set the unique_id
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry, unique_id="AA:BB:CC:DD:EE:FF"
    )


class MockAdvertisementData:
    """Helper to mock advertisement data payload."""
    def __init__(self, mfg_data: bytes) -> None:
        self.manufacturer_data = {COMPANY_ID: mfg_data}


import datetime
from sesame_ble import BaseKeypad
from sesame_ble.sesame_client import SesameKeypad
from sesame_ble.views import SesamePasscodesView, SesameCardsView, SesameFingerprintsView


def test_base_keypad_inheritance():
    """Verify that SesameKeypad inherits from BaseKeypad."""
    assert issubclass(SesameKeypad, BaseKeypad)


@pytest.mark.asyncio
async def test_scheduler_sync_and_apply_schedules():
    """Test background loop passcode schedule evaluation."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_wrapper_entry"

    # Define a MockKeypad class that inherits from BaseKeypad
    class MockKeypad(BaseKeypad):
        passcodes = {}
        cards = {}
        fingerprints = {}

    # Mock device
    mock_device = MagicMock(spec=MockKeypad)
    mock_device.is_logged_in = True
    mock_device.mac_address = "AA:BB:CC:DD:EE:FF"
    mock_device.mech_status = None
    mock_device.passcodes = {
        "01020304": {"name": "Physically Active", "code": "1234"}
    }
    mock_device.cards = {}
    mock_device.fingerprints = {}
    mock_device.get_passcodes = AsyncMock()
    mock_device.get_cards = AsyncMock()
    mock_device.get_fingerprints = AsyncMock()
    mock_device.add_passcode = AsyncMock()
    mock_device.delete_passcode = AsyncMock()
    mock_device.apply_passcode_schedules = BaseKeypad.apply_passcode_schedules.__get__(
        mock_device, BaseKeypad
    )

    # Mock store
    mock_store = AsyncMock()

    # Setup wrapper using MagicMock but binding the real scheduler method
    wrapper = MagicMock()
    wrapper.device = mock_device
    wrapper.store = mock_store
    wrapper.logical_passcodes = {}
    wrapper.logical_cards = {}
    wrapper.logical_fingerprints = {}
    wrapper.recently_deleted_passcodes = {}
    wrapper.recently_deleted_cards = {}
    wrapper.recently_deleted_fingerprints = {}
    wrapper._handle_status_update = MagicMock()

    # Bind the actual method to the mock wrapper instance
    wrapper._sync_and_apply_schedules = sesame_ble.SesameDeviceWrapper._sync_and_apply_schedules.__get__(
        wrapper, sesame_ble.SesameDeviceWrapper
    )

    now = datetime.datetime.now()
    future = now + datetime.timedelta(hours=2)
    past = now - datetime.timedelta(hours=2)

    # 1. logical_passcodes setup
    # Active passcode (within schedule) - should be physically added if missing
    active_missing_uid = bytes([5, 6, 7, 8]).hex()
    # Expired passcode - should be physically removed if present
    expired_present_uid = "01020304"  # matches the physical passcode
    # Pending passcode - should not be physically active
    pending_uid = bytes([9, 10, 11, 12]).hex()

    wrapper.logical_passcodes = {
        active_missing_uid: {
            "name": "Active Missing",
            "code": "5678",
            "start": (now - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
            "end": (now + datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
        },
        expired_present_uid: {
            "name": "Physically Active",
            "code": "1234",
            "start": (past - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"),
            "end": (past - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
        },
        pending_uid: {
            "name": "Pending",
            "code": "9012",
            "start": (future + datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
            "end": (future + datetime.timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M"),
        },
    }

    # Run scheduler sync
    await wrapper._sync_and_apply_schedules()

    # Assertions
    # 1. Should fetch passcodes from physical device
    mock_device.get_passcodes.assert_awaited_once()

    # 2. Should add "Active Missing" (5678)
    mock_device.add_passcode.assert_awaited_once_with("5678", "Active Missing")

    # 3. Should delete "Physically Active" (1234) because schedule expired
    mock_device.delete_passcode.assert_awaited_once_with(expired_present_uid)

    # 4. Should save updated state to store
    mock_store.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_passcodes_view():
    """Test SesamePasscodesView GET, POST, DELETE requests."""
    import json
    import struct
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    entry_id = "test_keypad_entry"

    # Setup wrapper and device via async_setup_entry
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.unique_id = "test_mac_touch_2_pro"
    entry.data = {
        "mac_address": "DE:34:B7:06:2E:56",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_TOUCH_2_PRO",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    # Setup the mock bluetooth discovery so async_setup_entry works
    with patch("sesame_ble.bluetooth") as mock_bluetooth:
        mock_ble_device = MagicMock()
        mock_ble_device.address = "DE:34:B7:06:2E:56"
        mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
        
        mfg_data = struct.pack("<HB16s", 26, 1, TEST_UUID.bytes)
        mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)
        
        setup_ok = await sesame_ble.async_setup_entry(hass, entry)
        assert setup_ok is True

    wrapper = hass.data[DOMAIN][entry_id]

    mock_device = MagicMock()
    mock_device.is_connected = True
    mock_device.is_logged_in = True
    mock_device.passcodes = {}
    mock_device.cards = {"card1": {"name": "Front Door Card", "type": "card"}}
    mock_device.fingerprints = {"fp1": {"name": "Index Finger", "type": "finger"}}
    mock_device.get_passcodes = AsyncMock()
    mock_device.get_cards = AsyncMock()
    mock_device.get_fingerprints = AsyncMock()
    mock_device.scanned_card = None
    mock_device.scanned_fingerprint = None
    mock_device.paired_locks = []

    wrapper.device = mock_device
    wrapper.store = MagicMock()
    wrapper.store.async_save = AsyncMock()
    wrapper.logical_passcodes = {
        "01020304": {
            "name": "Guest PIN",
            "code": "1234",
            "start": "",
            "end": "",
        }
    }
    wrapper.logical_cards = {"card1": {"name": "Front Door Card", "type": "card"}}
    wrapper.logical_fingerprints = {"fp1": {"name": "Index Finger", "type": "finger"}}
    wrapper._sync_and_apply_schedules = AsyncMock()

    view = SesamePasscodesView(hass)

    # 1. Test GET request
    req_get = MagicMock()
    resp_get = await view.get(req_get)
    assert resp_get.status == 200

    data_get = json.loads(resp_get.body.decode("utf-8"))
    assert len(data_get["keypads"]) == 1
    keypad_info = data_get["keypads"][0]
    assert keypad_info["entry_id"] == entry_id
    assert keypad_info["is_logged_in"] is True
    assert len(keypad_info["passcodes"]) == 1
    assert keypad_info["passcodes"][0]["name"] == "Guest PIN"
    assert len(keypad_info["cards"]) == 1
    assert keypad_info["cards"][0]["name"] == "Front Door Card"

    # 2. Test POST request (Add/Update passcode)
    req_post = MagicMock()
    req_post.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "name": "New PIN",
            "code": "9876",
            "start": "2026-06-16 10:00",
            "end": "2026-06-16 12:00",
        }
    )
    resp_post = await view.post(req_post)
    assert resp_post.status == 200
    data_post = json.loads(resp_post.body.decode("utf-8"))
    assert data_post["success"] is True

    new_uid = bytes([9, 8, 7, 6]).hex()
    assert new_uid in wrapper.logical_passcodes
    assert wrapper.logical_passcodes[new_uid]["name"] == "New PIN"
    assert wrapper._sync_and_apply_schedules.call_count == 2

    # 3. Test DELETE request
    req_delete = MagicMock()
    req_delete.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "uid": new_uid,
        }
    )
    resp_delete = await view.delete(req_delete)
    assert resp_delete.status == 200
    data_delete = json.loads(resp_delete.body.decode("utf-8"))
    assert data_delete["success"] is True
    assert new_uid not in wrapper.logical_passcodes


@pytest.mark.asyncio
async def test_api_cards_and_fingerprints_views():
    """Test SesameCardsView and SesameFingerprintsView POST/DELETE requests."""
    hass = MagicMock()
    entry_id = "test_keypad_entry"

    mock_device = MagicMock()
    mock_device.update_card_name = AsyncMock()
    mock_device.delete_card = AsyncMock()
    mock_device.get_cards = AsyncMock()
    mock_device.update_fingerprint_name = AsyncMock()
    mock_device.delete_fingerprint = AsyncMock()
    mock_device.get_fingerprints = AsyncMock()
    mock_device.mech_status = None

    wrapper = MagicMock()
    wrapper.device = mock_device
    wrapper.logical_passcodes = {}
    wrapper.logical_cards = {"card_uid": {"name": "Old Card Name", "type": 0x80}}
    wrapper.logical_fingerprints = {"fp_uid": {"name": "Old FP Name", "type": 0x01}}
    wrapper.store = AsyncMock()
    wrapper.recently_deleted_cards = {}
    wrapper.recently_deleted_fingerprints = {}
    wrapper._sync_and_apply_schedules = AsyncMock()

    hass.data = {DOMAIN: {entry_id: wrapper}}

    # Cards View
    cards_view = SesameCardsView(hass)

    # 1. Rename card
    req_rename_card = MagicMock()
    req_rename_card.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "uid": "card_uid",
            "name": "New Card Name",
        }
    )
    resp = await cards_view.post(req_rename_card)
    assert resp.status == 200
    mock_device.update_card_name.assert_awaited_once_with("card_uid", "New Card Name")
    mock_device.get_cards.assert_awaited_once()

    # 2. Delete card
    req_delete_card = MagicMock()
    req_delete_card.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "uid": "card_uid",
        }
    )
    resp = await cards_view.delete(req_delete_card)
    assert resp.status == 200
    mock_device.delete_card.assert_awaited_once_with("card_uid")

    # Fingerprints View
    fp_view = SesameFingerprintsView(hass)

    # 3. Rename fingerprint
    req_rename_fp = MagicMock()
    req_rename_fp.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "uid": "fp_uid",
            "name": "New FP Name",
        }
    )
    resp = await fp_view.post(req_rename_fp)
    assert resp.status == 200
    mock_device.update_fingerprint_name.assert_awaited_once_with("fp_uid", "New FP Name")
    mock_device.get_fingerprints.assert_awaited_once()

    # 4. Delete fingerprint
    req_delete_fp = MagicMock()
    req_delete_fp.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "uid": "fp_uid",
        }
    )
    resp_delete_fp = await fp_view.delete(req_delete_fp)
    assert resp_delete_fp.status == 200
    mock_device.delete_fingerprint.assert_awaited_once_with("fp_uid")


@pytest.mark.asyncio
async def test_api_pairing_and_history_views():
    """Test SesameLockHistoryView, SesameKeypadPairView, and SesameKeypadUnpairView."""
    import json
    from uuid import UUID
    import sesame_ble
    hass = MagicMock()
    entry_id = "test_keypad_entry"
    lock_entry_id = "test_lock_entry"

    # Set up keypad device mock
    mock_keypad = MagicMock()
    mock_keypad.is_connected = True
    mock_keypad.is_logged_in = True
    mock_keypad.add_paired_lock = AsyncMock()
    mock_keypad.remove_paired_lock = AsyncMock()
    mock_keypad.paired_locks = [
        {"uuid": "11200509-0108-0802-b700-6500ffffffff", "status": 4}
    ]

    keypad_wrapper = MagicMock()
    keypad_wrapper.model_name = "SESAME_TOUCH"
    keypad_wrapper.device = mock_keypad
    keypad_wrapper.entry.title = "Test Keypad"
    keypad_wrapper.logical_cards = {}
    keypad_wrapper.logical_fingerprints = {}
    keypad_wrapper.logical_passcodes = {}

    # Set up lock device mock
    mock_lock = MagicMock()
    mock_lock.is_connected = True
    mock_lock.is_logged_in = True
    mock_lock.fetch_and_flush_history = AsyncMock()

    lock_wrapper = MagicMock()
    lock_wrapper.hass = hass
    lock_wrapper.device = mock_lock
    lock_wrapper.fetch_and_flush_history = AsyncMock()
    lock_wrapper.model_name = "SESAME5"
    lock_wrapper.entry.title = "Test Lock"
    lock_wrapper.secret_key = "12345678901234567890123456789012"
    
    # Mock adv_data
    mock_adv = MagicMock()
    mock_adv.device_uuid = UUID("11200509-0108-0802-b700-6500ffffffff")
    lock_wrapper.adv_data = mock_adv

    # Bind resolve_history_record
    lock_wrapper.resolve_history_record = sesame_ble.SesameDeviceWrapper.resolve_history_record.__get__(
        lock_wrapper, sesame_ble.SesameDeviceWrapper
    )
    
    lock_wrapper.history_records = [
        {
            "record_id": 1,
            "type": 3,
            "timestamp": 1625097600,
            "tag": 2,
            "raw_parameter": "01020304"
        }
    ]

    # Register wrappers
    hass.data = {
        DOMAIN: {
            entry_id: keypad_wrapper,
            lock_entry_id: lock_wrapper
        }
    }

    # 1. Test SesameLockHistoryView
    from sesame_ble.views import SesameLockHistoryView, SesameKeypadPairView, SesameKeypadUnpairView
    history_view = SesameLockHistoryView(hass)
    req_history = MagicMock()
    req_history.query = {"entry_id": entry_id}
    
    # Mock _find_paired_lock helper to return our mock lock_wrapper
    with patch.object(history_view, "_find_paired_lock", return_value=lock_wrapper):
        resp_history = await history_view.get(req_history)
        assert resp_history.status == 200
        data_hist = json.loads(resp_history.body.decode("utf-8"))
        assert data_hist["lock_name"] == "Test Lock"
        assert len(data_hist["history"]) == 1
        assert data_hist["history"][0]["record_id"] == 1
        assert data_hist["history"][0]["method"] == "Passcode"

    # 2. Test SesameKeypadPairView
    pair_view = SesameKeypadPairView(hass)
    req_pair = MagicMock()
    req_pair.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "lock_entry_id": lock_entry_id
        }
    )
    resp_pair = await pair_view.post(req_pair)
    assert resp_pair.status == 200
    mock_keypad.add_paired_lock.assert_awaited_once()

    # 3. Test SesameKeypadUnpairView
    unpair_view = SesameKeypadUnpairView(hass)
    req_unpair = MagicMock()
    req_unpair.json = AsyncMock(
        return_value={
            "entry_id": entry_id,
            "lock_uuid": "11200509-0108-0802-b700-6500ffffffff"
        }
    )
    resp_unpair = await unpair_view.post(req_unpair)
    assert resp_unpair.status == 200
    mock_keypad.remove_paired_lock.assert_awaited_once_with(UUID("11200509-0108-0802-b700-6500ffffffff"))


@pytest.mark.asyncio
async def test_passcode_schedules_weekly_and_daily():
    """Test weekly days and daily time range schedule evaluations in BaseKeypad."""
    from sesame_ble.sesame_client.device import BaseKeypad
    import datetime

    class MockKeypad(BaseKeypad):
        passcodes = {}
        cards = {}
        fingerprints = {}

    keypad = MockKeypad()
    keypad.add_passcode = AsyncMock()
    keypad.delete_passcode = AsyncMock()

    # Current time is Thursday, 14:30
    fixed_now = datetime.datetime(2026, 6, 18, 14, 30) # Thursday is weekday 3

    with patch("datetime.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.datetime.fromisoformat
        mock_dt.strptime = datetime.datetime.strptime
        mock_dt.time = datetime.time

        # 1. Weekly schedule matches (Thursday=3)
        logical_passcodes = {
            "uid1": {
                "name": "Matching Weekly",
                "code": "1234",
                "days": [3], # Thursday
                "time_start": "14:00",
                "time_end": "15:00",
            },
            "uid2": {
                "name": "Mismatched Weekly",
                "code": "5678",
                "days": [0, 1, 2], # Mon, Tue, Wed
                "time_start": "14:00",
                "time_end": "15:00",
            },
            "uid3": {
                "name": "Mismatched Daily",
                "code": "9012",
                "days": [3],
                "time_start": "15:00",
                "time_end": "16:00",
            }
        }

        # Setup current physically active passcodes
        keypad.passcodes = {
            "uid2": {"name": "Mismatched Weekly", "code": "5678"},
        }

        # Run schedule evaluation
        changed = await keypad.apply_passcode_schedules(logical_passcodes)
        assert changed is True
        
        # Matches weekly should be added
        keypad.add_passcode.assert_awaited_once_with("1234", "Matching Weekly")
        # Mismatched weekly should be deleted
        keypad.delete_passcode.assert_awaited_once_with("uid2")


@pytest.mark.asyncio
async def test_otp_detection_and_deletion():
    """Test OTP passcode detection and deletion in fetch_and_flush_history."""
    import time
    import sesame_ble

    hass = MagicMock()
    # Mock states for Person
    hass.states.get.return_value = None
    
    # Pair keypad wrapper
    keypad_wrapper = MagicMock()
    keypad_wrapper.logical_passcodes = {
        "otp_uid": {
            "name": "OTP Code",
            "code": "1234",
            "one_time": True,
        }
    }
    keypad_wrapper.recently_deleted_passcodes = {}
    keypad_wrapper.device.delete_passcode = AsyncMock()
    keypad_wrapper._sync_and_apply_schedules = AsyncMock()

    # Lock wrapper
    lock_wrapper = MagicMock()
    lock_wrapper.hass = hass
    # Mock adv_data device UUID
    mock_uuid = UUID("11200509-0108-0802-b700-6500ffffffff")
    lock_wrapper.adv_data.device_uuid = mock_uuid
    lock_wrapper.device.fetch_and_flush_history = AsyncMock(return_value=[
        {
            "record_id": 10,
            "type": 11, # Keypad unlock
            "timestamp": int(time.time()),
            "tag": 2, # Passcode
            "raw_parameter": "otp_uid"
        }
    ])
    lock_wrapper.history_records = []
    lock_wrapper.resolve_history_record = sesame_ble.SesameDeviceWrapper.resolve_history_record.__get__(
        lock_wrapper, sesame_ble.SesameDeviceWrapper
    )

    # Register them in hass.data
    hass.data = {
        DOMAIN: {
            "keypad_entry": keypad_wrapper,
            "lock_entry": lock_wrapper,
        }
    }
    
    # Configure keypad paired lock mapping
    keypad_wrapper.device.paired_locks = [{"uuid": str(mock_uuid), "status": 4}]
    keypad_wrapper.model_name = "SESAME_TOUCH"

    # Bind and run fetch_and_flush_history
    lock_wrapper.fetch_and_flush_history = sesame_ble.SesameDeviceWrapper.fetch_and_flush_history.__get__(
        lock_wrapper, sesame_ble.SesameDeviceWrapper
    )
    
    await lock_wrapper.fetch_and_flush_history()

    # Verify that the OTP was deleted logically and physically
    assert "otp_uid" not in keypad_wrapper.logical_passcodes
    assert "otp_uid" in keypad_wrapper.recently_deleted_passcodes
    keypad_wrapper.device.delete_passcode.assert_awaited_once_with("otp_uid")


@pytest.mark.asyncio
async def test_fetch_and_flush_history_variable_lengths():
    """Test SesameDevice.fetch_and_flush_history with variable payload lengths."""
    from sesame_ble.sesame_client.device import SesameLock, ITEM_HISTORY, ITEM_HISTORY_DELETE
    
    mock_ble = MagicMock()
    mock_ble.address = "AA:BB:CC:DD:EE:FF"
    ad_data = MagicMock()
    
    device = SesameLock(mock_ble, ad_data, secret_key="0123456789abcdef0123456789abcdef")
    device.is_logged_in = True
    
    # 1. 16-byte payload (manual unlock event)
    payload_16 = struct.pack("<IBI7s", 100, 2, 1625097600, b"\x00" * 7)
    
    # 2. 48-byte payload (keypad passcode event)
    param_bytes_48 = struct.pack("<HB", 2, 7) + b"otp_uid" + b"\x00" * 22
    payload_48 = struct.pack("<IBI7s32s", 101, 11, 1625097700, b"\x00" * 7, param_bytes_48)
    
    # 3. 24-byte payload (variable custom event)
    param_bytes_24 = struct.pack("<HB", 0, 3) + b"nfc" + b"\x00" * 3
    payload_24 = struct.pack("<IBI7s8s", 102, 12, 1625097800, b"\x00" * 7, param_bytes_24)
    
    call_index = 0
    async def mock_send_command(item_code, payload, encrypt=True):
        nonlocal call_index
        if item_code == ITEM_HISTORY:
            assert payload == b"\x01"
            if call_index == 0:
                call_index += 1
                return payload_16
            elif call_index == 1:
                call_index += 1
                return payload_48
            elif call_index == 2:
                call_index += 1
                return payload_24
            else:
                raise Exception("Command error: 5")
        elif item_code == ITEM_HISTORY_DELETE:
            assert len(payload) == 4
            return b""
        raise Exception(f"Unexpected item code: {item_code}")
        
    device.send_command = mock_send_command
    
    records = await device.fetch_and_flush_history()
    
    assert len(records) == 3
    
    # Verify manual event (16 bytes)
    assert records[0]["record_id"] == 100
    assert records[0]["type"] == 2
    assert records[0]["timestamp"] == 1625097600
    assert records[0]["tag"] == 0
    assert records[0]["raw_parameter"] == ""
    
    # Verify 48-byte keypad passcode event
    assert records[1]["record_id"] == 101
    assert records[1]["type"] == 11
    assert records[1]["timestamp"] == 1625097700
    assert records[1]["tag"] == 2
    assert records[1]["raw_parameter"] == b"otp_uid".hex()
    
    # Verify 24-byte variable event
    assert records[2]["record_id"] == 102
    assert records[2]["type"] == 12
    assert records[2]["timestamp"] == 1625097800
    assert records[2]["tag"] == 0
    assert records[2]["raw_parameter"] == b"nfc".hex()


@pytest.mark.asyncio
async def test_person_name_resolution():
    """Test person entity resolution in SesameDeviceWrapper.resolve_history_record."""
    import sesame_ble

    hass = MagicMock()
    # Mock a Person state
    mock_person_state = MagicMock()
    mock_person_state.name = "Alice Smith"
    hass.states.get.return_value = mock_person_state

    # Keypad wrapper with credentials
    keypad_wrapper = MagicMock()
    keypad_wrapper.model_name = "SESAME_TOUCH"
    keypad_wrapper.logical_passcodes = {
        "pass_uid": {
            "name": "Private Passcode",
            "person_id": "person.alice",
        }
    }
    keypad_wrapper.device.paired_locks = [{"uuid": "11200509-0108-0802-b700-6500ffffffff", "status": 4}]

    lock_wrapper = MagicMock()
    lock_wrapper.hass = hass
    lock_wrapper.adv_data.device_uuid = UUID("11200509-0108-0802-b700-6500ffffffff")
    
    hass.data = {
        DOMAIN: {
            "keypad_entry": keypad_wrapper,
            "lock_entry": lock_wrapper,
        }
    }

    # Bind resolve_history_record
    lock_wrapper.resolve_history_record = sesame_ble.SesameDeviceWrapper.resolve_history_record.__get__(
        lock_wrapper, sesame_ble.SesameDeviceWrapper
    )

    resolved = lock_wrapper.resolve_history_record(
        record_id=12,
        history_type=12, # Unlock/Lock type
        timestamp=1625097600,
        tag=2, # Passcode
        raw_param="pass_uid"
    )

    assert resolved["person_id"] == "person.alice"
    assert resolved["caller"] == "Alice Smith"
    hass.states.get.assert_called_once_with("person.alice")


@pytest.mark.asyncio
async def test_rssi_and_connection_sensors():
    """Test SesameRSSISensor and SesameConnectionSensor states."""
    from sesame_ble.sensor import SesameRSSISensor, SesameConnectionSensor
    from homeassistant.components import bluetooth

    hass = MagicMock()
    wrapper = MagicMock()
    wrapper.hass = hass
    wrapper.ble_device.address = "AA:BB:CC:DD:EE:FF"
    wrapper.entry.unique_id = "test_unique_id"
    wrapper.device.is_connected = True
    wrapper.device.is_logged_in = True

    # 1. RSSI sensor
    rssi_sensor = SesameRSSISensor(wrapper)
    assert rssi_sensor.unique_id == "test_unique_id_rssi"
    assert rssi_sensor.available is True

    # Mock bluetooth service info
    mock_service_info = MagicMock()
    mock_service_info.rssi = -75
    
    with patch("homeassistant.components.bluetooth.async_last_service_info", return_value=mock_service_info) as mock_last_info:
        assert rssi_sensor.native_value == -75
        mock_last_info.assert_called_once_with(hass, "AA:BB:CC:DD:EE:FF")

    # 2. Connection sensor
    conn_sensor = SesameConnectionSensor(wrapper)
    assert conn_sensor.unique_id == "test_unique_id_connection_state"
    assert conn_sensor.available is True
    assert conn_sensor.native_value == "connected"
    assert conn_sensor.extra_state_attributes["is_connected"] is True
    assert conn_sensor.extra_state_attributes["is_logged_in"] is True

    # Test disconnected state
    wrapper.device.is_connected = False
    assert conn_sensor.native_value == "disconnected"


@pytest.mark.asyncio
async def test_add_passcode_service():
    """Test handle_add_passcode service call with constraints and OTP parameters."""
    import struct
    from uuid import UUID
    import sesame_ble
    from homeassistant.core import ServiceCall

    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    # Mock services.async_register
    hass.services.async_register = MagicMock()

    entry_id = "test_keypad_entry"

    entry = MagicMock()
    entry.entry_id = entry_id
    entry.unique_id = "test_mac_touch_2_pro"
    entry.data = {
        "mac_address": "DE:34:B7:06:2E:56",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_TOUCH_2_PRO",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    with patch("sesame_ble.bluetooth") as mock_bluetooth:
        mock_ble_device = MagicMock()
        mock_ble_device.address = "DE:34:B7:06:2E:56"
        mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
        
        mfg_data = struct.pack("<HB16s", 26, 1, TEST_UUID.bytes)
        mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)
        
        setup_ok = await sesame_ble.async_setup_entry(hass, entry)
        assert setup_ok is True

    wrapper = hass.data[DOMAIN][entry_id]

    mock_device = MagicMock()
    mock_device.is_connected = True
    mock_device.is_logged_in = True
    mock_device.passcodes = {}
    mock_device.update_passcode_name = AsyncMock()
    mock_device.mech_status = None
    
    wrapper.device = mock_device
    wrapper.store = AsyncMock()
    wrapper.logical_passcodes = {}
    wrapper._sync_and_apply_schedules = AsyncMock()

    # Extract the registered add_passcode service handler
    service_register_calls = hass.services.async_register.call_args_list
    add_passcode_handler = None
    for call_args in service_register_calls:
        if call_args[0][1] == "add_passcode":
            add_passcode_handler = call_args[0][2]
            break
            
    assert add_passcode_handler is not None

    # Mock device registry
    mock_dev_reg = MagicMock()
    mock_device_entry = MagicMock()
    mock_device_entry.config_entries = [entry_id]
    mock_dev_reg.async_get.return_value = mock_device_entry
    
    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg, create=True):
        service_call = MagicMock()
        service_call.data = {
            "device_id": "some_device_id",
            "passcode": "8888",
            "name": "Service Temp User",
            "start": "2026-06-20 08:00",
            "end": "2026-06-20 18:00",
            "days": ["0", "1", "2"],
            "time_start": "09:00",
            "time_end": "17:00",
            "one_time": True,
            "person_id": "person.guest",
        }
        await add_passcode_handler(service_call)

        uid = bytes([8, 8, 8, 8]).hex()
        assert uid in wrapper.logical_passcodes
        logical_info = wrapper.logical_passcodes[uid]
        assert logical_info["name"] == "Service Temp User"
        assert logical_info["code"] == "8888"
        assert logical_info["start"] == "2026-06-20 08:00"
        assert logical_info["end"] == "2026-06-20 18:00"
        assert logical_info["days"] == [0, 1, 2]
        assert logical_info["time_start"] == "09:00"
        assert logical_info["time_end"] == "17:00"
        assert logical_info["one_time"] is True
        assert logical_info["person_id"] == "person.guest"


@pytest.mark.asyncio
async def test_passcode_service_entity_id_resolution():
    """Test handle_add_passcode and delete_passcode using entity IDs (keypad sensor and paired lock)."""
    import struct
    from uuid import UUID
    import sesame_ble
    from homeassistant.core import ServiceCall

    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.services.async_register = MagicMock()

    keypad_entry_id = "test_keypad_entry_id"
    lock_entry_id = "test_lock_entry_id"
    lock_uuid = UUID("11111111-2222-3333-4444-555555555555")

    # Set up keypad entry
    keypad_entry = MagicMock()
    keypad_entry.entry_id = keypad_entry_id
    keypad_entry.unique_id = "keypad_mac"
    keypad_entry.data = {
        "mac_address": "DE:34:B7:06:2E:56",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_TOUCH_2_PRO",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    with patch("sesame_ble.bluetooth") as mock_bluetooth:
        mock_ble_device = MagicMock()
        mock_ble_device.address = "DE:34:B7:06:2E:56"
        mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device
        mfg_data = struct.pack("<HB16s", 26, 1, TEST_UUID.bytes)
        mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)
        await sesame_ble.async_setup_entry(hass, keypad_entry)

    keypad_wrapper = hass.data[DOMAIN][keypad_entry_id]
    mock_keypad_dev = MagicMock()
    mock_keypad_dev.is_connected = True
    mock_keypad_dev.is_logged_in = True
    mock_keypad_dev.passcodes = {}
    mock_keypad_dev.paired_locks = [{"uuid": str(lock_uuid)}]
    mock_keypad_dev.update_passcode_name = AsyncMock()
    mock_keypad_dev.delete_passcode = AsyncMock()
    mock_keypad_dev.get_passcodes = AsyncMock()
    mock_keypad_dev._resolve_code = MagicMock(return_value=bytes([1, 2, 3, 4]))
    mock_keypad_dev.mech_status = None

    keypad_wrapper.device = mock_keypad_dev
    keypad_wrapper.store = AsyncMock()
    keypad_wrapper.logical_passcodes = {}
    keypad_wrapper._sync_and_apply_schedules = AsyncMock()

    # Set up lock entry in hass.data
    lock_wrapper = MagicMock()
    lock_wrapper.model_name = "SESAME6_PRO"
    lock_wrapper.adv_data.device_uuid = lock_uuid
    hass.data[DOMAIN][lock_entry_id] = lock_wrapper

    # Extract handlers
    add_handler = None
    del_handler = None
    for call_args in hass.services.async_register.call_args_list:
        if call_args[0][1] == "add_passcode":
            add_handler = call_args[0][2]
        elif call_args[0][1] == "delete_passcode":
            del_handler = call_args[0][2]

    assert add_handler is not None
    assert del_handler is not None

    # Setup mock registries
    mock_dev_reg = MagicMock()
    keypad_device_entry = MagicMock()
    keypad_device_entry.config_entries = [keypad_entry_id]
    lock_device_entry = MagicMock()
    lock_device_entry.config_entries = [lock_entry_id]

    def dev_reg_get(dev_id):
        if dev_id == "keypad_dev_id":
            return keypad_device_entry
        elif dev_id == "lock_dev_id":
            return lock_device_entry
        return None

    mock_dev_reg.async_get.side_effect = dev_reg_get

    mock_ent_reg = MagicMock()
    keypad_sensor_entity = MagicMock()
    keypad_sensor_entity.device_id = "keypad_dev_id"
    lock_entity = MagicMock()
    lock_entity.device_id = "lock_dev_id"

    def ent_reg_get(ent_id):
        if ent_id == "sensor.entry_currawang_keypad_registered_passcodes":
            return keypad_sensor_entity
        elif ent_id == "lock.entry_currawang_entry":
            return lock_entity
        return None

    mock_ent_reg.async_get.side_effect = ent_reg_get

    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg, create=True), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_ent_reg, create=True):

        # 1. Add passcode via Keypad Entity ID
        call1 = MagicMock()
        call1.data = {
            "device_id": "sensor.entry_currawang_keypad_registered_passcodes",
            "passcode": "1234",
            "name": "Guest Keypad",
        }
        await add_handler(call1)
        uid1 = bytes([1, 2, 3, 4]).hex()
        assert uid1 in keypad_wrapper.logical_passcodes
        assert keypad_wrapper.logical_passcodes[uid1]["name"] == "Guest Keypad"

        # 2. Add passcode via Lock Entity ID (auto-resolves paired keypad)
        call2 = MagicMock()
        call2.data = {
            "entity_id": "lock.entry_currawang_entry",
            "passcode": "5678",
            "name": "Guest Via Lock",
        }
        await add_handler(call2)
        uid2 = bytes([5, 6, 7, 8]).hex()
        assert uid2 in keypad_wrapper.logical_passcodes
        assert keypad_wrapper.logical_passcodes[uid2]["name"] == "Guest Via Lock"

        # 3. Delete passcode via Lock Entity ID
        del_call = MagicMock()
        del_call.data = {
            "device_id": "lock.entry_currawang_entry",
            "passcode_or_id": "1234",
        }
        await del_handler(del_call)
        assert uid1 not in keypad_wrapper.logical_passcodes


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_face_1_keypad_setup_and_entities(mock_bluetooth) -> None:
    """Tests entry setup and Keypad Manager recognition for a Sesame Face 1 keypad."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_ble_device.address = "FA:CE:01:22:33:44"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    # Model ID 13 (SESAME_FACE)
    mfg_data = struct.pack("<HB16s", 13, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_face_1"
    entry.unique_id = "test_mac_face_1"
    entry.data = {
        "mac_address": "FA:CE:01:22:33:44",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_FACE",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True

    wrapper = hass.data[DOMAIN][entry.entry_id]
    assert wrapper.model_name == "SESAME_FACE"
    assert sesame_ble.is_keypad_model("SESAME_FACE") is True

    # Check Keypad Passcodes View returns Face 1 in keypads_list
    import json
    view = SesamePasscodesView(hass)
    req = MagicMock()
    resp = await view.get(req)
    data = json.loads(resp.body)


    assert "keypads" in data
    keypad_ids = [k["entry_id"] for k in data["keypads"]]
    assert entry.entry_id in keypad_ids


@pytest.mark.asyncio
@patch("sesame_ble.bluetooth")
async def test_face_ai_keypad_setup_and_entities_filtering(mock_bluetooth) -> None:
    """Tests that Face AI keypads filter out lock angles/position buttons and unsupported auth count sensors (cards/fingerprints)."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_ble_device.address = "FA:CE:AI:00:11:22"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    # Model ID 23 (SESAME_FACE_AI)
    mfg_data = struct.pack("<HB16s", 23, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_face_ai"
    entry.unique_id = "test_mac_face_ai"
    entry.data = {
        "mac_address": "FA:CE:AI:00:11:22",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME_FACE_AI",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True

    # 1. Lock entities must be skipped
    async_add_locks = MagicMock()
    await sesame_ble.lock.async_setup_entry(hass, entry, async_add_locks)
    async_add_locks.assert_not_called()

    # 2. Calibration buttons must be skipped
    async_add_buttons = MagicMock()
    await sesame_ble.button.async_setup_entry(hass, entry, async_add_buttons)
    async_add_buttons.assert_not_called()

    # 3. Auto lock numbers must be skipped
    async_add_numbers = MagicMock()
    await sesame_ble.number.async_setup_entry(hass, entry, async_add_numbers)
    async_add_numbers.assert_not_called()

    # 4. Door binary sensor must be skipped
    async_add_binary = MagicMock()
    await sesame_ble.binary_sensor.async_setup_entry(hass, entry, async_add_binary)
    async_add_binary.assert_not_called()

    # 5. Sensors must ONLY include Battery, Passcodes, Faces, Palms, Paired Locks, RSSI, Connection (NO Cards, NO Fingerprints, NO lock position angles)
    async_add_sensors = MagicMock()
    await sesame_ble.sensor.async_setup_entry(hass, entry, async_add_sensors)
    async_add_sensors.assert_called_once()
    sensors = async_add_sensors.call_args[0][0]

    sensor_names = [s.name for s in sensors]
    assert "Battery" in sensor_names
    assert "Registered Passcodes" in sensor_names
    assert "Registered Faces" in sensor_names
    assert "Registered Palms" in sensor_names
    assert "Paired Locks" in sensor_names
    assert "Signal Strength" in sensor_names

    assert "Connection State" in sensor_names



    # Ensure unsupported auth counts and lock angles are NOT present
    assert "Registered Cards" not in sensor_names
    assert "Registered Fingerprints" not in sensor_names
    assert "Locked Position" not in sensor_names
    assert "Unlocked Position" not in sensor_names
    assert "Current Position" not in sensor_names


@pytest.mark.asyncio
@patch("custom_components.sesame_ble.bluetooth")
async def test_firmware_update_entity_and_dfu(mock_bluetooth):
    """Test setup of SesameFirmwareUpdateEntity, version checking, and DFU mode trigger."""
    import custom_components.sesame_ble as sesame_ble
    from custom_components.sesame_ble.update import SesameFirmwareUpdateEntity
    import custom_components.sesame_ble.update as sesame_update

    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    mock_ble_device = MagicMock()
    mock_ble_device.address = "DE:34:B7:06:2E:99"
    mock_bluetooth.async_ble_device_from_address.return_value = mock_ble_device

    # Model ID 5 (SESAME5)
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_bluetooth.async_get_advertisement_data.return_value = MockAdvertisementData(mfg_data)

    entry = MagicMock()
    entry.entry_id = "test_entry_update"
    entry.unique_id = "test_mac_update"
    entry.data = {
        "mac_address": "DE:34:B7:06:2E:99",
        CONF_SECRET_KEY: "8934fa89504ea1b70993b91ff4407aa9",
        CONF_MODEL: "SESAME5",
        CONF_DEVICE_UUID: str(TEST_UUID),
    }

    setup_ok = await sesame_ble.async_setup_entry(hass, entry)
    assert setup_ok is True
    wrapper = hass.data[DOMAIN][entry.entry_id]

    # Setup update platform
    async_add_updates = MagicMock()
    await sesame_update.async_setup_entry(hass, entry, async_add_updates)
    async_add_updates.assert_called_once()
    
    entities = async_add_updates.call_args[0][0]
    assert len(entities) == 1
    update_entity: SesameFirmwareUpdateEntity = entities[0]

    assert update_entity.name == "Firmware Update"
    assert update_entity.unique_id == "test_mac_update_firmware_update"
    assert update_entity.latest_version == "v3.0"

    # Simulate device returned installed firmware version v2.0
    wrapper.device._firmware_version = "v2.0"
    wrapper.device.is_logged_in = True

    assert update_entity.installed_version == "v2.0"
    assert update_entity.available is True
    assert "v3.0" in update_entity.release_summary

    # Test async_install triggers DFU command
    wrapper.device.enable_dfu = AsyncMock()
    await update_entity.async_install("v3.0", backup=False)
    wrapper.device.enable_dfu.assert_called_once()




