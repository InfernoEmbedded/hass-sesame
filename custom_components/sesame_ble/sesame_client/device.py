"""BLE transport, protocol state machine, and device classes for Candy House Sesame.

Based strictly on the official documentation:
https://github.com/CANDY-HOUSE/API_document
"""

import asyncio
import logging
import random
import struct
import time
import base64
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Self
from urllib import parse
from uuid import UUID

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakDeviceNotFoundError

from .crypto import SesameCipher, derive_session_token_key, generate_ecc_keypair, derive_device_secret

logger = logging.getLogger(__name__)

# --- Protocol Constants ---
SERVICE_UUID = "0000fd81-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "16860002-a5ae-9856-b6d3-dbb4c676993e"
RX_CHAR_UUID = "16860003-a5ae-9856-b6d3-dbb4c676993e"
COMPANY_ID = 0x055A
MTU_SIZE = 20

# Operation Codes
OP_RESPONSE = 0x07
OP_PUBLISH = 0x08

# Protocol Item Codes
ITEM_REGISTRATION = 1
ITEM_LOGIN = 2
ITEM_INITIAL = 14
ITEM_AUTOLOCK = 11
ITEM_MECH_SETTING = 80
ITEM_MECH_STATUS = 81
ITEM_LOCK = 82
ITEM_UNLOCK = 83
ITEM_HISTORY = 4
ITEM_HISTORY_DELETE = 18
ITEM_KEYPAD_LOCK_LIST = 102
ITEM_ADD_SESAME = 101
ITEM_REMOVE_SESAME = 103

# Touch Keypad Passcode Item Codes
ITEM_PASSCODE_CHANGE = 123
ITEM_PASSCODE_DELETE = 124
ITEM_PASSCODE_GET = 125
ITEM_PASSCODE_NOTIFY = 126
ITEM_PASSCODE_LAST = 127
ITEM_PASSCODE_FIRST = 128
ITEM_PASSCODE_ADD = 138

# Touch Keypad Card Item Codes
ITEM_CARD_CHANGE = 107
ITEM_CARD_DELETE = 108
ITEM_CARD_GET = 109
ITEM_CARD_NOTIFY = 110
ITEM_CARD_LAST = 111
ITEM_CARD_FIRST = 112
ITEM_CARD_MODE_SET = 114
ITEM_CARD_ADD = 140

# Touch Keypad Fingerprint Item Codes
ITEM_FINGER_CHANGE = 115
ITEM_FINGER_DELETE = 116
ITEM_FINGER_GET = 117
ITEM_FINGER_NOTIFY = 118
ITEM_FINGER_LAST = 119
ITEM_FINGER_FIRST = 120
ITEM_FINGER_MODE_SET = 122

# Product Models
class ProductModels(IntEnum):
    SESAME5 = 5
    SESAME5_PRO = 7
    SESAME_TOUCH_PRO = 9
    SESAME_TOUCH = 10
    SESAME5_USA = 16
    SESAME_TOUCH_2_PRO = 26

MODEL_SESAME5 = ProductModels.SESAME5
MODEL_SESAME5_PRO = ProductModels.SESAME5_PRO
MODEL_SESAME_TOUCH_PRO = ProductModels.SESAME_TOUCH_PRO
MODEL_SESAME_TOUCH = ProductModels.SESAME_TOUCH
MODEL_SESAME5_USA = ProductModels.SESAME5_USA
MODEL_SESAME_TOUCH_2_PRO = ProductModels.SESAME_TOUCH_2_PRO


VOLTAGE_LEVELS = (
    5.85, 5.82, 5.79, 5.76, 5.73, 5.70, 5.65, 5.60,
    5.55, 5.50, 5.40, 5.20, 5.10, 5.0, 4.8, 4.6
)
BATTERY_PERCENTAGES = (
    100.0, 95.0, 90.0, 85.0, 80.0, 70.0, 60.0, 50.0,
    40.0, 32.0, 21.0, 13.0, 10.0, 7.0, 3.0, 0.0
)

def calculate_battery_percentage(voltage: float) -> int:
    """Calculates estimated battery capacity from voltage using linear interpolation."""
    if voltage >= VOLTAGE_LEVELS[0]:
        return int(BATTERY_PERCENTAGES[0])
    if voltage <= VOLTAGE_LEVELS[-1]:
        return int(BATTERY_PERCENTAGES[-1])
    for i in range(len(VOLTAGE_LEVELS) - 1):
        upper_v = VOLTAGE_LEVELS[i]
        lower_v = VOLTAGE_LEVELS[i + 1]
        if lower_v < voltage <= upper_v:
            ratio = (voltage - lower_v) / (upper_v - lower_v)
            upper_p = BATTERY_PERCENTAGES[i]
            lower_p = BATTERY_PERCENTAGES[i + 1]
            return int((upper_p - lower_p) * ratio + lower_p)
    return 0

