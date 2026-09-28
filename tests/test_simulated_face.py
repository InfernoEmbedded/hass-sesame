"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame Face keypad."""

import asyncio
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameFace,
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


async def create_connected_face(mock_bleak_establish, secret_key: bytes = b"\x33" * 16) -> tuple[SimulatedSesameFace, SesameKeypad]:
    sim_face = SimulatedSesameFace(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_face.ble_address,
        name="SESAME_FACE",
        sim_device=sim_face,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_FACE.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666619"),
    )
    keypad_device = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=secret_key.hex(),
    )
    await keypad_device.connect()
    await keypad_device.login()
    return sim_face, keypad_device


@pytest.mark.asyncio
async def test_face_login_and_initial_telemetry(mock_bleak_establish):
    """Verify session establishment and mechanical status parsing."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    assert face.is_connected is True
    assert face.is_logged_in is True
    assert face.battery_voltage == 6.0
    assert face.passcodes_count == 0
    assert face.cards_count == 0
    assert face.fingerprints_count == 0
    assert face.faces_count == 0

    await face.disconnect()
    assert face.is_logged_in is False


@pytest.mark.asyncio
async def test_face_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that connecting with a wrong secret key fails during login."""
    sim_face = SimulatedSesameFace(secret_key=b"\x33" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_face.ble_address,
        name="SESAME_FACE",
        sim_device=sim_face,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME_FACE.value,
        is_registered=True,
        device_uuid=UUID("22222222-3333-4444-5555-666666666619"),
    )
    face = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="44" * 16,
    )

    await face.connect()
    with pytest.raises(Exception):
        await face.login()


