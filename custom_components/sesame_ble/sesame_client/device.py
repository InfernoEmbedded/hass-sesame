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
ITEM_MECH_SETTING = 80
ITEM_MECH_STATUS = 81
ITEM_LOCK = 82
ITEM_UNLOCK = 83

# Touch Keypad Passcode Item Codes
ITEM_PASSCODE_CHANGE = 123
ITEM_PASSCODE_DELETE = 124
ITEM_PASSCODE_GET = 125
ITEM_PASSCODE_NOTIFY = 126
ITEM_PASSCODE_LAST = 127
ITEM_PASSCODE_FIRST = 128
ITEM_PASSCODE_ADD = 138

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
            await asyncio.wait_for(self._token_future, timeout=5.0)
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
            return device_time
        except Exception:
            self._reset_state()
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
                self._reset_state()
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
            payload = self._cipher.decrypt_payload(payload)

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

    async def send_command(self, item_code: int, payload: bytes, encrypt: bool = True) -> bytes:
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

            fut = asyncio.get_running_loop().create_future()
            self._pending_responses[item_code] = fut

            try:
                packets = self._segmenter.segment_payload(transmission, encrypt)
                for packet in packets:
                    await self._client.write_gatt_char(TX_CHAR_UUID, packet, response=False)

                return await asyncio.wait_for(fut, timeout=2.0)
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
            # We must receive mech setting to complete registration/login flow event
            if not self._login_event.is_set():
                self._login_event.set()

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

class SesameKeypad(SesameDevice):
    """Subclass representing a Sesame Touch or Sesame Touch Pro keypad."""

    def __init__(
        self,
        ble_device,
        ad_data: SesameAdData,
        secret_key: str | None = None,
        status_callback: Callable[[object, object], None] | None = None,
        reconnect_attempts: int = 0,
    ) -> None:
        super().__init__(ble_device, ad_data, secret_key, status_callback, reconnect_attempts)
        self.passcodes = {}
        self._temp_passcodes = {}
        self._sync_future = None

        self.cards_count = 0
        self.fingerprints_count = 0
        self.passcodes_count = 0
        self.battery_voltage = None
        self.battery_percentage = None
        self.is_battery_critical = None

    def on_published(self, item_code: int, payload: bytes) -> None:
        if item_code == ITEM_MECH_STATUS:
            # Struct: battery (uint16_t), cards (int16_t), fingerprints (int16_t), passwords (int16_t), flags (uint8_t)
            raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhhhB", payload)
            
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
            idx = 0
            while idx + 3 <= len(payload):
                pw_type = payload[idx]
                pw_id_len = payload[idx + 1]
                if idx + 2 + pw_id_len > len(payload):
                    break
                pw_id = payload[idx + 2 : idx + 2 + pw_id_len]
                pw_name_len = payload[idx + 2 + pw_id_len]
                if idx + 2 + pw_id_len + 1 + pw_name_len > len(payload):
                    break
                pw_name = payload[idx + 2 + pw_id_len + 1 : idx + 2 + pw_id_len + 1 + pw_name_len].decode("utf-8", errors="replace")

                self._temp_passcodes[pw_id.hex()] = {
                    "name": pw_name,
                    "code": "".join(str(b) for b in pw_id),
                    "type": pw_type,
                }
                idx += 2 + pw_id_len + 1 + pw_name_len
        
        elif item_code == ITEM_PASSCODE_LAST:
            self.passcodes = self._temp_passcodes
            logger.debug("Passcode sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)

    async def get_passcodes(self) -> dict[str, dict]:
        """Syncs the passcode database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_passcodes = {}

        try:
            await self.send_command(ITEM_PASSCODE_GET, b"", encrypt=True)
            await asyncio.wait_for(self._sync_future, timeout=5.0)
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
