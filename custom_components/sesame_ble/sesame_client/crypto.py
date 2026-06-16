"""Cryptographic functions for the Sesame OS3 BLE protocol.

Implements ECDH P-256 key agreement, AES-CMAC session key derivation,
and AES-CCM message encryption/decryption as defined in the official docs:
https://github.com/CANDY-HOUSE/API_document/blob/main/SesameOS3/bluetooth.md
"""

import logging

from Crypto.Cipher import AES
from Crypto.Hash import CMAC
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC

logger = logging.getLogger(__name__)

class SesameCipher:
    """Manages secure communication using AES-CCM for a Sesame BLE session."""

    def __init__(self, session_token: bytes, session_key: bytes) -> None:
        """Initialize the cipher with the session token and derived key.

        Args:
            session_token: 4-byte session token (random code).
            session_key: 16-byte session key (Token derived via AES-CMAC).
        """
        self._token = session_token
        self._key = session_key
        self._encrypt_seq = 0
        self._decrypt_seq = 0

    def _build_nonce(self, counter: int) -> bytes:
        """Constructs the 13-byte CCM_IV: 8-byte count (LE) + 1-byte nouse (0) + 4-byte token."""
        return counter.to_bytes(8, byteorder="little") + b"\x00" + self._token

    def encrypt_payload(self, plaintext: bytes) -> bytes:
        """Encrypts data using AES-CCM (13-byte IV, 1-byte AAD: 0x00, 4-byte MAC/tag)."""
        nonce = self._build_nonce(self._encrypt_seq)
        self._encrypt_seq += 1
        
        cipher = AES.new(self._key, AES.MODE_CCM, nonce=nonce, mac_len=4)
        cipher.update(b"\x00")  # AAD is 0x00
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return ciphertext + tag

    def decrypt_payload(self, ciphertext: bytes) -> bytes:
        """Decrypts and verifies data using AES-CCM."""
        nonce = self._build_nonce(self._decrypt_seq)
        self._decrypt_seq += 1
        
        try:
            cipher = AES.new(self._key, AES.MODE_CCM, nonce=nonce, mac_len=4)
            cipher.update(b"\x00")  # AAD is 0x00
            # The last 4 bytes are the MAC/tag
            return cipher.decrypt_and_verify(ciphertext[:-4], ciphertext[-4:])
        except ValueError as err:
            logger.error(
                "Decryption failed!\n  Session Key: %s\n  Nonce: %s\n  Ciphertext: %s\n  Tag: %s",
                self._key.hex(),
                nonce.hex(),
                ciphertext[:-4].hex(),
                ciphertext[-4:].hex()
            )
            raise err

def generate_ecc_keypair() -> tuple[bytes, ECC.EccKey]:
    """Generates a NIST P-256 ECC key pair for Sesame registration.

    Returns:
        A tuple of (public_key_raw_64_bytes, private_key_object).
    """
    private_key = ECC.generate(curve="NIST P-256")
    public_key_raw = private_key.public_key().export_key(format="raw")
    # Remove the 0x04 uncompressed prefix byte
    return public_key_raw[1:], private_key

def derive_device_secret(device_pubkey_raw: bytes, app_privkey: ECC.EccKey) -> bytes:
    """Derives the 16-byte device secret using ECDH.

    Args:
        device_pubkey_raw: Device's 64-byte uncompressed public key (no 0x04 prefix).
        app_privkey: The app's private ECC key.
    """
    uncompressed_pub = b"\x04" + device_pubkey_raw
    device_public_key = ECC.import_key(uncompressed_pub, curve_name="NIST P-256")
    shared_secret = key_agreement(
        static_priv=app_privkey, static_pub=device_public_key, kdf=lambda x: x
    )
    if not isinstance(shared_secret, bytes):
        shared_secret = bytes(shared_secret)
    return shared_secret[:16]

def derive_session_token_key(device_secret: bytes, session_token: bytes) -> bytes:
    """Derives the 16-byte session key (Token) by signing the random code using AES-CMAC.

    Formula: AES_CMAC(key: device_secret, input: session_token)
    """
    cmac_obj = CMAC.new(device_secret, ciphermod=AES)
    cmac_obj.update(session_token)
    return cmac_obj.digest()
