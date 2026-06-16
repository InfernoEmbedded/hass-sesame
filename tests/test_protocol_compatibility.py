import struct
from uuid import UUID
import pytest

# Import local custom component modules
from sesame_ble.sesame_client.device import (
    calculate_battery_percentage,
    SesameAdData,
    SesameQRCode,
    SesameSegmentLayer,
    SesameLock,
    SesameKeypad,
)
from sesame_ble.sesame_client.crypto import (
    generate_ecc_keypair,
    derive_device_secret,
)

# Import gomalock modules
from gomalock import os3_protocol, const, protocol_types, sesametouch, sesame5
from gomalock.os3_cipher import (
    generate_app_keys,
    generate_device_secret_key,
)


def test_battery_percentage_compatibility() -> None:
    """Verifies that the battery percentage estimation calculation is identical to gomalock."""
    # Test a wide range of battery voltages to ensure our linear interpolation is identical to gomalock's
    voltages = [
        6.0, 5.85, 5.84, 5.82, 5.80, 5.75, 5.70, 5.62, 5.50,
        5.30, 5.15, 5.05, 4.95, 4.70, 4.60, 4.50, 4.0
    ]
    for v in voltages:
        p_custom = calculate_battery_percentage(v)
        p_gomalock = os3_protocol.calculate_battery_percentage(v)
        assert p_custom == p_gomalock


def test_ad_data_compatibility() -> None:
    """Verifies that manufacturing advertisement decoding is identical to gomalock."""
    device_uuid = UUID("12345678-1234-5678-1234-567812345678")
    model_id = const.ProductModels.SESAME5.value
    registered = 1
    manufacturer_data = struct.pack("<HB16s", model_id, registered, device_uuid.bytes)

    # Decode with custom component
    ad_custom = SesameAdData.decode(manufacturer_data)
    assert ad_custom.model_id == model_id
    assert ad_custom.is_registered is True
    assert ad_custom.device_uuid == device_uuid

    # Decode with gomalock
    ad_gomalock = protocol_types.SesameAdvertisementData.from_manufacturer_data(manufacturer_data)
    assert ad_gomalock.product_model == const.ProductModels.SESAME5
    assert ad_gomalock.is_registered is True
    assert ad_gomalock.device_uuid == device_uuid


def test_qr_code_compatibility() -> None:
    """Verifies that the QR code schema and base64 payloads map correctly between both ciphers."""
    device_uuid = UUID("9abcdef0-9abc-def0-9abc-def09abcdef0")
    secret_key = bytes(range(16))
    device_name = "Living Room Sesame"

    # 1. Custom component setup URL encoding -> Gomalock URL decoding
    qr_custom = SesameQRCode(
        device_name=device_name,
        key_level=0,  # OWNER
        model_id=5,   # SESAME5
        device_uuid=device_uuid,
        secret_key=secret_key
    )
    url_custom = qr_custom.to_url()

    qr_gomalock = os3_protocol.OS3QRCode.from_qr_url(url_custom)
    assert qr_gomalock.device_name == device_name
    assert qr_gomalock.key_level == os3_protocol.KeyLevels.OWNER
    assert qr_gomalock.product_model == os3_protocol.ProductModels.SESAME5
    assert qr_gomalock.device_uuid == device_uuid
    assert qr_gomalock.secret_key == secret_key

    # 2. Gomalock URL encoding -> Custom component URL decoding
    qr_g_enc = os3_protocol.OS3QRCode(
        device_name=device_name,
        key_level=os3_protocol.KeyLevels.MANAGER,
        product_model=os3_protocol.ProductModels.SESAME_TOUCH,
        device_uuid=device_uuid,
        secret_key=secret_key
    )
    url_gomalock = qr_g_enc.qr_url

    qr_c_dec = SesameQRCode.from_url(url_gomalock)
    assert qr_c_dec.device_name == device_name
    assert qr_c_dec.key_level == 1  # MANAGER
    assert qr_c_dec.model_id == 10  # SESAME_TOUCH
    assert qr_c_dec.device_uuid == device_uuid
    assert qr_c_dec.secret_key == secret_key


