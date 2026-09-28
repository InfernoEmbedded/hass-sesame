import pytest
from Crypto.PublicKey import ECC

from cryptography.hazmat.primitives.asymmetric import ec

# Import local custom component modules
from pysesame_ble.crypto import (
    SesameCipher,
    generate_ecc_keypair,
    derive_device_secret,
    derive_session_token_key,
)

# Import gomalock modules
from gomalock import os3_cipher


def test_generate_ecc_keypair_properties() -> None:
    """Verifies that generated ECC keys have the same structure and size as gomalock."""
    # 1. Custom component key generation (cryptography)
    pubkey_raw, privkey = generate_ecc_keypair()
    assert len(pubkey_raw) == 64
    assert isinstance(privkey, ec.EllipticCurvePrivateKey)
    assert privkey.curve.name == "secp256r1"

    # 2. Gomalock key generation (pycryptodome)
    g_pubkey_raw, g_privkey = os3_cipher.generate_app_keys()
    assert len(g_pubkey_raw) == 64
    assert isinstance(g_privkey, ECC.EccKey)
    assert g_privkey.curve == "NIST P-256"
    assert g_privkey.has_private()


def test_derive_device_secret_compatibility() -> None:
    """Verifies that ECDH shared secret derivation matches between custom component and gomalock."""
    # Generate keys
    app_pub_raw, app_priv = generate_ecc_keypair()
    g_pub_raw, g_priv = os3_cipher.generate_app_keys()

    # Cross-derive shared secret between cryptography and pycryptodome
    secret_custom = derive_device_secret(g_pub_raw, app_priv)
    secret_gomalock = os3_cipher.generate_device_secret_key(app_pub_raw, g_priv)

    # Assert outputs are identical
    assert len(secret_custom) == 16
    assert secret_custom == secret_gomalock


def test_derive_session_key_compatibility() -> None:
    """Verifies that session key derivation (AES-CMAC) matches gomalock's output."""
    device_secret = bytes(range(16))
    session_token = b"\x0a\x0b\x0c\x0d"

    # Derive with custom component
    session_key_custom = derive_session_token_key(device_secret, session_token)

    # Derive with gomalock
    session_key_gomalock = os3_cipher.generate_session_key(device_secret, session_token)

    assert len(session_key_custom) == 16
    assert session_key_custom == session_key_gomalock


def test_aes_ccm_cipher_roundtrip_compatibility() -> None:
    """Verifies that encrypting with SesameCipher can be decrypted by OS3Cipher, and vice versa."""
    session_token = b"\x99\x88\x77\x66"
    session_key = bytes(range(16, 32))
    plaintext = b"This is a secret message sent to Sesame lock!"

    # Initialize ciphers
    custom_cipher = SesameCipher(session_token, session_key)
    gomalock_cipher = os3_cipher.OS3Cipher(session_token, session_key)

    # 1. Custom component encrypt -> Gomalock decrypt
    ciphertext_custom = custom_cipher.encrypt_payload(plaintext)
    decrypted_gomalock = gomalock_cipher.decrypt(ciphertext_custom)
    assert decrypted_gomalock == plaintext

    # 2. Gomalock encrypt -> Custom component decrypt
    ciphertext_gomalock = gomalock_cipher.encrypt(plaintext)
    decrypted_custom = custom_cipher.decrypt_payload(ciphertext_gomalock)
    assert decrypted_custom == plaintext


def test_cipher_sequence_lockstep_compatibility() -> None:
    """Verifies that both ciphers remain compatible over multiple sequential messages."""
    session_token = b"\x12\x34\x56\x78"
    session_key = bytes(range(16))
    messages = [
        b"Unlock command PDU",
        b"Lock command PDU",
        b"Status request PDU",
        b"Another PDU message",
    ]

    custom_cipher = SesameCipher(session_token, session_key)
    gomalock_cipher = os3_cipher.OS3Cipher(session_token, session_key)

    for msg in messages:
        # Check encrypting matches in sequence
        enc_custom = custom_cipher.encrypt_payload(msg)
        enc_gomalock = gomalock_cipher.encrypt(msg)
        assert enc_custom == enc_gomalock

        # Decrypt check
        assert custom_cipher.decrypt_payload(enc_gomalock) == msg
        assert gomalock_cipher.decrypt(enc_custom) == msg
