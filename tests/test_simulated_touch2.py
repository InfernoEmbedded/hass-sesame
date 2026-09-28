"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame Touch 2 keypad."""

import asyncio
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameTouch2,
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


async def create_connected_touch2(mock_bleak_establish, secret_key: bytes = b"\x33" * 16) -> tuple[SimulatedSesameTouch2, SesameKeypad]:
    sim_touch2 = SimulatedSesameTouch2(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_touch2.ble_address,
        name="SESAME_TOUCH_2",
        sim_device=sim_touch2,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_TOUCH_2.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666625"),
    )
    keypad_device = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=secret_key.hex(),
    )
    await keypad_device.connect()
    await keypad_device.login()
    return sim_touch2, keypad_device


@pytest.mark.asyncio
async def test_touch2_login_and_initial_telemetry(mock_bleak_establish):
    """Verify session establishment and mechanical status parsing."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    assert touch2.is_connected is True
    assert touch2.is_logged_in is True
    assert touch2.battery_voltage == 6.0
    assert touch2.passcodes_count == 0
    assert touch2.cards_count == 0
    assert touch2.fingerprints_count == 0

    await touch2.disconnect()
    assert touch2.is_logged_in is False


@pytest.mark.asyncio
async def test_touch2_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that connecting with a wrong secret key fails during login."""
    sim_touch2 = SimulatedSesameTouch2(secret_key=b"\x33" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_touch2.ble_address,
        name="SESAME_TOUCH_2",
        sim_device=sim_touch2,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_TOUCH_2.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666625"),
    )
    touch2 = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="44" * 16,
    )

    await touch2.connect()
    with pytest.raises(Exception):
        await touch2.login()