@pytest.mark.asyncio
async def test_face_passcode_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting passcodes."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    # 1. Add passcode PIN "1234"
    await face.add_passcode("1234", "Guest Key")
    await asyncio.sleep(0.05)

    assert len(sim_face.registered_passcodes) == 1
    assert face.passcodes_count == 1

    # 2. Sync passcodes database
    passcodes = await face.get_passcodes()
    assert len(passcodes) == 1
    code_id = list(passcodes.keys())[0]
    assert passcodes[code_id]["code"] == "1234"
    assert passcodes[code_id]["name"] == "Guest Key"

    # 3. Update passcode nickname
    await face.update_passcode_name("1234", "VIP Key")
    await asyncio.sleep(0.05)
    passcodes = await face.get_passcodes()
    assert passcodes[code_id]["name"] == "VIP Key"

    # 4. Delete passcode
    await face.delete_passcode("1234")
    await asyncio.sleep(0.05)
    assert len(sim_face.registered_passcodes) == 0
    assert face.passcodes_count == 0

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_card_lifecycle(mock_bleak_establish):
    """Verify adding, syncing, updating, and deleting RFID/NFC cards."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    card_hex = "010203040506"
    # 1. Add card
    await face.add_card(card_hex, "Alice Card")
    await asyncio.sleep(0.05)

    assert len(sim_face.registered_cards) == 1
    assert face.cards_count == 1

    # 2. Sync cards database
    cards = await face.get_cards()
    assert len(cards) == 1
    assert card_hex in cards
    assert cards[card_hex]["name"] == "Alice Card"

    # 3. Update card nickname
    await face.update_card_name(card_hex, "Alice Work Badge")
    await asyncio.sleep(0.05)
    cards = await face.get_cards()
    assert cards[card_hex]["name"] == "Alice Work Badge"

    # 4. Delete card
    await face.delete_card(card_hex)
    await asyncio.sleep(0.05)
    assert len(sim_face.registered_cards) == 0
    assert face.cards_count == 0

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_fingerprint_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting fingerprints."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    # Seed an enrolled fingerprint
    fp_id_bytes = bytes.fromhex("AA11BB22")
    sim_face.registered_fingerprints.append({
        "id": fp_id_bytes.hex(),
        "id_bytes": fp_id_bytes,
        "name": "Right Index",
        "type": 0,
    })
    sim_face._emit_mech_status()
    await asyncio.sleep(0.05)
    assert face.fingerprints_count == 1

    # 1. Sync fingerprints
    fps = await face.get_fingerprints()
    assert len(fps) == 1
    assert fp_id_bytes.hex() in fps
    assert fps[fp_id_bytes.hex()]["name"] == "Right Index"

    # 2. Update nickname
    await face.update_fingerprint_name(fp_id_bytes.hex(), "Left Thumb")
    await asyncio.sleep(0.05)
    fps = await face.get_fingerprints()
    assert fps[fp_id_bytes.hex()]["name"] == "Left Thumb"

    # 3. Delete fingerprint
    await face.delete_fingerprint(fp_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_face.registered_fingerprints) == 0
    assert face.fingerprints_count == 0

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_biometric_lifecycle(mock_bleak_establish):
    """Verify syncing, updating nickname, and deleting biometric face profiles."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    # Seed an enrolled face profile
    face_id_bytes = bytes.fromhex("CC33DD44")
    sim_face.registered_faces.append({
        "id": face_id_bytes.hex(),
        "id_bytes": face_id_bytes,
        "name": "Admin Face",
        "type": 0,
    })
    sim_face._emit_mech_status()
    await asyncio.sleep(0.05)
    assert face.faces_count == 1

    # 1. Sync faces
    faces = await face.get_faces()
    assert len(faces) == 1
    assert face_id_bytes.hex() in faces
    assert faces[face_id_bytes.hex()]["name"] == "Admin Face"

    # 2. Update nickname
    await face.update_face_name(face_id_bytes.hex(), "Owner Face")
    await asyncio.sleep(0.05)
    faces = await face.get_faces()
    assert faces[face_id_bytes.hex()]["name"] == "Owner Face"

    # 3. Delete face
    await face.delete_face(face_id_bytes.hex())
    await asyncio.sleep(0.05)
    assert len(sim_face.registered_faces) == 0
    assert face.faces_count == 0

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_paired_locks_management(mock_bleak_establish):
    """Verify pairing and unpairing locks to the keypad."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    target_uuid = UUID("12345678-1234-5678-1234-567812345678")
    secret = b"\x77" * 16

    # 1. Pair lock
    await face.add_paired_lock(target_uuid, secret)
    await asyncio.sleep(0.05)

    assert len(sim_face.paired_locks) == 1
    assert len(face.paired_locks) == 1
    assert face.paired_locks[0]["uuid"] == str(target_uuid)

    # 2. Unpair lock
    await face.remove_paired_lock(target_uuid)
    await asyncio.sleep(0.05)

    assert len(sim_face.paired_locks) == 0
    assert len(face.paired_locks) == 0

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_registration_modes(mock_bleak_establish):
    """Verify setting passcode, card, fingerprint, and face registration modes."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    await face.set_passcode_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face.passcode_registration_mode is True

    await face.set_card_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face.card_registration_mode is True

    await face.set_fingerprint_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face.fingerprint_registration_mode is True

    await face.set_face_registration_mode(True)
    await asyncio.sleep(0.02)
    assert sim_face.face_registration_mode is True

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_physical_matrix_valid_passcode_unlocks_linked_lock(mock_bleak_establish):
    """Verify entering enrolled PIN on physical keypad illuminates green LED, beeps, and unlocks linked lock."""
    sim_face, face = await create_connected_face(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Register passcode "4321"
    await face.add_passcode("4321", "Test PIN")
    await asyncio.sleep(0.05)

    # Simulate typing "4", "3", "2", "1", "#" on keypad
    sim_face.press_key("4")
    sim_face.press_key("3")
    sim_face.press_key("2")
    sim_face.press_key("1")
    sim_face.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_face.led_green is True
    assert sim_lock.is_locked is False

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_physical_matrix_invalid_passcode_rejected(mock_bleak_establish):
    """Verify invalid PIN entry illuminates red LED and leaves linked lock locked."""
    sim_face, face = await create_connected_face(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    # Type wrong code "9999#"
    sim_face.press_key("9")
    sim_face.press_key("9")
    sim_face.press_key("9")
    sim_face.press_key("9")
    sim_face.press_key("#")

    assert sim_face.led_red is True
    assert sim_lock.is_locked is True

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_physical_matrix_backspace_clear(mock_bleak_establish):
    """Verify '*' clears input buffer before evaluating PIN."""
    sim_face, face = await create_connected_face(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face.linked_lock = sim_lock

    # Type wrong digits, then "*", then default demo code "123456#"
    sim_face.press_key("9")
    sim_face.press_key("9")
    sim_face.press_key("*")
    assert sim_face.keypad_input == ""

    for digit in "123456":
        sim_face.press_key(digit)
    sim_face.press_key("#")
    await asyncio.sleep(0.35)

    assert sim_face.led_green is True
    assert sim_lock.is_locked is False

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_card_tap_unlocks_linked_lock(mock_bleak_establish):
    """Verify tapping enrolled NFC card unlocks linked lock."""
    sim_face, face = await create_connected_face(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face.linked_lock = sim_lock

    card_uid = "A1B2C3D4"
    await face.add_card(card_uid, "Office Badge")
    await asyncio.sleep(0.05)

    assert sim_lock.is_locked is True

    sim_face.scan_card(card_uid)
    await asyncio.sleep(0.35)

    assert sim_face.led_green is True
    assert sim_lock.is_locked is False

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_card_tap_unregistered_triggers_cloud_verification(mock_bleak_establish):
    """Verify tapping unknown NFC card triggers cloud verification broadcast."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    unknown_card = "DEADBEEF0102"
    sim_face.scan_card(unknown_card)
    await asyncio.sleep(0.05)

    assert sim_face.led_red is True
    assert face.scanned_card is not None
    assert face.scanned_card["uid"].lower() == unknown_card.lower()

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_biometric_scan_unlocks_linked_lock(mock_bleak_establish):
    """Verify face recognition scan unlocks linked lock."""
    sim_face, face = await create_connected_face(mock_bleak_establish)
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    sim_face.linked_lock = sim_lock

    assert sim_lock.is_locked is True

    sim_face.scan_face(matched=True)
    await asyncio.sleep(0.35)

    assert sim_face.led_green is True
    assert sim_lock.is_locked is False

    # Unmatched face fails
    sim_face.scan_face(matched=False)
    assert sim_face.led_red is True

    await face.disconnect()


@pytest.mark.asyncio
async def test_face_firmware_version_query(mock_bleak_establish):
    """Verify querying firmware version from Sesame Face."""
    sim_face, face = await create_connected_face(mock_bleak_establish)

    version = await face.request_firmware_version()
    assert version == "3.0-19-e877d5"
    assert face.firmware_version == "3.0-19-e877d5"

    await face.disconnect()
