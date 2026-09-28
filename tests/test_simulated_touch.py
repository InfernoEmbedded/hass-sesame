"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame Touch keypad."""

import asyncio
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameTouch,
)
from sesame_sim.ble_bridge import VirtualBleakClient, VirtualBLEDevice
from pysesame_ble import (
    ProductModels,
    SesameAdData,
    SesameKeypad,
)


@pytest.fixture
def mock_bleak_establish(monkeypatch):
    """Mocks establish_connection to return our in-memory VirtualBleakClient."""
    async def mock_establish(client_cls, dev, *args, **kwargs):
        client = VirtualBleakClient(dev, simulated_device=dev._sim_device, **kwargs)
        await client.connect()
        return client

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", mock_establish)


async def create_connected_touch(mock_bleak_establish, secret_key: bytes = b"\x33" * 16) -> tuple[SimulatedSesameTouch, SesameKeypad]:
    sim_touch = SimulatedSesameTouch(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_touch.ble_address,
        name="SESAME_TOUCH",
        sim_device=sim_touch,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_TOUCH.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666610"),
    )
    keypad_device = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=secret_key.hex(),
    )
    await keypad_device.connect()
    await keypad_device.login()
    return sim_touch, keypad_device


@pytest.mark.asyncio
async def test_touch_login_and_initial_telemetry(mock_bleak_establish):
    """Verify session establishment and mechanical status parsing."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    assert touch.is_connected is True
    assert touch.is_logged_in is True
    assert touch.battery_voltage == 6.0
    assert touch.passcodes_count == 0
    assert touch.cards_count == 0
    assert touch.fingerprints_count == 0

    await touch.disconnect()
    assert touch.is_logged_in is False


@pytest.mark.asyncio
async def test_touch_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that connecting with a wrong secret key fails during login."""
    sim_touch = SimulatedSesameTouch(secret_key=b"\x33" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_touch.ble_address,
        name="SESAME_TOUCH",
        sim_device=sim_touch,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_TOUCH.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666610"),
    )
    touch = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="44" * 16,
    )

    await touch.connect()
    with pytest.raises(Exception):
        await touch.login()


