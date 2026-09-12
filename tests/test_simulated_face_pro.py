"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame Face Pro keypad."""

import asyncio
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameFacePro,
)
from sesame_sim.ble_bridge import VirtualBleakClient, VirtualBLEDevice
from custom_components.sesame_ble.sesame_client.device import (
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


async def create_connected_face_pro(mock_bleak_establish, secret_key: bytes = b"\x33" * 16) -> tuple[SimulatedSesameFacePro, SesameKeypad]:
    sim_face_pro = SimulatedSesameFacePro(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_face_pro.ble_address,
        name="SESAME_FACE_PRO",
        sim_device=sim_face_pro,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_FACE_PRO.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666618"),
    )
    keypad_device = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=secret_key.hex(),
    )
    await keypad_device.connect()
    await keypad_device.login()
    return sim_face_pro, keypad_device


@pytest.mark.asyncio
async def test_face_pro_login_and_initial_telemetry(mock_bleak_establish):
    """Verify session establishment and mechanical status parsing."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    assert face_pro.is_connected is True
    assert face_pro.is_logged_in is True
    assert face_pro.battery_voltage == 6.0
    assert face_pro.passcodes_count == 0
    assert face_pro.cards_count == 0
    assert face_pro.fingerprints_count == 0
    assert face_pro.faces_count == 0

    await face_pro.disconnect()
    assert face_pro.is_logged_in is False


@pytest.mark.asyncio
async def test_face_pro_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that connecting with a wrong secret key fails during login."""
    sim_face_pro = SimulatedSesameFacePro(secret_key=b"\x33" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_face_pro.ble_address,
        name="SESAME_FACE_PRO",
        sim_device=sim_face_pro,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_FACE_PRO.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666618"),
    )
    face_pro = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="44" * 16,
    )

    await face_pro.connect()
    with pytest.raises(Exception):
        await face_pro.login()