def test_packet_fragmentation_compatibility() -> None:
    """Verifies that payload fragmentation produces identical segment headers and size splits."""
    # Large payload that requires fragmentation
    payload = b"A" * 35  # MTU is 20, 1 byte header, so chunk size is 19. 35 bytes -> 2 packets (19 + 16)

    # 1. Plaintext fragmentation
    segmenter = SesameSegmentLayer()
    packets_custom = segmenter.segment_payload(payload, encrypt=False)
    assert len(packets_custom) == 2
    assert packets_custom[0][0] == 0x01  # start
    assert packets_custom[0][1:] == b"A" * 19
    assert packets_custom[1][0] == 0x02  # plain end
    assert packets_custom[1][1:] == b"A" * 16

    # Verify matching gomalock headers
    assert packets_custom[0][0] == int(const.PacketTypes.BEGINNING)
    assert packets_custom[1][0] == int(const.PacketTypes.PLAINTEXT_END)

    # 2. Encrypted fragmentation
    packets_custom_enc = segmenter.segment_payload(payload, encrypt=True)
    assert len(packets_custom_enc) == 2
    assert packets_custom_enc[0][0] == 0x01  # start
    assert packets_custom_enc[0][1:] == b"A" * 19
    assert packets_custom_enc[1][0] == 0x04  # crypt end
    assert packets_custom_enc[1][1:] == b"A" * 16

    assert packets_custom_enc[0][0] == int(const.PacketTypes.BEGINNING)
    assert packets_custom_enc[1][0] == int(const.PacketTypes.ENCRYPTED_END)


def test_packet_reassembly_compatibility() -> None:
    """Verifies that segment reassembly correctly parses beginning and end sequences."""
    segmenter = SesameSegmentLayer()

    # Plain end packet sequence
    p1 = b"\x01First part of msg "
    p2 = b"\x02Second part of msg"

    res1 = segmenter.feed_packet(p1)
    assert res1 is None  # not finished yet

    res2 = segmenter.feed_packet(p2)
    assert res2 is not None
    payload, is_complete, is_encrypted = res2
    assert is_complete is True
    assert is_encrypted is False
    assert payload == b"First part of msg Second part of msg"

    # Encrypted end packet sequence
    p3 = b"\x01Encrypted part 1 "
    p4 = b"\x04Encrypted part 2"

    res3 = segmenter.feed_packet(p3)
    assert res3 is None

    res4 = segmenter.feed_packet(p4)
    assert res4 is not None
    payload, is_complete, is_encrypted = res4
    assert is_complete is True
    assert is_encrypted is True
    assert payload == b"Encrypted part 1 Encrypted part 2"


def test_mech_status_parsing_compatibility() -> None:
    """Verifies that the unpacked attributes and statuses for lock/keypad match gomalock exactly."""
    # 1. Sesame 5 Lock Mech Status
    # battery (uint16_t), target (int16_t), position (int16_t), flags (uint8_t)
    # raw_battery = 2900 (5.8V), target = 180, position = 90, flags = 0x22 (IS_IN_LOCK_RANGE | IS_BATTERY_CRITICAL)
    payload_lock = struct.pack("<HhhB", 2900, 180, 90, 0x22)

    # Custom client parsing
    mock_ad = SesameAdData(5, True, UUID("01234567-89ab-cdef-0123-456789abcdef"))
    lock = SesameLock(ble_device=None, ad_data=mock_ad)
    lock.on_published(const.ItemCodes.MECH_STATUS.value, payload_lock)

    assert lock.battery_voltage == 5.8
    assert lock.battery_percentage == calculate_battery_percentage(5.8)
    assert lock.target_angle == 180
    assert lock.current_angle == 90
    assert lock.is_locked is True
    assert lock.is_unlocked is False
    assert lock.is_moving is True
    assert lock.is_battery_critical is True

    # Gomalock Sesame5MechStatus parsing
    status_gomalock = sesame5.Sesame5MechStatus.from_payload(payload_lock)
    assert status_gomalock.battery_voltage == 5.8
    assert status_gomalock.battery_percentage == os3_protocol.calculate_battery_percentage(5.8)
    assert status_gomalock.target == 180
    assert status_gomalock.position == 90
    assert status_gomalock.is_in_lock_range is True
    assert status_gomalock.is_in_unlock_range is False
    assert status_gomalock.is_stop is False
    assert status_gomalock.is_battery_critical is True

    # 2. Sesame Touch Keypad Mech Status
    # battery (uint16_t), cards (int16_t), fingerprints (int16_t), passwords (int16_t), flags (uint8_t)
    # raw_battery = 2800 (5.6V), cards = 3, fingerprints = 12, passwords = 5, flags = 0x20 (IS_BATTERY_CRITICAL)
    payload_touch = struct.pack("<HhhhB", 2800, 3, 12, 5, 0x20)

    # Custom client parsing
    keypad = SesameKeypad(ble_device=None, ad_data=mock_ad)
    keypad.on_published(const.ItemCodes.MECH_STATUS.value, payload_touch)

    assert keypad.battery_voltage == 5.6
    assert keypad.battery_percentage == calculate_battery_percentage(5.6)
    assert keypad.cards_count == 3
    assert keypad.fingerprints_count == 12
    assert keypad.passcodes_count == 5
    assert keypad.is_battery_critical is True

    # Gomalock SesameTouchMechStatus parsing
    touch_gomalock = sesametouch.SesameTouchMechStatus.from_payload(payload_touch)
    assert touch_gomalock.battery_voltage == 5.6
    assert touch_gomalock.battery_percentage == os3_protocol.calculate_battery_percentage(5.6)
    assert touch_gomalock.cards_number == 3
    assert touch_gomalock.fingerprints_number == 12
    assert touch_gomalock.passwords_number == 5
    assert touch_gomalock.is_battery_critical is True