@pytest.mark.asyncio
async def test_touch2_passcode_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting passcodes."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    # 1. Add passcode PIN "1234"
    await touch2.add_passcode("1234", "Guest Key")
    await asyncio.sleep(0.05)

    assert len(sim_touch2.registered_passcodes) == 1
    assert touch2.passcodes_count == 1

    # 2. Sync passcodes database
    passcodes = await touch2.get_passcodes()
    assert len(passcodes) == 1
    code_id = list(passcodes.keys())[0]
    assert passcodes[code_id]["code"] == "1234"
    assert passcodes[code_id]["name"] == "Guest Key"

    # 3. Update passcode nickname
    await touch2.update_passcode_name("1234", "VIP Key")
    await asyncio.sleep(0.05)
    passcodes = await touch2.get_passcodes()
    assert passcodes[code_id]["name"] == "VIP Key"

    # 4. Delete passcode
    await touch2.delete_passcode("1234")
    await asyncio.sleep(0.05)
    assert len(sim_touch2.registered_passcodes) == 0
    assert touch2.passcodes_count == 0

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_card_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting RFID/NFC cards."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    card_hex = "010203040506"
    # 1. Add card
    await touch2.add_card(card_hex, "Alice Card")
    await asyncio.sleep(0.05)

    assert len(sim_touch2.registered_cards) == 1
    assert touch2.cards_count == 1

    # 2. Sync cards database
    cards = await touch2.get_cards()
    assert len(cards) == 1
    assert card_hex in cards
    assert cards[card_hex]["name"] == "Alice Card"

    # 3. Update card nickname
    await touch2.update_card_name(card_hex, "Alice Work Badge")
    await asyncio.sleep(0.05)
    cards = await touch2.get_cards()
    assert cards[card_hex]["name"] == "Alice Work Badge"

    # 4. Delete card
    await touch2.delete_card(card_hex)
    await asyncio.sleep(0.05)
    assert len(sim_touch2.registered_cards) == 0
    assert touch2.cards_count == 0

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_fingerprint_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting fingerprints."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    # Seed an enrolled fingerprint
    fp_id_bytes = bytes.fromhex("AA11BB22")
    sim_touch2.registered_fingerprints.append({
        "id": fp_id_bytes.hex(),
        "id_bytes": fp_id_bytes,
        "name": "Right Index",
        "type": 0,
    })
    sim_touch2._emit_mech_status()
    await asyncio.sleep(0.05)
    assert touch2.fingerprints_count == 1

    # 1. Sync fingerprints
    fps = await touch2.get_fingerprints()
    assert len(fps) == 1
    assert fp_id_bytes.hex() in fps
    assert fps[fp_id_bytes.hex()]["name"] == "Right Index"

    # 2. Update nickname
    await touch2.update_fingerprint_name(fp_id_bytes.hex(), "Left Thumb")
    await asyncio.sleep(0.05)
    fps = await touch2.get_fingerprints()
    assert fps[fp_id_bytes.hex()]["name"] == "Left Thumb"

    # 3. Delete fingerprint
    await touch2.delete_fingerprint(fp_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_touch2.registered_fingerprints) == 0
    assert touch2.fingerprints_count == 0

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_paired_locks_management(mock_bleak_establish):
    """Verify pairing and unpairing locks to the keypad."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    target_uuid = UUID("12345678-1234-5678-1234-567812345678")
    secret = b"\x77" * 16

    # 1. Pair lock
    await touch2.add_paired_lock(target_uuid, secret)
    await asyncio.sleep(0.05)

    assert len(sim_touch2.paired_locks) == 1
    assert len(touch2.paired_locks) == 1
    assert touch2.paired_locks[0]["uuid"] == str(target_uuid)

    # 2. Unpair lock
    await touch2.remove_paired_lock(target_uuid)
    await asyncio.sleep(0.05)

    assert len(sim_touch2.paired_locks) == 0
    assert len(touch2.paired_locks) == 0

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_registration_modes(mock_bleak_establish):
    """Verify setting passcode, card, and fingerprint registration modes."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    await touch2.set_passcode_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch2.passcode_registration_mode is True

    await touch2.set_card_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch2.card_registration_mode is True

    await touch2.set_fingerprint_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_touch2.fingerprint_registration_mode is True

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_physical_matrix_valid_passcode_unlocks_linked_lock(mock_bleak_establish):
    """Verify entering enrolled PIN on physical keypad illuminates green LED, beeps, and unlocks linked lock."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch2.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Register passcode "4321"
    await touch2.add_passcode("4321", "Test PIN")
    await asyncio.sleep(0.05)

    # Simulate typing "4", "3", "2", "1", "#" on keypad
    sim_touch2.press_key("4")
    sim_touch2.press_key("3")
    sim_touch2.press_key("2")
    sim_touch2.press_key("1")
    sim_touch2.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_touch2.led_green is True
    assert sim_lock.is_locked is False

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_physical_matrix_invalid_passcode_rejected(mock_bleak_establish):
    """Verify invalid PIN entry illuminates red LED and leaves linked lock locked."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch2.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Type wrong code "9999#"
    sim_touch2.press_key("9")
    sim_touch2.press_key("9")
    sim_touch2.press_key("9")
    sim_touch2.press_key("9")
    sim_touch2.press_key("#")

    assert sim_touch2.led_red is True
    assert sim_lock.is_locked is True

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_physical_matrix_backspace_clear(mock_bleak_establish):
    """Verify '*' clears input buffer before evaluating PIN."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch2.linked_lock = sim_lock

    # Type wrong digits, then "*", then default demo code "123456#"
    sim_touch2.press_key("9")
    sim_touch2.press_key("9")
    sim_touch2.press_key("*")
    assert sim_touch2.keypad_input == ""

    for digit in "123456":
        sim_touch2.press_key(digit)
    sim_touch2.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_touch2.led_green is True
    assert sim_lock.is_locked is False

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_card_tap_unlocks_linked_lock(mock_bleak_establish):
    """Verify tapping enrolled NFC card unlocks linked lock."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch2.linked_lock = sim_lock

    card_uid = "A1B2C3D4"
    await touch2.add_card(card_uid, "Office Badge")
    await asyncio.sleep(0.05)

    assert sim_lock.is_locked is True

    sim_touch2.scan_card(card_uid)
    await asyncio.sleep(0.35)

    assert sim_touch2.led_green is True
    assert sim_lock.is_locked is False

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_card_tap_unregistered_triggers_cloud_verification(mock_bleak_establish):
    """Verify tapping unknown NFC card triggers cloud verification broadcast."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    unknown_card = "DEADBEEF0102"
    sim_touch2.scan_card(unknown_card)
    await asyncio.sleep(0.05)

    assert sim_touch2.led_red is True
    assert touch2.scanned_card is not None
    assert touch2.scanned_card["uid"].lower() == unknown_card.lower()

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_fingerprint_scan_unlocks_linked_lock(mock_bleak_establish):
    """Verify matched fingerprint unlocks linked lock."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_touch2.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    sim_touch2.scan_fingerprint(matched=True)
    await asyncio.sleep(0.35)

    assert sim_touch2.led_green is True
    assert sim_lock.is_locked is False

    # Unmatched fingerprint fails
    sim_touch2.scan_fingerprint(matched=False)
    assert sim_touch2.led_red is True

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_unregistered_passcode_cloud_verify(mock_bleak_establish):
    """Verify entering unknown passcode triggers cloud verification broadcast."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    for digit in "7890":
        sim_touch2.press_key(digit)
    sim_touch2.press_key("#")
    await asyncio.sleep(0.05)

    assert sim_touch2.led_red is True
    assert touch2.scanned_passcode is not None
    assert touch2.scanned_passcode["code"] == "7890"

    await touch2.disconnect()


@pytest.mark.asyncio
async def test_touch2_firmware_version_query(mock_bleak_establish):
    """Verify querying firmware version from Sesame Touch 2."""
    sim_touch2, touch2 = await create_connected_touch2(mock_bleak_establish)

    version = await touch2.request_firmware_version()
    assert version == "3.0-10-e877d5"
    assert touch2.firmware_version == "3.0-10-e877d5"

    await touch2.disconnect()