@dataclass(frozen=True)
class SesameAdData:
    """Decoded manufacturer data from a Sesame BLE advertisement."""
    model_id: int
    is_registered: bool
    device_uuid: UUID

    @classmethod
    def decode(cls, manufacturer_data: bytes) -> Self:
        """Parses raw advertisement bytes (<HB16s)."""
        model_val, registered_val, uuid_bytes = struct.unpack("<HB16s", manufacturer_data)
        return cls(
            model_id=model_val,
            is_registered=bool(registered_val),
            device_uuid=UUID(bytes=uuid_bytes),
        )

@dataclass(frozen=True)
class SesameQRCode:
    """Decodes and encodes setup QR codes for Sesame devices."""
    device_name: str
    key_level: int
    model_id: int
    device_uuid: UUID
    secret_key: bytes

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Parses a ssm:// setup URL from the Candy House app."""
        query = parse.parse_qs(parse.urlparse(url).query)
        key_level = int(query.get("l", ["0"])[0])
        device_name = query.get("n", [""])[0]
        
        shared_key = base64.b64decode(query.get("sk", [""])[0])
        # Format: >B16s4s2s16s (Model ID, secret key, token placeholder, index placeholder, UUID bytes)
        model_id, secret_key, _, _, uuid_bytes = struct.unpack(">B16s4s2s16s", shared_key)
        
        return cls(
            device_name=device_name,
            key_level=key_level,
            model_id=model_id,
            device_uuid=UUID(bytes=uuid_bytes),
            secret_key=secret_key,
        )

    def to_url(self) -> str:
        """Generates a ssm:// setup URL compatible with the official app."""
        shared_key = struct.pack(
            ">B16s4s2s16s",
            self.model_id,
            self.secret_key,
            bytes(4),
            bytes(2),
            self.device_uuid.bytes,
        )
        sk_b64 = base64.b64encode(shared_key).decode("ascii")
        params = parse.urlencode(
            {
                "t": "sk",
                "sk": sk_b64,
                "l": self.key_level,
                "n": self.device_name,
            },
            quote_via=parse.quote,
        )
        return f"ssm://UI?{params}"

class SesameSegmentLayer:
    """Handles GATT packet fragmentation and reassembly."""

    def __init__(self) -> None:
        self._rx_buffer = bytearray()

    def feed_packet(self, data: bytes) -> tuple[bytes, bool, bool] | None:
        """Processes an incoming notification chunk.

        Returns:
            A tuple of (message_payload, is_complete, is_encrypted) or None if incomplete.
        """
        if not data:
            return None
        
        header = data[0]
        payload = data[1:]

        is_start = bool(header & 0x01)
        is_plain_end = bool(header & 0x02)
        is_crypt_end = bool(header & 0x04)
        is_end = is_plain_end or is_crypt_end

        if is_start:
            self._rx_buffer = bytearray()
        
        self._rx_buffer.extend(payload)

        if is_end:
            assembled = bytes(self._rx_buffer)
            self._rx_buffer = bytearray()
            return assembled, True, is_crypt_end

        return None

    def segment_payload(self, payload: bytes, encrypt: bool) -> list[bytes]:
        """Fragments a message into MTU-sized packets with 1-byte headers."""
        packets = []
        chunk_size = MTU_SIZE - 1  # 19 bytes payload max
        total_len = len(payload)

        for i in range(0, total_len, chunk_size):
            chunk = payload[i : i + chunk_size]
            is_start = (i == 0)
            is_end = (i + chunk_size >= total_len)

            header = 0
            if is_start:
                header |= 0x01
            if is_end:
                header |= (0x04 if encrypt else 0x02)

            packets.append(bytes([header]) + chunk)

        return packets