@pytest.mark.asyncio
async def test_touch_passcode_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting passcodes."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    # 1. Add passcode PIN "1234"
    await touch.add_passcode("1234", "Guest Key")
    await asyncio.sleep(0.05)

    assert len(sim_touch.registered_passcodes) == 1
    assert touch.passcodes_count == 1

    # 2. Sync passcodes database
    passcodes = await touch.get_passcodes()
    assert len(passcodes) == 1
    code_id = list(passcodes.keys())[0]
    assert passcodes[code_id]["code"] == "1234"
    assert passcodes[code_id]["name"] == "Guest Key"

    # 3. Update passcode nickname
    await touch.update_passcode_name("1234", "VIP Key")
    await asyncio.sleep(0.05)
    passcodes = await touch.get_passcodes()
    assert passcodes[code_id]["name"] == "VIP Key"

    # 4. Delete passcode
    await touch.delete_passcode("1234")
    await asyncio.sleep(0.05)
    assert len(sim_touch.registered_passcodes) == 0
    assert touch.passcodes_count == 0

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_card_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting RFID/NFC cards."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    card_hex = "010203040506"
    # 1. Add card
    await touch.add_card(card_hex, "Alice Card")
    await asyncio.sleep(0.05)

    assert len(sim_touch.registered_cards) == 1
    assert touch.cards_count == 1

    # 2. Sync cards database
    cards = await touch.get_cards()
    assert len(cards) == 1
    assert card_hex in cards
    assert cards[card_hex]["name"] == "Alice Card"

    # 3. Update card nickname
    await touch.update_card_name(card_hex, "Alice Work Badge")
    await asyncio.sleep(0.05)
    cards = await touch.get_cards()
    assert cards[card_hex]["name"] == "Alice Work Badge"

    # 4. Delete card
    await touch.delete_card(card_hex)
    await asyncio.sleep(0.05)
    assert len(sim_touch.registered_cards) == 0
    assert touch.cards_count == 0

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_fingerprint_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting fingerprints."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    # Seed an enrolled fingerprint
    fp_id_bytes = bytes.fromhex("AA11BB22")
    sim_touch.registered_fingerprints.append({
        "id": fp_id_bytes.hex(),
        "id_bytes": fp_id_bytes,
        "name": "Right Index",
        "type": 0,
    })
    sim_touch._emit_mech_status()
    await asyncio.sleep(0.05)
    assert touch.fingerprints_count == 1

    # 1. Sync fingerprints
    fps = await touch.get_fingerprints()
    assert len(fps) == 1
    assert fp_id_bytes.hex() in fps
    assert fps[fp_id_bytes.hex()]["name"] == "Right Index"

    # 2. Update nickname
    await touch.update_fingerprint_name(fp_id_bytes.hex(), "Left Thumb")
    await asyncio.sleep(0.05)
    fps = await touch.get_fingerprints()
    assert fps[fp_id_bytes.hex()]["name"] == "Left Thumb"

    # 3. Delete fingerprint
    await touch.delete_fingerprint(fp_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_touch.registered_fingerprints) == 0
    assert touch.fingerprints_count == 0

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_paired_locks_management(mock_bleak_establish):
    """Verify pairing and unpairing locks to the keypad."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    target_uuid = UUID("12345678-1234-5678-1234-567812345678")
    secret = b"\x77" * 16

    # 1. Pair lock
    await touch.add_paired_lock(target_uuid, secret)
    await asyncio.sleep(0.05)

    assert len(sim_touch.paired_locks) == 1
    assert len(touch.paired_locks) == 1
    assert touch.paired_locks[0]["uuid"] == str(target_uuid)

    # 2. Unpair lock
    await touch.remove_paired_lock(target_uuid)
    await asyncio.sleep(0.05)

    assert len(sim_touch.paired_locks) == 0
    assert len(touch.paired_locks) == 0

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_registration_modes(mock_bleak_establish):
    """Verify setting passcode, card, and fingerprint registration modes."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    await touch.set_passcode_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch.passcode_registration_mode is True

    await touch.set_card_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch.card_registration_mode is True

    await touch.set_fingerprint_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch.fingerprint_registration_mode is True

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_physical_matrix_valid_passcode_unlocks_linked_lock(mock_bleak_establish):
    """Verify entering enrolled PIN on physical keypad illuminates green LED, beeps, and unlocks linked lock."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Register passcode "4321"
    await touch.add_passcode("4321", "Test PIN")
    await asyncio.sleep(0.05)

    # Simulate typing "4", "3", "2", "1", "#" on keypad
    sim_touch.press_key("4")
    sim_touch.press_key("3")
    sim_touch.press_key("2")
    sim_touch.press_key("1")
    sim_touch.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_touch.led_green is True
    assert sim_lock.is_locked is False

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_physical_matrix_invalid_passcode_rejected(mock_bleak_establish):
    """Verify invalid PIN entry illuminates red LED and leaves linked lock locked."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Type wrong code "9999#"
    sim_touch.press_key("9")
    sim_touch.press_key("9")
    sim_touch.press_key("9")
    sim_touch.press_key("9")
    sim_touch.press_key("#")

    assert sim_touch.led_red is True
    assert sim_lock.is_locked is True

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_physical_matrix_backspace_clear(mock_bleak_establish):
    """Verify '*' clears input buffer before evaluating PIN."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch.linked_lock = sim_lock

    # Type wrong digits, then "*", then default demo code "123456#"
    sim_touch.press_key("9")
    sim_touch.press_key("9")
    sim_touch.press_key("*")
    assert sim_touch.keypad_input == ""

    for digit in "123456":
        sim_touch.press_key(digit)
    sim_touch.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_touch.led_green is True
    assert sim_lock.is_locked is False

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_card_tap_unlocks_linked_lock(mock_bleak_establish):
    """Verify tapping enrolled NFC card unlocks linked lock."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch.linked_lock = sim_lock

    card_uid = "A1B2C3D4"
    await touch.add_card(card_uid, "Office Badge")
    await asyncio.sleep(0.05)

    assert sim_lock.is_locked is True

    sim_touch.scan_card(card_uid)
    await asyncio.sleep(0.35)

    assert sim_touch.led_green is True
    assert sim_lock.is_locked is False

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_card_tap_unregistered_triggers_cloud_verification(mock_bleak_establish):
    """Verify tapping unknown NFC card triggers cloud verification broadcast."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    unknown_card = "DEADBEEF0102"
    sim_touch.scan_card(unknown_card)
    await asyncio.sleep(0.05)

    assert sim_touch.led_red is True
    assert touch.scanned_card is not None
    assert touch.scanned_card["uid"].lower() == unknown_card.lower()

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_fingerprint_scan_unlocks_linked_lock(mock_bleak_establish):
    """Verify matched fingerprint unlocks linked lock."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    sim_touch.scan_fingerprint(matched=True)
    await asyncio.sleep(0.35)

    assert sim_touch.led_green is True
    assert sim_lock.is_locked is False

    # Unmatched fingerprint fails
    sim_touch.scan_fingerprint(matched=False)
    assert sim_touch.led_red is True

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_unregistered_passcode_cloud_verify(mock_bleak_establish):
    """Verify entering unknown passcode triggers cloud verification broadcast."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    for digit in "7890":
        sim_touch.press_key(digit)
    sim_touch.press_key("#")
    await asyncio.sleep(0.05)

    assert sim_touch.led_red is True
    assert touch.scanned_passcode is not None
    assert touch.scanned_passcode["code"] == "7890"

    await touch.disconnect()


@pytest.mark.asyncio
async def test_touch_firmware_version_query(mock_bleak_establish):
    """Verify querying firmware version from Sesame Touch."""
    sim_touch, touch = await create_connected_touch(mock_bleak_establish)

    version = await touch.request_firmware_version()
    assert version == "3.0-10-e877d5"
    assert touch.firmware_version == "3.0-10-e877d5"

    await touch.disconnect()