def test_history_tag_compatibility() -> None:
    """Verifies that the generated lock/unlock history tags match length prefixing from gomalock."""
    history_names = ["Home Assistant", "Owner Phone", "あいうえお", "a" * 30]
    for name in history_names:
        # Custom client lock/unlock payload logic:
        name_bytes_custom = name.encode("utf-8")[:20]
        payload_custom = bytes([len(name_bytes_custom)]) + name_bytes_custom

        # Gomalock lock/unlock payload logic:
        payload_gomalock = os3_protocol.create_history_tag(name)

        assert payload_custom == payload_gomalock


def test_registration_crypto_compatibility() -> None:
    """Verifies that our ECDH keypair generation and shared secret derivation are compatible with gomalock."""
    # 1. Test key generation outputs
    pub_custom, priv_custom = generate_ecc_keypair()
    assert len(pub_custom) == 64

    # 2. Test shared secret derivation compatibility using a fixed/known device public key
    # Generate a device keypair using gomalock
    dev_pub_gomalock, dev_priv_gomalock = generate_app_keys()

    # Derive secret key using custom client with the app private key and device public key
    secret_custom = derive_device_secret(dev_pub_gomalock, priv_custom)
    
    # Derive secret key using gomalock with the same keys
    secret_gomalock = generate_device_secret_key(dev_pub_gomalock, priv_custom)

    assert secret_custom == secret_gomalock
    assert len(secret_custom) == 16


def test_keypad_multiple_passcodes_parsing() -> None:
    """Verifies that SesameKeypad parses multiple packed passcodes in a single notify packet."""
    from sesame_ble.sesame_client import SesameKeypad, SesameAdData
    from uuid import UUID
    mock_ad = SesameAdData(model_id=26, is_registered=True, device_uuid=UUID("00000000-0000-0000-0000-000000000000"))
    keypad = SesameKeypad(ble_device=None, ad_data=mock_ad)
    
    pin1 = bytes([1, 4, 1, 5, 8, 4])
    name1 = '4Y/dK0 eC"L'.encode("utf-8")
    
    pin2 = bytes([1, 2, 3, 4, 5, 6])
    name2 = 'Test Code'.encode("utf-8")
    
    payload = (
        bytes([0, len(pin1)]) + pin1 + bytes([len(name1)]) + name1 +
        bytes([0, len(pin2)]) + pin2 + bytes([len(name2)]) + name2
    )
    
    keypad.on_published(128, b"") # ITEM_PASSCODE_FIRST
    keypad.on_published(126, payload) # ITEM_PASSCODE_NOTIFY
    keypad.on_published(127, b"") # ITEM_PASSCODE_LAST
    
    assert len(keypad.passcodes) == 2
    assert keypad.passcodes[pin1.hex()]["name"] == '4Y/dK0 eC"L'
    assert keypad.passcodes[pin1.hex()]["code"] == "141584"
    assert keypad.passcodes[pin2.hex()]["name"] == "Test Code"
    assert keypad.passcodes[pin2.hex()]["code"] == "123456"