class SesameDevice:
    """Coordinates the BLE connection, session login, and command loop."""

    def __init__(
        self,
        ble_device,
        ad_data: SesameAdData,
        secret_key: str | None = None,
        status_callback: Callable[[object, object], None] | None = None,
        reconnect_attempts: int = 0,
    ) -> None:
        self._ble_device = ble_device
        self._secret = bytes.fromhex(secret_key) if secret_key else None
        self._reconnect_limit = reconnect_attempts
        self._status_cb = status_callback

        self._segmenter = SesameSegmentLayer()
        self._client = None
        self._cipher = None
        self._reconnect_task = None

        self._login_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        
        self._token_future = None
        self._pending_responses = {}
        self.is_logged_in = False
        self.mech_status = None

    @property
    def address(self) -> str:
        """The BLE MAC address."""
        return self._ble_device.address

    @property
    def mac_address(self) -> str:
        """The BLE MAC address for compatibility."""
        return self.address


    @property
    def is_connected(self) -> bool:
        """Return True if BLE client is actively connected."""
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        """Connects and starts notifications, waiting for the initial session token."""
        if self._reconnect_task and not self._reconnect_task.done():
            if asyncio.current_task() is not self._reconnect_task:
                raise Exception("Cannot connect while auto-reconnecting")

        if self.is_connected:
            raise Exception("Already connected")

        logger.info("Connecting to Sesame [address=%s]", self.address)
        self._token_future = asyncio.get_running_loop().create_future()

        try:
            from bleak_retry_connector import establish_connection
            self._client = await establish_connection(
                BleakClient,
                self._ble_device,
                "sesame_ble",
                disconnected_callback=self._on_disconnect,
            )
        except (ModuleNotFoundError, ImportError):
            self._client = BleakClient(self._ble_device, disconnected_callback=self._on_disconnect)
            try:
                await self._client.connect()
            except BleakDeviceNotFoundError as e:
                raise Exception("BLE device not found") from e
        except Exception as e:
            raise Exception(f"Connection failed: {e}") from e

        try:
            await self._client.start_notify(RX_CHAR_UUID, self._on_gatt_notification)
            logger.debug("Waiting for session token publish")
            if self._token_future is None:
                raise Exception("Disconnected before token received")
            await asyncio.wait_for(self._token_future, timeout=15.0)
        except Exception:
            await self.disconnect()
            raise

    async def register(self) -> str:
        """Performs cryptographic registration handshake and returns derived secret key hex."""
        if not self._token_future or not self._token_future.done():
            raise Exception("Session token not received yet")

        app_pub, app_priv = generate_ecc_keypair()
        timestamp = int(time.time()).to_bytes(4, "little")

        try:
            response = await self.send_command(
                ITEM_REGISTRATION, app_pub + timestamp, encrypt=False
            )
            device_pub = response[-64:]
            secret_key = derive_device_secret(device_pub, app_priv)
            return secret_key.hex()
        except Exception:
            self._reset_state()
            raise

    async def login(self) -> int:
        """Performs cryptographic login handshake."""
        if self.is_logged_in:
            raise Exception("Already logged in")
        if not self._secret:
            raise Exception("Secret key is missing")
        if not self._token_future or not self._token_future.done():
            raise Exception("Session token not received yet")

        token = self._token_future.result()
        session_key = derive_session_token_key(self._secret, token)
        self._cipher = SesameCipher(token, session_key)

        try:
            # Login payload is first 4 bytes of session key
            response = await self.send_command(ITEM_LOGIN, session_key[:4], encrypt=False)
            device_time = int.from_bytes(response, "little")
            self.is_logged_in = True
            logger.info("Logged in successfully. Device time: %d", device_time)
            
            # Wait for mechanical settings/status to be published to complete login
            try:
                await asyncio.wait_for(self._login_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for initial mechanical settings/status publish")

            if self._status_cb:
                self._status_cb(self, self)

            return device_time
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Gracefully disconnects and resets state."""
        self._reset_state()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            finally:
                self._client = None

    def _reset_state(self) -> None:
        was_logged_in = self.is_logged_in
        self.is_logged_in = False
        self._cipher = None
        self._token_future = None
        self.mech_status = None
        self._login_event.clear()
        
        for fut in list(self._pending_responses.values()):
            if not fut.done():
                fut.cancel()
        self._pending_responses.clear()
        self._segmenter = SesameSegmentLayer()

        if was_logged_in and self._status_cb:
            self._status_cb(self, self)

    def _on_disconnect(self, client: BleakClient) -> None:
        del client
        logger.warning("Sesame BLE disconnected unexpectedly [address=%s]", self.address)
        self._reset_state()
        if self._reconnect_limit and (not self._reconnect_task or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._auto_reconnect())

    async def _auto_reconnect(self) -> None:
        for attempt in range(self._reconnect_limit):
            delay = min(2**attempt + random.random(), 30.0)
            logger.info("Scheduling reconnection attempt %d/%d in %.1fs", attempt + 1, self._reconnect_limit, delay)
            await asyncio.sleep(delay)
            try:
                await self.connect()
                await self.login()
                logger.info("Auto-reconnection successful [address=%s]", self.address)
                return
            except Exception as e:
                logger.warning("Reconnection attempt %d failed: %s", attempt + 1, e)
                await self.disconnect()
        logger.error("Auto-reconnection limit reached. Connection abandoned.")

    def _on_gatt_notification(self, characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        del characteristic
        logger.info("Received raw notification: %s", data.hex())
        res = self._segmenter.feed_packet(bytes(data))
        if not res:
            return

        payload, _, is_encrypted = res
        logger.info("Feed packet result: payload=%s, is_encrypted=%s", payload.hex(), is_encrypted)
        if is_encrypted:
            if not self._cipher:
                logger.warning("Received encrypted message before login cipher was configured")
                return
            logger.info("Session key used: %s", self._cipher._key.hex())
            payload = self._cipher.decrypt_payload(payload)
            logger.info("Decrypted payload: %s", payload.hex())

        if not payload:
            return

        op_code = payload[0]
        body = payload[1:]

        if op_code == OP_RESPONSE:
            if len(body) >= 2:
                item_code = body[0]
                result_code = body[1]
                response_payload = body[2:]
                
                fut = self._pending_responses.pop(item_code, None)
                if fut and not fut.done():
                    if result_code == 0:
                        fut.set_result(response_payload)
                    else:
                        fut.set_exception(Exception(f"Command error: {result_code}"))
        elif op_code == OP_PUBLISH:
            if len(body) >= 1:
                item_code = body[0]
                publish_payload = body[1:]
                
                if item_code == ITEM_INITIAL:
                    logger.info("Received ITEM_INITIAL token payload: %s (len=%d)", publish_payload.hex(), len(publish_payload))
                    if self._token_future and not self._token_future.done():
                        self._token_future.set_result(publish_payload)
                else:
                    self.on_published(item_code, publish_payload)

    def on_published(self, item_code: int, payload: bytes) -> None:
        """To be overridden by subclasses."""
        pass

    async def send_command(
        self,
        item_code: int,
        payload: bytes,
        encrypt: bool = True,
        wait_for_response: bool = True,
    ) -> bytes:
        """Sends a structured request and awaits the result response."""
        async with self._send_lock:
            # Build request payload: [item_code] + [payload]
            msg = bytes([item_code]) + payload
            
            if encrypt:
                if not self._cipher:
                    raise Exception("Not logged in (cipher is missing)")
                msg = self._cipher.encrypt_payload(msg)

            # In Sesame OS3 protocol, the segment header defines encryption.
            # The transmission payload should be just msg ([item_code] + [payload]).
            transmission = msg

            if not wait_for_response:
                packets = self._segmenter.segment_payload(transmission, encrypt)
                for packet in packets:
                    await self._client.write_gatt_char(TX_CHAR_UUID, packet, response=False)
                return b""

            fut = asyncio.get_running_loop().create_future()
            self._pending_responses[item_code] = fut

            try:
                packets = self._segmenter.segment_payload(transmission, encrypt)
                for packet in packets:
                    await self._client.write_gatt_char(TX_CHAR_UUID, packet, response=False)

                return await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.CancelledError:
                self._pending_responses.pop(item_code, None)
                raise Exception("Command cancelled: connection lost")
            except Exception:
                self._pending_responses.pop(item_code, None)
                raise

class SesameLock(SesameDevice):
    """Subclass representing a Sesame 5 or Sesame 5 Pro lock."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.battery_voltage = None
        self.battery_percentage = None
        self.target_angle = None
        self.current_angle = None
        self.is_locked = None
        self.is_unlocked = None
        self.is_moving = None
        self.is_battery_critical = None
        self.lock_position = None
        self.unlock_position = None
        self.auto_lock_second = None


    def on_published(self, item_code: int, payload: bytes) -> None:
        if item_code == ITEM_MECH_STATUS:
            # Struct: battery (uint16_t), target (int16_t), position (int16_t), flags (uint8_t)
            raw_battery, target, position, flags = struct.unpack("<HhhB", payload)
            
            self.battery_voltage = raw_battery * 2 / 1000
            self.battery_percentage = calculate_battery_percentage(self.battery_voltage)
            self.target_angle = target
            self.current_angle = position
            
            self.is_locked = bool(flags & 0x02)
            self.is_unlocked = bool(flags & 0x04)
            self.is_moving = not bool(flags & 0x10)
            self.is_battery_critical = bool(flags & 0x20)
            
            self.mech_status = self

            if self._status_cb:
                self._status_cb(self, self)

        elif item_code == ITEM_MECH_SETTING:
            if len(payload) >= 4:
                lock_pos, unlock_pos = struct.unpack("<hh", payload[0:4])
                self.lock_position = lock_pos
                self.unlock_position = unlock_pos
            if len(payload) >= 6:
                self.auto_lock_second = struct.unpack("<H", payload[4:6])[0]
            # We must receive mech setting to complete registration/login flow event
            if not self._login_event.is_set():
                self._login_event.set()

            if self._status_cb:
                self._status_cb(self, self)

    async def lock(self, history_name: str = "Home Assistant") -> None:
        """Locks the Sesame device."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        name_bytes = history_name.encode("utf-8")[:20]
        payload = bytes([len(name_bytes)]) + name_bytes
        await self.send_command(ITEM_LOCK, payload, encrypt=True)

    async def unlock(self, history_name: str = "Home Assistant") -> None:
        """Unlocks the Sesame device."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        name_bytes = history_name.encode("utf-8")[:20]
        payload = bytes([len(name_bytes)]) + name_bytes
        await self.send_command(ITEM_UNLOCK, payload, encrypt=True)

    async def configure_lock_position(self, lock_position: int, unlock_position: int) -> None:
        """Configures the lock and unlock angle thresholds."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        payload = struct.pack("<hh", lock_position, unlock_position)
        logger.info(
            "Configuring lock positions [address=%s, lock=%d, unlock=%d]",
            self.address,
            lock_position,
            unlock_position,
        )
        await self.send_command(ITEM_MECH_SETTING, payload, encrypt=True)

    async def set_auto_lock_second(self, second: int) -> None:
        """Configures the automatic locking timer in seconds."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        payload = struct.pack("<H", second)
        logger.info(
            "Configuring auto lock duration [address=%s, seconds=%d]",
            self.address,
            second,
        )
        await self.send_command(ITEM_AUTOLOCK, payload, encrypt=True)

    async def fetch_and_flush_history(self) -> list[dict]:
        """Fetch and flush all available history records from the lock.
        
        Reads the oldest history record, parses it, deletes it from the lock,
        and repeats until the lock has no more history records.
        """
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        records = []
        while True:
            try:
                # 0x01: Read oldest history record without deleting
                payload = await self.send_command(ITEM_HISTORY, b"\x01", encrypt=True)
                if len(payload) < 48:
                    logger.warning("Received invalid history payload length: %d", len(payload))
                    break

                # Parse history record
                # ssm_history: id(4B) + type(1B) + ts(4B) + mech_status(7B) + param(32B) = 48B
                record_id, history_type, ts, mech_status, param_bytes = struct.unpack("<IBI7s32s", payload)

                # Parse parameter/tag
                tag = struct.unpack("<H", param_bytes[0:2])[0]
                data_length = min(max(0, param_bytes[2]), 29)
                raw_val = param_bytes[3 : 3 + data_length]

                # Store the record details
                record = {
                    "record_id": record_id,
                    "type": history_type,
                    "timestamp": ts,
                    "tag": tag,
                    "raw_parameter": raw_val.hex(),
                }
                records.append(record)

                # Delete the record from the lock to advance the queue
                await self.send_command(ITEM_HISTORY_DELETE, payload[0:4], encrypt=True)

            except Exception as e:
                # Command error: 5 means CMD_RESULT_NOT_FOUND (queue empty)
                if "Command error: 5" in str(e):
                    logger.debug("History queue is empty (finished fetching)")
                    break
                else:
                    logger.exception("Error during history fetch/flush loop: %s", e)
                    raise e

        return records

class BaseKeypad:
    """Generic interface/base class representing a keypad device with passcode/card/fingerprint management."""

    def __init__(self) -> None:
        self.passcodes = {}
        self.cards = {}
        self.fingerprints = {}
        self.cards_count = 0
        self.fingerprints_count = 0
        self.passcodes_count = 0

    async def get_passcodes(self) -> dict[str, dict]:
        """Fetch/sync passcodes from the keypad."""
        raise NotImplementedError()

    async def add_passcode(self, code: str, name: str) -> None:
        """Add a new passcode to the keypad."""
        raise NotImplementedError()

    async def delete_passcode(self, code_or_id: str) -> None:
        """Delete an existing passcode from the keypad."""
        raise NotImplementedError()

    async def update_passcode_name(self, code_or_id: str, name: str) -> None:
        """Update the nickname of an existing passcode on the keypad."""
        raise NotImplementedError()

    async def get_cards(self) -> dict[str, dict]:
        """Fetch/sync NFC cards from the keypad."""
        raise NotImplementedError()

    async def delete_card(self, card_id: str) -> None:
        """Delete a card from the keypad."""
        raise NotImplementedError()

    async def update_card_name(self, card_id: str, name: str) -> None:
        """Update the nickname of an existing card on the keypad."""
        raise NotImplementedError()

    async def get_fingerprints(self) -> dict[str, dict]:
        """Fetch/sync fingerprints from the keypad."""
        raise NotImplementedError()

    async def delete_fingerprint(self, finger_id: str) -> None:
        """Delete a fingerprint from the keypad."""
        raise NotImplementedError()

    async def update_fingerprint_name(self, finger_id: str, name: str) -> None:
        """Update the nickname of an existing fingerprint on the keypad."""
        raise NotImplementedError()


class SesameKeypad(SesameDevice, BaseKeypad):
    """Subclass representing a Sesame Touch or Sesame Touch Pro keypad."""

    def __init__(
        self,
        ble_device,
        ad_data: SesameAdData,
        secret_key: str | None = None,
        status_callback: Callable[[object, object], None] | None = None,
        reconnect_attempts: int = 0,
    ) -> None:
        SesameDevice.__init__(self, ble_device, ad_data, secret_key, status_callback, reconnect_attempts)
        BaseKeypad.__init__(self)
        self._temp_passcodes = {}
        self._temp_cards = {}
        self._temp_fingerprints = {}
        self._sync_future = None
        self.scanned_fingerprint = None
        self.paired_locks = []
        self.card_registration_mode = False
        self.fingerprint_registration_mode = False

        self.battery_voltage = None
        self.battery_percentage = None
        self.is_battery_critical = None

    def on_published(self, item_code: int, payload: bytes) -> None:
        if item_code == ITEM_MECH_STATUS:
            # Struct varies by payload length:
            # - 7 bytes: battery (uint16_t), cards (int16_t), fingerprints (uint8_t), passwords (uint8_t), flags (uint8_t)
            # - 9 bytes: battery (uint16_t), cards (int16_t), fingerprints (uint8_t), passwords (uint8_t), faces (uint8_t), palms (uint8_t), flags (uint8_t)
            if len(payload) == 7:
                raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhBBB", payload)
            elif len(payload) == 9:
                raw_battery, cards, fingerprints, passwords, _faces, _palms, flags = struct.unpack("<HhBBBBB", payload)
            else:
                # Fallback to legacy format if size is unexpected
                raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhhhB", payload[:9])
            
            self.battery_voltage = raw_battery * 2 / 1000
            self.battery_percentage = calculate_battery_percentage(self.battery_voltage)
            self.cards_count = cards
            self.fingerprints_count = fingerprints
            self.passcodes_count = passwords
            self.is_battery_critical = bool(flags & 0x20)
            
            self.mech_status = self
            if not self._login_event.is_set():
                self._login_event.set()

            if self._status_cb:
                self._status_cb(self, self)

        elif item_code == ITEM_PASSCODE_FIRST:
            self._temp_passcodes = {}
            logger.debug("Passcode sync database started")
        
        elif item_code == ITEM_PASSCODE_NOTIFY:
            parsed = self._parse_notify_payload(payload, is_passcode=True)
            self._temp_passcodes.update(parsed)
        
        elif item_code == ITEM_PASSCODE_LAST:
            self.passcodes = self._temp_passcodes
            logger.debug("Passcode sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)

        elif item_code == ITEM_CARD_FIRST:
            self._temp_cards = {}
            logger.debug("Card sync database started")

        elif item_code == ITEM_CARD_NOTIFY:
            parsed = self._parse_notify_payload(payload, is_passcode=False)
            self._temp_cards.update(parsed)
            logger.info("ITEM_CARD_NOTIFY received: parsed=%s, sync_future=%s, card_reg_mode=%s", parsed, self._sync_future, getattr(self, "card_registration_mode", False))
            for uid, info in parsed.items():
                is_new = uid not in self.cards
                logger.info("Checking card notify: uid=%s, is_new=%s, existing_cards=%s", uid, is_new, list(self.cards.keys()))
                if self._sync_future is None or (getattr(self, "card_registration_mode", False) and is_new):
                    self.scanned_card = {
                        "uid": uid,
                        "type": info["type"]
                    }
                    logger.info("Captured scanned card: %s (is_new=%s)", self.scanned_card, is_new)
                    if self._status_cb:
                        self._status_cb(self, self)
                else:
                    logger.info("Skipped scanned card capture. sync_future=%s, card_reg_mode=%s, is_new=%s", self._sync_future, getattr(self, "card_registration_mode", False), is_new)

        elif item_code == ITEM_CARD_LAST:
            self.cards = self._temp_cards
            logger.debug("Card sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)

        elif item_code == ITEM_FINGER_FIRST:
            self._temp_fingerprints = {}
            logger.debug("Fingerprint sync database started")

        elif item_code == ITEM_FINGER_NOTIFY:
            parsed = self._parse_notify_payload(payload, is_passcode=False)
            self._temp_fingerprints.update(parsed)
            logger.info("ITEM_FINGER_NOTIFY received: parsed=%s, sync_future=%s, fp_reg_mode=%s", parsed, self._sync_future, getattr(self, "fingerprint_registration_mode", False))
            for uid, info in parsed.items():
                is_new = uid not in self.fingerprints
                logger.info("Checking fingerprint notify: uid=%s, is_new=%s, existing_fps=%s", uid, is_new, list(self.fingerprints.keys()))
                if self._sync_future is None or (getattr(self, "fingerprint_registration_mode", False) and is_new):
                    self.scanned_fingerprint = {
                        "uid": uid,
                        "type": info["type"]
                    }
                    logger.info("Captured scanned fingerprint: %s (is_new=%s)", self.scanned_fingerprint, is_new)
                    if self._status_cb:
                        self._status_cb(self, self)
                else:
                    logger.info("Skipped scanned fingerprint capture. sync_future=%s, fp_reg_mode=%s, is_new=%s", self._sync_future, getattr(self, "fingerprint_registration_mode", False), is_new)

        elif item_code == ITEM_FINGER_LAST:
            self.fingerprints = self._temp_fingerprints
            logger.debug("Fingerprint sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)

        elif item_code == ITEM_KEYPAD_LOCK_LIST:
            self._parse_paired_locks(payload)

    async def get_passcodes(self) -> dict[str, dict]:
        """Syncs the passcode database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "passcodes_count", 0) == 0:
            self.passcodes = {}
            return self.passcodes

        self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_passcodes = {}

        try:
            await self.send_command(ITEM_PASSCODE_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(self._sync_future, timeout=15.0)
            return self.passcodes
        finally:
            self._sync_future = None

    def _resolve_code(self, code_or_id: str) -> bytes:
        """Resolves raw digits (e.g. '1234') or Hex ID string (e.g. '01020304') to bytes."""
        if code_or_id in self.passcodes:
            return bytes.fromhex(code_or_id)

        # Hex heuristic: even length, min 8, and format matches '0X0X0X...'
        if (
            len(code_or_id) % 2 == 0
            and len(code_or_id) >= 8
            and all(code_or_id[i] == "0" for i in range(0, len(code_or_id), 2))
            and all(code_or_id[i].isdigit() for i in range(1, len(code_or_id), 2))
        ):
            return bytes.fromhex(code_or_id)

        if code_or_id.isdigit():
            return bytes(int(c) for c in code_or_id)

        raise ValueError(f"Invalid passcode formatting: {code_or_id}")

    async def add_passcode(self, code: str, name: str) -> None:
        """Adds a passcode PIN to the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        id_bytes = self._resolve_code(code)
        if len(id_bytes) < 4 or len(id_bytes) > 16:
            raise ValueError("Passcode must be between 4 and 16 digits")

        name_bytes = name.encode("utf-8")[:20]

        payload = bytearray(40)
        payload[0] = 0xF0  # KB_DATA_USED
        payload[1] = 0x00  # KB_TYPE_LOCAL
        payload[2] = len(id_bytes)
        payload[3 : 3 + len(id_bytes)] = id_bytes
        payload[19] = len(name_bytes)
        payload[20 : 20 + len(name_bytes)] = name_bytes

        await self.send_command(ITEM_PASSCODE_ADD, bytes(payload), encrypt=True)

    async def delete_passcode(self, code_or_id: str) -> None:
        """Deletes a passcode PIN or Hex ID from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        id_bytes = self._resolve_code(code_or_id)
        await self.send_command(ITEM_PASSCODE_DELETE, id_bytes, encrypt=True)

    async def update_passcode_name(self, code_or_id: str, name: str) -> None:
        """Updates the nickname of an existing passcode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        id_bytes = self._resolve_code(code_or_id)
        name_bytes = name.encode("utf-8")[:20]

        payload = bytes([len(id_bytes)]) + id_bytes + name_bytes
        await self.send_command(ITEM_PASSCODE_CHANGE, payload, encrypt=True)

    def _parse_notify_payload(self, payload: bytes, is_passcode: bool = False) -> dict[str, dict]:
        results = {}
        idx = 0
        while idx + 3 <= len(payload):
            item_type = payload[idx]
            id_len = payload[idx + 1]
            if idx + 2 + id_len > len(payload):
                break
            item_id = payload[idx + 2 : idx + 2 + id_len]
            name_len = payload[idx + 2 + id_len]
            if idx + 2 + id_len + 1 + name_len > len(payload):
                break
            name_bytes = payload[idx + 2 + id_len + 1 : idx + 2 + id_len + 1 + name_len]
            logger.info("Parsed notify item: type=%d, id=%s, name_len=%d, raw_name_bytes=%s", item_type, item_id.hex(), name_len, name_bytes.hex())
            
            # The Candyhouse official app uses a random UUIDv4 in the name field 
            # to link the passcode to the actual user name stored in their cloud DB.
            name = None
            if len(name_bytes) == 16:
                try:
                    val = UUID(bytes=name_bytes)
                    if val.version == 4:
                        name = f"App Code ({str(val)[:8]})"
                except ValueError:
                    pass
                    
            if name is None:
                name = name_bytes.decode("utf-8", errors="replace").rstrip("\x00")

            if is_passcode:
                code_str = "".join(str(b) for b in item_id)
            else:
                code_str = item_id.hex()

            results[item_id.hex()] = {
                "name": name,
                "code": code_str,
                "type": item_type,
            }
            idx += 2 + id_len + 1 + name_len
        return results

    async def get_cards(self) -> dict[str, dict]:
        """Syncs the card database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "cards_count", 0) == 0:
            self.cards = {}
            return self.cards

        self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_cards = {}

        try:
            await self.send_command(ITEM_CARD_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(self._sync_future, timeout=15.0)
            return self.cards
        finally:
            self._sync_future = None

    async def delete_card(self, card_id: str) -> None:
        """Deletes a card from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        await self.send_command(ITEM_CARD_DELETE, bytes.fromhex(card_id), encrypt=True)

    async def update_card_name(self, card_id: str, name: str) -> None:
        """Updates the nickname of an existing card."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        id_bytes = bytes.fromhex(card_id)
        name_bytes = name.encode("utf-8")[:20]
        payload = bytes([len(id_bytes)]) + id_bytes + name_bytes
        await self.send_command(ITEM_CARD_CHANGE, payload, encrypt=True)

    async def get_fingerprints(self) -> dict[str, dict]:
        """Syncs the fingerprint database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "fingerprints_count", 0) == 0:
            self.fingerprints = {}
            return self.fingerprints

        self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_fingerprints = {}

        try:
            await self.send_command(ITEM_FINGER_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(self._sync_future, timeout=15.0)
            return self.fingerprints
        finally:
            self._sync_future = None

    async def delete_fingerprint(self, finger_id: str) -> None:
        """Deletes a fingerprint from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        await self.send_command(ITEM_FINGER_DELETE, bytes.fromhex(finger_id), encrypt=True)

    async def update_fingerprint_name(self, finger_id: str, name: str) -> None:
        """Updates the nickname of an existing fingerprint."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        id_bytes = bytes.fromhex(finger_id)
        name_bytes = name.encode("utf-8")[:20]
        payload = bytes([len(id_bytes)]) + id_bytes + name_bytes
        await self.send_command(ITEM_FINGER_CHANGE, payload, encrypt=True)

    async def set_card_registration_mode(self, active: bool) -> None:
        """Sets the keypad card mode: True for Add Mode, False for Verification Mode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        mode = 0x01 if active else 0x00
        await self.send_command(ITEM_CARD_MODE_SET, bytes([mode]), encrypt=True)
        self.card_registration_mode = active

    async def set_fingerprint_registration_mode(self, active: bool) -> None:
        """Sets the keypad fingerprint mode: True for Add Mode, False for Verification Mode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        mode = 0x01 if active else 0x00
        await self.send_command(ITEM_FINGER_MODE_SET, bytes([mode]), encrypt=True)
        self.fingerprint_registration_mode = active

    async def add_card(self, card_id: str, name: str, card_type: int = 0x80) -> None:
        """Registers/Adds a card to the keypad database."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
            
        card_id_bytes = bytes.fromhex(card_id)
        card_name_bytes = name.encode("utf-8")[:20]
        
        payload = bytearray(40)
        payload[0] = 0xF0  # CARD_DATA_USED
        payload[1] = card_type
        payload[2] = len(card_id_bytes)
        payload[3 : 3 + len(card_id_bytes)] = card_id_bytes
        payload[19] = len(card_name_bytes)
        payload[20 : 20 + len(card_name_bytes)] = card_name_bytes
        
        await self.send_command(ITEM_CARD_ADD, bytes(payload), encrypt=True)

    def _parse_paired_locks(self, payload: bytes) -> None:
        """Parses the list of paired locks published by the keypad."""
        if len(payload) < 69:
            return
        
        paired = []
        for i in range(3):
            offset = i * 23
            name_bytes = payload[offset : offset + 22]
            status_byte = payload[offset + 22]
            
            uuid_obj = self._parse_base64_uuid(name_bytes)
            if uuid_obj and status_byte != 0:
                paired.append({
                    "uuid": str(uuid_obj),
                    "status": status_byte,
                })
        self.paired_locks = paired
        logger.info("Parsed paired locks list: %s", self.paired_locks)
        if self._status_cb:
            self._status_cb(self, self)

    def _parse_base64_uuid(self, name_bytes: bytes) -> UUID | None:
        """Decodes the 22-byte paddingless base64 representation of a UUID or raw binary UUID."""
        binary_bytes = name_bytes.rstrip(b"\x00")
        if len(binary_bytes) == 16:
            try:
                return UUID(bytes=binary_bytes)
            except ValueError:
                pass

        try:
            clean = name_bytes.rstrip(b"\x00").decode("ascii").strip()
            if not clean:
                return None
            padding = "=" * (4 - len(clean) % 4)
            uuid_bytes = base64.b64decode(clean + padding)
            if len(uuid_bytes) == 16:
                return UUID(bytes=uuid_bytes)
        except Exception:
            pass
        return None

    async def add_paired_lock(self, device_uuid: UUID, secret_key: bytes) -> None:
        """Add/Pair a Sesame Lock to the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        payload = device_uuid.bytes + secret_key
        await self.send_command(ITEM_ADD_SESAME, payload, encrypt=True)

    async def remove_paired_lock(self, device_uuid: UUID) -> None:
        """Remove/Unpair a Sesame Lock from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        await self.send_command(ITEM_REMOVE_SESAME, device_uuid.bytes, encrypt=True)