@pytest.mark.asyncio
async def test_face_pro_passcode_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting passcodes."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    # 1. Add passcode PIN "1234"
    await face_pro.add_passcode("1234", "Guest Key")
    await asyncio.sleep(0.05)

    assert len(sim_face_pro.registered_passcodes) == 1
    assert face_pro.passcodes_count == 1

    # 2. Sync passcodes database
    passcodes = await face_pro.get_passcodes()
    assert len(passcodes) == 1
    code_id = list(passcodes.keys())[0]
    assert passcodes[code_id]["code"] == "1234"
    assert passcodes[code_id]["name"] == "Guest Key"

    # 3. Update passcode nickname
    await face_pro.update_passcode_name("1234", "VIP Key")
    await asyncio.sleep(0.05)
    passcodes = await face_pro.get_passcodes()
    assert passcodes[code_id]["name"] == "VIP Key"

    # 4. Delete passcode
    await face_pro.delete_passcode("1234")
    await asyncio.sleep(0.05)
    assert len(sim_face_pro.registered_passcodes) == 0
    assert face_pro.passcodes_count == 0

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_card_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting RFID/NFC cards."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    card_hex = "010203040506"
    # 1. Add card
    await face_pro.add_card(card_hex, "Alice Card")
    await asyncio.sleep(0.05)

    assert len(sim_face_pro.registered_cards) == 1
    assert face_pro.cards_count == 1

    # 2. Sync cards database
    cards = await face_pro.get_cards()
    assert len(cards) == 1
    assert card_hex in cards
    assert cards[card_hex]["name"] == "Alice Card"

    # 3. Update card nickname
    await face_pro.update_card_name(card_hex, "Alice Work Badge")
    await asyncio.sleep(0.05)
    cards = await face_pro.get_cards()
    assert cards[card_hex]["name"] == "Alice Work Badge"

    # 4. Delete card
    await face_pro.delete_card(card_hex)
    await asyncio.sleep(0.05)
    assert len(sim_face_pro.registered_cards) == 0
    assert face_pro.cards_count == 0

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_fingerprint_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting fingerprints."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    # Seed an enrolled fingerprint
    fp_id_bytes = bytes.fromhex("AA11BB22")
    sim_face_pro.registered_fingerprints.append({
        "id": fp_id_bytes.hex(),
        "id_bytes": fp_id_bytes,
        "name": "Right Index",
        "type": 0,
    })
    sim_face_pro._emit_mech_status()
    await asyncio.sleep(0.05)
    assert face_pro.fingerprints_count == 1

    # 1. Sync fingerprints
    fps = await face_pro.get_fingerprints()
    assert len(fps) == 1
    assert fp_id_bytes.hex() in fps
    assert fps[fp_id_bytes.hex()]["name"] == "Right Index"

    # 2. Update nickname
    await face_pro.update_fingerprint_name(fp_id_bytes.hex(), "Left Thumb")
    await asyncio.sleep(0.05)
    fps = await face_pro.get_fingerprints()
    assert fps[fp_id_bytes.hex()]["name"] == "Left Thumb"

    # 3. Delete fingerprint
    await face_pro.delete_fingerprint(fp_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_face_pro.registered_fingerprints) == 0
    assert face_pro.fingerprints_count == 0

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_biometric_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting biometric face profiles."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    # Seed an enrolled face profile
    face_id_bytes = bytes.fromhex("CC33DD44")
    sim_face_pro.registered_faces.append({
        "id": face_id_bytes.hex(),
        "id_bytes": face_id_bytes,
        "name": "Admin Face",
        "type": 0,
    })
    sim_face_pro._emit_mech_status()
    await asyncio.sleep(0.05)
    assert face_pro.faces_count == 1

    # 1. Sync faces
    faces = await face_pro.get_faces()
    assert len(faces) == 1
    assert face_id_bytes.hex() in faces
    assert faces[face_id_bytes.hex()]["name"] == "Admin Face"

    # 2. Update nickname
    await face_pro.update_face_name(face_id_bytes.hex(), "Owner Face")
    await asyncio.sleep(0.05)
    faces = await face_pro.get_faces()
    assert faces[face_id_bytes.hex()]["name"] == "Owner Face"

    # 3. Delete face
    await face_pro.delete_face(face_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_face_pro.registered_faces) == 0
    assert face_pro.faces_count == 0

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_paired_locks_management(mock_bleak_establish):
    """Verify pairing and unpairing locks to the keypad."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    target_uuid = UUID("12345678-1234-5678-1234-567812345678")
    secret = b"\x77" * 16

    # 1. Pair lock
    await face_pro.add_paired_lock(target_uuid, secret)
    await asyncio.sleep(0.05)

    assert len(sim_face_pro.paired_locks) == 1
    assert len(face_pro.paired_locks) == 1
    assert face_pro.paired_locks[0]["uuid"] == str(target_uuid)

    # 2. Unpair lock
    await face_pro.remove_paired_lock(target_uuid)
    await asyncio.sleep(0.05)

    assert len(sim_face_pro.paired_locks) == 0
    assert len(face_pro.paired_locks) == 0

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_registration_modes(mock_bleak_establish):
    """Verify setting passcode, card, fingerprint, and face registration modes."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    await face_pro.set_passcode_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face_pro.passcode_registration_mode is True

    await face_pro.set_card_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face_pro.card_registration_mode is True

    await face_pro.set_fingerprint_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face_pro.fingerprint_registration_mode is True

    await face_pro.set_face_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face_pro.face_registration_mode is True

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_physical_matrix_valid_passcode_unlocks_linked_lock(mock_bleak_establish):
    """Verify entering enrolled PIN on physical keypad illuminates green LED, beeps, and unlocks linked lock."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face_pro.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Register passcode "4321"
    await face_pro.add_passcode("4321", "Test PIN")
    await asyncio.sleep(0.05)

    # Simulate typing "4", "3", "2", "1", "#" on keypad
    sim_face_pro.press_key("4")
    sim_face_pro.press_key("3")
    sim_face_pro.press_key("2")
    sim_face_pro.press_key("1")
    sim_face_pro.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_face_pro.led_green is True
    assert sim_lock.is_locked is False

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_physical_matrix_invalid_passcode_rejected(mock_bleak_establish):
    """Verify invalid PIN entry illuminates red LED and leaves linked lock locked."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face_pro.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Type wrong code "9999#"
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("#")

    assert sim_face_pro.led_red is True
    assert sim_lock.is_locked is True

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_physical_matrix_backspace_clear(mock_bleak_establish):
    """Verify '*' clears input buffer before evaluating PIN."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face_pro.linked_lock = sim_lock

    # Type wrong digits, then "*", then default demo code "123456#"
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("9")
    sim_face_pro.press_key("*")
    assert sim_face_pro.keypad_input == ""

    for digit in "123456":
        sim_face_pro.press_key(digit)
    sim_face_pro.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_face_pro.led_green is True
    assert sim_lock.is_locked is False

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_card_tap_unlocks_linked_lock(mock_bleak_establish):
    """Verify tapping enrolled NFC card unlocks linked lock."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face_pro.linked_lock = sim_lock

    card_uid = "A1B2C3D4"
    await face_pro.add_card(card_uid, "Office Badge")
    await asyncio.sleep(0.05)

    assert sim_lock.is_locked is True

    sim_face_pro.scan_card(card_uid)
    await asyncio.sleep(0.35)

    assert sim_face_pro.led_green is True
    assert sim_lock.is_locked is False

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_card_tap_unregistered_triggers_cloud_verification(mock_bleak_establish):
    """Verify tapping unknown NFC card triggers cloud verification broadcast."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    unknown_card = "DEADBEEF0102"
    sim_face_pro.scan_card(unknown_card)
    await asyncio.sleep(0.05)

    assert sim_face_pro.led_red is True
    assert face_pro.scanned_card is not None
    assert face_pro.scanned_card["uid"].lower() == unknown_card.lower()

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_biometric_scan_unlocks_linked_lock(mock_bleak_establish):
    """Verify face recognition scan unlocks linked lock."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face_pro.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    sim_face_pro.scan_face(matched=True)
    await asyncio.sleep(0.35)

    assert sim_face_pro.led_green is True
    assert sim_lock.is_locked is False

    # Unmatched face fails
    sim_face_pro.scan_face(matched=False)
    assert sim_face_pro.led_red is True

    await face_pro.disconnect()


@pytest.mark.asyncio
async def test_face_pro_firmware_version_query(mock_bleak_establish):
    """Verify querying firmware version from Sesame Face Pro."""
    sim_face_pro, face_pro = await create_connected_face_pro(mock_bleak_establish)

    version = await face_pro.request_firmware_version()
    assert version == "3.0-18-e877d5"
    assert face_pro.firmware_version == "3.0-18-e877d5"

    await face_pro.disconnect()
