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
from typing import Any, Callable, Self
from urllib import parse
from uuid import UUID
import datetime

from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakDeviceNotFoundError

from .crypto import SesameCipher, derive_session_token_key, generate_ecc_keypair, derive_device_secret

logger = logging.getLogger(__name__)

# --- Protocol Constants ---
SERVICE_UUID = "0000fd81-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "16860002-a5ae-9856-b6d3-dbb4c676993e"
RX_CHAR_UUID = "16860003-a5ae-9856-b6d3-dbb4c676993e"
COMPANY_ID = 0x055A
COMPANY_IDS = (0x055A, 0x05A7, 0x05A5, 0x053A)

def get_sesame_mfg_data(mfg_dict: dict[int, bytes]) -> tuple[int, bytes] | None:
    """Finds Sesame/CandyHouse manufacturer data across all known company IDs or matching payloads."""
    if not mfg_dict:
        return None
    for cid in COMPANY_IDS:
        if cid in mfg_dict:
            return cid, mfg_dict[cid]
    # Fallback: check any company ID with >= 19 bytes payload
    for cid, data in mfg_dict.items():
        if len(data) >= 19:
            return cid, data
    return None

MTU_SIZE = 20


# Operation Codes
OP_CREATE = 0x01
OP_READ = 0x02
OP_RESPONSE = 0x07
OP_PUBLISH = 0x08

# Protocol Item Codes
ITEM_REGISTRATION = 1
ITEM_LOGIN = 2
ITEM_VERSION_TAG = 5
ITEM_ENABLE_DFU = 7
ITEM_TIME = 8
ITEM_INITIAL = 14
ITEM_MAGNET = 17
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

# OpenSensor Item Codes
ITEM_DOOR_OPEN = 90
ITEM_DOOR_CLOSED = 91
ITEM_OPS_TIMER_SETTING = 92

# Touch Keypad Passcode Item Codes
ITEM_PASSCODE_CHANGE = 123
ITEM_PASSCODE_DELETE = 124
ITEM_PASSCODE_GET = 125
ITEM_PASSCODE_NOTIFY = 126
ITEM_PASSCODE_LAST = 127
ITEM_PASSCODE_FIRST = 128
ITEM_PASSCODE_MODE_GET = 129
ITEM_PASSCODE_MODE_SET = 130
ITEM_PASSCODE_ADD = 138
ITEM_PASSCODE_VERIFY_TO_CLOUD = 151

# Touch Keypad Card Item Codes
ITEM_CARD_CHANGE = 107
ITEM_CARD_DELETE = 108
ITEM_CARD_GET = 109
ITEM_CARD_NOTIFY = 110
ITEM_CARD_LAST = 111
ITEM_CARD_FIRST = 112
ITEM_CARD_MODE_SET = 114
ITEM_CARD_ADD = 140
ITEM_CARD_VERIFY_TO_CLOUD = 203

# Touch Keypad Fingerprint Item Codes
ITEM_FINGER_CHANGE = 115
ITEM_FINGER_DELETE = 116
ITEM_FINGER_GET = 117
ITEM_FINGER_NOTIFY = 118
ITEM_FINGER_LAST = 119
ITEM_FINGER_FIRST = 120
ITEM_FINGER_MODE_SET = 122

# Biometric Keypad Face Item Codes
ITEM_FACE_CHANGE = 154
ITEM_FACE_DELETE = 155
ITEM_FACE_GET = 156
ITEM_FACE_NOTIFY = 157
ITEM_FACE_LAST = 158
ITEM_FACE_FIRST = 159
ITEM_FACE_MODE_GET = 160
ITEM_FACE_MODE_SET = 161
ITEM_FACE_MODE_DELETE_NOTIFY = 192

# Biometric Keypad Palm Item Codes
ITEM_PALM_CHANGE = 162
ITEM_PALM_DELETE = 163
ITEM_PALM_GET = 164
ITEM_PALM_NOTIFY = 165
ITEM_PALM_LAST = 166
ITEM_PALM_FIRST = 167
ITEM_PALM_MODE_GET = 168
ITEM_PALM_MODE_SET = 169
ITEM_PALM_MODE_DELETE_NOTIFY = 193


# Product Models
class ProductModels(IntEnum):
    SESAME5 = 5
    SESAME_BIKE2 = 6
    SESAME5_PRO = 7
    SESAME_TOUCH_PRO = 9
    SESAME_TOUCH = 10
    SESAME5_USA = 16
    SESAME_FACE_PRO = 18
    SESAME_FACE = 19
    SESAME6 = 20
    SESAME6_PRO = 21
    SESAME_FACE_PRO_AI = 22
    SESAME_FACE_AI = 23
    SESAME_TOUCH_2 = 25
    SESAME_TOUCH_2_PRO = 26
    SESAME_FACE_2 = 27
    SESAME_FACE_2_PRO = 28
    SESAME_FACE_2_AI = 30
    SESAME_FACE_2_PRO_AI = 31
    SESAME6_PRO_SLIDING_DOOR = 32
    SESAME_BIKE3 = 33


def is_keypad_model(model: str | int | ProductModels | None) -> bool:
    """Return True if model is a keypad/touch/face device."""
    if model is None:
        return False
    if isinstance(model, ProductModels):
        name = model.name
    elif isinstance(model, int):
        try:
            name = ProductModels(model).name
        except ValueError:
            return False
    else:
        name = str(model)
    name_upper = name.upper()
    return any(k in name_upper for k in ("TOUCH", "FACE", "AI", "KEYPAD"))


def parse_dt(dt_str: str) -> datetime.datetime | None:
    """Parses ISO-8601 or common datetime string into a datetime object."""
    if not dt_str:
        return None
    s = dt_str.strip()
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

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

@dataclass
class SesameAdData:

    """Decoded manufacturer data from a Sesame BLE advertisement."""
    model_id: int
    is_registered: bool
    device_uuid: UUID


    @classmethod
    def decode(cls, manufacturer_data: bytes) -> Self:
        """Parses raw advertisement bytes (<HB16s)."""
        if len(manufacturer_data) < 19:
            raise ValueError(f"Manufacturer data too short: {len(manufacturer_data)} bytes")
        model_val, registered_val, uuid_bytes = struct.unpack("<HB16s", manufacturer_data[:19])
        return cls(
            model_id=model_val,
            is_registered=bool(registered_val & 1),
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
        parsed = parse.urlparse(url.strip())
        query_raw = parsed.query
        params = dict(parse.parse_qsl(query_raw, keep_blank_values=True))
        
        sk_str = params.get("sk", "").replace(" ", "+")
        missing_padding = len(sk_str) % 4
        if missing_padding:
            sk_str += "=" * (4 - missing_padding)
            
        key_level = int(params.get("l", "0"))
        device_name = params.get("n", "")
        
        shared_key = base64.b64decode(sk_str)
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
        self.ad_data = ad_data
        self.product_model = ad_data.model_id if ad_data else None
        self._secret = bytes.fromhex(secret_key) if secret_key else None

        self._reconnect_limit = reconnect_attempts
        self._status_cb = status_callback

        self._segmenter = SesameSegmentLayer()
        self._client = None
        self._cipher = None
        self._reconnect_task = None
        self._auto_reconnect_paused = False

        self._login_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        
        self._token_future = None
        self._pending_responses = {}
        self.is_logged_in = False
        self.mech_status = None

        self.current_angle = None
        self.target_angle = None
        self.lock_position = None
        self.unlock_position = None
        self.is_locked = False
        self.is_unlocked = False
        self.is_moving = False



    @property
    def address(self) -> str:
        """The BLE MAC address."""
        if self._ble_device and getattr(self._ble_device, "address", None):
            return self._ble_device.address
        return getattr(self, "_address", None) or "00:00:00:00:00:00"

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

            # Auto-sync time if drift is detected (more than 5 seconds difference)
            host_time = int(time.time())
            if abs(device_time - host_time) > 5:
                try:
                    await self.sync_time()
                except Exception as e:
                    logger.warning("Failed to automatically synchronize clock on login: %s", e)
            
            # Wait for mechanical settings/status to be published to complete login
            try:
                await asyncio.wait_for(self._login_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for initial mechanical settings/status publish")

            if self._status_cb:
                self._status_cb(self, self)

            # Request firmware version in background on successful login
            asyncio.create_task(self.request_firmware_version())

            return device_time
        except Exception:
            await self.disconnect()
            raise

    @property
    def firmware_version(self) -> str | None:
        """Return the current firmware version string if fetched."""
        return getattr(self, "_firmware_version", None)

    async def request_firmware_version(self) -> str | None:
        """Request the firmware version tag from the device over BLE."""
        if not self.is_logged_in:
            return self.firmware_version
        try:
            res = await self.send_command(ITEM_VERSION_TAG, b"")
            if res:
                ver_str = res.decode("utf-8", errors="ignore").strip("\x00").strip()
                if ver_str:
                    self._firmware_version = ver_str
                    logger.info("Retrieved firmware version for %s: %s", self.address, ver_str)
                    if self._status_cb:
                        self._status_cb(self, self)
                    return ver_str
        except Exception as err:
            logger.debug("Failed to fetch firmware version from %s: %s", self.address, err)
        return self.firmware_version

    async def enable_dfu(self) -> None:
        """Put device into BLE DFU mode for over-the-air firmware updates."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        logger.info("Sending ENABLE_DFU command to device %s", self.address)
        await self.send_command(ITEM_ENABLE_DFU, b"\x01", wait_for_response=False)

    async def sync_time(self) -> None:
        """Synchronizes the device's clock with the host time."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        timestamp = int(time.time())
        logger.info("Synchronizing time on device [address=%s, timestamp=%d]", self.address, timestamp)
        payload = struct.pack("<I", timestamp)
        await self.send_command(ITEM_TIME, payload, encrypt=True)

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

    def pause_auto_reconnect(self) -> None:
        """Cancels and suppresses automatic reconnection during DFU or manual disconnect."""
        self._auto_reconnect_paused = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None

    def resume_auto_reconnect(self) -> None:
        """Restores automatic reconnection."""
        self._auto_reconnect_paused = False

    def _on_disconnect(self, client: BleakClient) -> None:
        del client
        logger.warning("Sesame BLE disconnected unexpectedly [address=%s]", self.address)
        self._reset_state()
        if not self._auto_reconnect_paused and self._reconnect_limit and (not self._reconnect_task or self._reconnect_task.done()):
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
        op_code: int | None = None,
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
        self.door_status = None
        self.ops_lock_second = None

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

        elif item_code == ITEM_DOOR_OPEN:
            self.door_status = "open"
            if self._status_cb:
                self._status_cb(self, self)

        elif item_code == ITEM_DOOR_CLOSED:
            self.door_status = "closed"
            if self._status_cb:
                self._status_cb(self, self)

        elif item_code == ITEM_OPS_TIMER_SETTING:
            if len(payload) >= 2:
                self.ops_lock_second = struct.unpack("<H", payload[0:2])[0]
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

    async def set_ops_lock_second(self, second: int) -> None:
        """Configures the OpenSensor auto lock duration in seconds."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        payload = struct.pack("<H", second)
        logger.info(
            "Configuring OpenSensor auto lock duration [address=%s, seconds=%d]",
            self.address,
            second,
        )
        await self.send_command(ITEM_OPS_TIMER_SETTING, payload, encrypt=True)

    async def calibrate_magnet(self) -> None:
        """Triggers the lock to perform magnet calibration/angle correction."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        logger.info("Triggering magnet calibration [address=%s]", self.address)
        await self.send_command(ITEM_MAGNET, b"", encrypt=True)

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
                if len(payload) < 16:
                    logger.warning("Received invalid history payload length: %d", len(payload))
                    break

                # Parse history record header
                # ssm_history header: id(4B) + type(1B) + ts(4B) + mech_status(7B) = 16B
                record_id, history_type, ts, mech_status = struct.unpack("<IBI7s", payload[:16])

                tag = 0
                raw_val = b""

                if len(payload) > 16:
                    param_bytes = payload[16:]
                    if len(param_bytes) >= 2:
                        tag = struct.unpack("<H", param_bytes[0:2])[0]
                        if len(param_bytes) >= 3:
                            data_length = min(max(0, param_bytes[2]), len(param_bytes) - 3)
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

    async def apply_passcode_schedules(self, logical_passcodes: dict[str, dict]) -> bool:
        """Evaluates schedules and synchronizes passcodes.

        This implements the virtualization of time-windowed passcodes by checking
        start/end times, weekly day lists, and daily time ranges, adding or
        deleting passcodes accordingly.

        Drivers can override this method if the hardware has native support for
        scheduled or time-windowed passcodes.

        Returns:
            bool: True if any changes were made, False otherwise.
        """
        now = datetime.datetime.now()
        changed = False

        for uid, logical_info in list(logical_passcodes.items()):
            # 1. Absolute date/time range check
            start_dt = parse_dt(logical_info.get("start", ""))
            end_dt = parse_dt(logical_info.get("end", ""))

            should_be_active = True
            if start_dt and now < start_dt:
                should_be_active = False
            if end_dt and now >= end_dt:
                should_be_active = False

            # 2. Weekly days check
            days_list = logical_info.get("days")
            if should_be_active and days_list:
                try:
                    # Convert elements to integers in case they are stringified
                    int_days = [int(d) for d in days_list]
                    if now.weekday() not in int_days:
                        should_be_active = False
                except (ValueError, TypeError) as e:
                    logger.warning("Invalid days list for passcode '%s': %s", logical_info.get("name"), e)

            # 3. Daily time range check
            time_start_str = logical_info.get("time_start", "").strip()
            time_end_str = logical_info.get("time_end", "").strip()
            if should_be_active and (time_start_str or time_end_str):
                current_time = now.time()
                if time_start_str:
                    try:
                        h, m = map(int, time_start_str.split(":"))
                        t_start = datetime.time(h, m)
                        if current_time < t_start:
                            should_be_active = False
                    except (ValueError, TypeError) as e:
                        logger.warning("Invalid time_start '%s' for passcode '%s': %s", time_start_str, logical_info.get("name"), e)
                if time_end_str:
                    try:
                        h, m = map(int, time_end_str.split(":"))
                        t_end = datetime.time(h, m)
                        if current_time >= t_end:
                            should_be_active = False
                    except (ValueError, TypeError) as e:
                        logger.warning("Invalid time_end '%s' for passcode '%s': %s", time_end_str, logical_info.get("name"), e)

            is_physically_active = uid in self.passcodes

            if should_be_active and not is_physically_active:
                logger.info("Scheduler: Adding passcode '%s' via virtualization", logical_info["name"])
                try:
                    await self.add_passcode(logical_info["code"], logical_info["name"])
                    changed = True
                except Exception as e:
                    logger.error("Failed to add passcode '%s': %s", logical_info["name"], e)
            elif not should_be_active and is_physically_active:
                logger.info("Scheduler: Removing passcode '%s' via virtualization", logical_info["name"])
                try:
                    await self.delete_passcode(uid)
                    changed = True
                except Exception as e:
                    logger.error("Failed to delete passcode '%s': %s", logical_info["name"], e)

        return changed

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

    async def get_faces(self) -> dict[str, dict]:
        """Fetch/sync faces from the keypad."""
        raise NotImplementedError()

    async def delete_face(self, face_id: str) -> None:
        """Delete a face from the keypad."""
        raise NotImplementedError()

    async def update_face_name(self, face_id: str, name: str) -> None:
        """Update the nickname of an existing face on the keypad."""
        raise NotImplementedError()

    async def get_palms(self) -> dict[str, dict]:
        """Fetch/sync palms from the keypad."""
        raise NotImplementedError()

    async def delete_palm(self, palm_id: str) -> None:
        """Delete a palm from the keypad."""
        raise NotImplementedError()

    async def update_palm_name(self, palm_id: str, name: str) -> None:
        """Update the nickname of an existing palm on the keypad."""
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
        self._temp_faces = {}
        self._temp_palms = {}
        self.faces = {}
        self.palms = {}
        self.faces_count = 0
        self.palms_count = 0
        self._sync_future = None
        self.scanned_fingerprint = None
        self.scanned_passcode = None
        self.scanned_face = None
        self.scanned_palm = None
        self.paired_locks = []
        self.card_registration_mode = False
        self.fingerprint_registration_mode = False
        self.passcode_registration_mode = False
        self.face_registration_mode = False
        self.palm_registration_mode = False

        self.battery_voltage = None
        self.battery_percentage = None
        self.is_battery_critical = None

    def on_published(self, item_code: int, payload: bytes) -> None:
        if item_code == ITEM_MECH_STATUS:
            # Struct varies by payload length:
            # - 7 bytes: battery (uint16_t), cards (int16_t), fingerprints (uint8_t), passwords (uint8_t), flags (uint8_t)
            # - 9 bytes: battery (uint16_t), cards (int16_t), fingerprints (uint8_t), passwords (uint8_t), faces (uint8_t), palms (uint8_t), flags (uint8_t)
            faces = 0
            palms = 0
            if len(payload) == 7:
                raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhBBB", payload)
                faces = 0
                palms = 0
            elif len(payload) == 9:
                model_id = getattr(getattr(self, "ad_data", None), "model_id", None) or getattr(self, "product_model", None)
                if isinstance(model_id, str) and model_id in ProductModels.__members__:
                    model_id = ProductModels[model_id].value
                if model_id in (18, 19, 22, 23, 25, 26, 27, 28, 30, 31):
                    raw_battery, cards, fingerprints, passwords, faces, palms, flags = struct.unpack("<HhBBBBB", payload)
                else:
                    raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhhhB", payload)
                    faces = 0
                    palms = 0
            else:
                model_id = getattr(getattr(self, "ad_data", None), "model_id", None) or getattr(self, "product_model", None)
                if isinstance(model_id, str) and model_id in ProductModels.__members__:
                    model_id = ProductModels[model_id].value
                if model_id in (18, 19, 22, 23, 25, 26, 27, 28, 30, 31):
                    raw_battery, cards, fingerprints, passwords, faces, palms, flags = struct.unpack("<HhBBBBB", payload[:9])
                else:
                    raw_battery, cards, fingerprints, passwords, flags = struct.unpack("<HhhhB", payload[:9])
                    faces = 0
                    palms = 0


            
            self.battery_voltage = raw_battery * 2 / 1000
            self.battery_percentage = calculate_battery_percentage(self.battery_voltage)
            self.cards_count = cards
            self.fingerprints_count = fingerprints
            self.passcodes_count = passwords
            self.faces_count = faces
            self.palms_count = palms
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
            logger.info("ITEM_PASSCODE_NOTIFY received: parsed=%s, sync_future=%s, passcode_reg_mode=%s", parsed, self._sync_future, getattr(self, "passcode_registration_mode", False))
            for uid, info in parsed.items():
                is_new = uid not in self.passcodes
                logger.info("Checking passcode notify: uid=%s, is_new=%s, existing_passcodes=%s", uid, is_new, list(self.passcodes.keys()))
                if self._sync_future is None or (getattr(self, "passcode_registration_mode", False) and is_new):
                    self.scanned_passcode = {
                        "uid": uid,
                        "code": info["code"],
                        "type": info["type"]
                    }
                    logger.info("Captured scanned passcode: %s (is_new=%s)", self.scanned_passcode, is_new)
                    if self._status_cb:
                        self._status_cb(self, self)
                else:
                    logger.info("Skipped scanned passcode capture. sync_future=%s, passcode_reg_mode=%s, is_new=%s", self._sync_future, getattr(self, "passcode_registration_mode", False), is_new)
        
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

        elif item_code == ITEM_FACE_FIRST:
            self._temp_faces = {}
            logger.debug("Face sync database started")

        elif item_code == ITEM_FACE_NOTIFY:
            parsed = self._parse_notify_payload(payload, is_passcode=False)
            self._temp_faces.update(parsed)
            logger.info("ITEM_FACE_NOTIFY received: parsed=%s", parsed)
            for uid, info in parsed.items():
                is_new = uid not in self.faces
                if self._sync_future is None or (getattr(self, "face_registration_mode", False) and is_new):
                    self.scanned_face = {"uid": uid, "type": info["type"]}
                    if self._status_cb:
                        self._status_cb(self, self)

        elif item_code == ITEM_FACE_LAST:
            self.faces = self._temp_faces
            logger.debug("Face sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)

        elif item_code == ITEM_PALM_FIRST:
            self._temp_palms = {}
            logger.debug("Palm sync database started")

        elif item_code == ITEM_PALM_NOTIFY:
            parsed = self._parse_notify_payload(payload, is_passcode=False)
            self._temp_palms.update(parsed)
            logger.info("ITEM_PALM_NOTIFY received: parsed=%s", parsed)
            for uid, info in parsed.items():
                is_new = uid not in self.palms
                if self._sync_future is None or (getattr(self, "palm_registration_mode", False) and is_new):
                    self.scanned_palm = {"uid": uid, "type": info["type"]}
                    if self._status_cb:
                        self._status_cb(self, self)

        elif item_code == ITEM_PALM_LAST:
            self.palms = self._temp_palms
            logger.debug("Palm sync database completed")
            if self._sync_future and not self._sync_future.done():
                self._sync_future.set_result(True)


        elif item_code == ITEM_KEYPAD_LOCK_LIST:
            self._parse_paired_locks(payload)

        elif item_code == ITEM_PASSCODE_VERIFY_TO_CLOUD:
            if len(payload) >= 2:
                kb_type = payload[0]
                id_len = payload[1]
                if len(payload) >= 2 + id_len:
                    kb_id = payload[2 : 2 + id_len]
                    code_str = "".join(str(b) for b in kb_id)
                    self.scanned_passcode = {
                        "uid": kb_id.hex(),
                        "code": code_str,
                        "type": kb_type
                    }
                    logger.info("Captured scanned unregistered passcode (151): %s", self.scanned_passcode)
                    if self._status_cb:
                        self._status_cb(self, self)

        elif item_code == ITEM_CARD_VERIFY_TO_CLOUD:
            if len(payload) >= 2:
                card_type = payload[0]
                id_len = payload[1]
                if len(payload) >= 2 + id_len:
                    card_id = payload[2 : 2 + id_len]
                    card_id_hex = card_id.hex()
                    self.scanned_card = {
                        "uid": card_id_hex,
                        "type": card_type
                    }
                    logger.info("Captured scanned unregistered card (203): %s", self.scanned_card)
                    if self._status_cb:
                        self._status_cb(self, self)

    async def get_passcodes(self) -> dict[str, dict]:
        """Syncs the passcode database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "passcodes_count", 0) == 0:
            self.passcodes = {}
            return self.passcodes

        fut = self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_passcodes = {}

        try:
            await self.send_command(ITEM_PASSCODE_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(fut, timeout=15.0)
            return self.passcodes
        finally:
            if self._sync_future is fut:
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

        fut = self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_cards = {}

        try:
            await self.send_command(ITEM_CARD_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(fut, timeout=15.0)
            return self.cards
        finally:
            if self._sync_future is fut:
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

        fut = self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_fingerprints = {}

        try:
            await self.send_command(ITEM_FINGER_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(fut, timeout=15.0)
            return self.fingerprints
        finally:
            if self._sync_future is fut:
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

    async def set_passcode_registration_mode(self, active: bool) -> None:
        """Sets the keypad passcode mode: True for Add Mode, False for Verification Mode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        mode = 0x01 if active else 0x00
        await self.send_command(ITEM_PASSCODE_MODE_SET, bytes([mode]), encrypt=True)
        self.passcode_registration_mode = active

    async def get_faces(self) -> dict[str, dict]:
        """Syncs the face database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "faces_count", 0) == 0:
            self.faces = {}
            return self.faces

        fut = self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_faces = {}

        try:
            await self.send_command(ITEM_FACE_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(fut, timeout=15.0)
            return self.faces
        finally:
            if self._sync_future is fut:
                self._sync_future = None

    async def delete_face(self, face_id: str) -> None:
        """Deletes a face from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        await self.send_command(ITEM_FACE_DELETE, bytes.fromhex(face_id), encrypt=True)

    async def update_face_name(self, face_id: str, name: str) -> None:
        """Updates the nickname of an existing face."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        id_bytes = bytes.fromhex(face_id)
        name_bytes = name.encode("utf-8")[:20]
        payload = bytes([len(id_bytes)]) + id_bytes + name_bytes
        await self.send_command(ITEM_FACE_CHANGE, payload, encrypt=True)

    async def set_face_registration_mode(self, active: bool) -> None:
        """Sets the keypad face mode: True for Add Mode, False for Verification Mode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        mode = 0x01 if active else 0x00
        await self.send_command(ITEM_FACE_MODE_SET, bytes([mode]), encrypt=True)
        self.face_registration_mode = active

    async def get_palms(self) -> dict[str, dict]:
        """Syncs the palm database from the keypad and returns it."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")

        if getattr(self, "palms_count", 0) == 0:
            self.palms = {}
            return self.palms

        fut = self._sync_future = asyncio.get_running_loop().create_future()
        self._temp_palms = {}

        try:
            await self.send_command(ITEM_PALM_GET, b"", encrypt=True, wait_for_response=False)
            await asyncio.wait_for(fut, timeout=15.0)
            return self.palms
        finally:
            if self._sync_future is fut:
                self._sync_future = None

    async def delete_palm(self, palm_id: str) -> None:
        """Deletes a palm from the keypad."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        await self.send_command(ITEM_PALM_DELETE, bytes.fromhex(palm_id), encrypt=True)

    async def update_palm_name(self, palm_id: str, name: str) -> None:
        """Updates the nickname of an existing palm."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        id_bytes = bytes.fromhex(palm_id)
        name_bytes = name.encode("utf-8")[:20]
        payload = bytes([len(id_bytes)]) + id_bytes + name_bytes
        await self.send_command(ITEM_PALM_CHANGE, payload, encrypt=True)

    async def set_palm_registration_mode(self, active: bool) -> None:
        """Sets the keypad palm mode: True for Add Mode, False for Verification Mode."""
        if not self.is_logged_in:
            raise Exception("Device is not logged in")
        mode = 0x01 if active else 0x00
        await self.send_command(ITEM_PALM_MODE_SET, bytes([mode]), encrypt=True)
        self.palm_registration_mode = active


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


def create_sesame_device(
    ble_device: Any,
    ad_data: SesameAdData,
    secret_key: str | bytes | None = None,
    **kwargs: Any,
) -> SesameLock | SesameKeypad:
    """Factory function to instantiate the appropriate Sesame device class."""
    if is_keypad_model(ad_data.model_id):
        return SesameKeypad(ble_device, ad_data, secret_key=secret_key, **kwargs)
    return SesameLock(ble_device, ad_data, secret_key=secret_key, **kwargs)


async def scan_sesame_devices(
    timeout: float = 5.0,
    callback: Callable[[BLEDevice, SesameAdData], None] | None = None,
) -> dict[str, tuple[BLEDevice, SesameAdData]]:
    """Scan for nearby Sesame BLE devices."""
    found_devices: dict[str, tuple[BLEDevice, SesameAdData]] = {}

    def detection_callback(device: BLEDevice, advertisement_data: Any) -> None:
        mfg_dict = advertisement_data.manufacturer_data
        mfg = get_sesame_mfg_data(mfg_dict) if mfg_dict else None
        if not mfg:
            return
        _, raw_data = mfg
        try:
            ad_data = SesameAdData.decode(raw_data)
            if device.address not in found_devices:
                found_devices[device.address] = (device, ad_data)
                if callback:
                    callback(device, ad_data)
        except Exception:
            pass

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return found_devices


async def find_sesame_device(
    address: str,
    timeout: float = 5.0,
) -> tuple[BLEDevice, SesameAdData | None] | None:
    """Find a specific Sesame BLE device by MAC address."""
    scanner = BleakScanner()
    await scanner.start()
    target_addr = address.upper()
    ble_device: BLEDevice | None = None
    ad_data: SesameAdData | None = None

    for _ in range(int(timeout * 10)):
        for addr, (dev, adv) in scanner.discovered_devices_and_advertisement_data.items():
            if addr.upper() == target_addr:
                ble_device = dev
                if adv.manufacturer_data:
                    mfg = get_sesame_mfg_data(adv.manufacturer_data)
                    if mfg:
                        try:
                            ad_data = SesameAdData.decode(mfg[1])
                        except Exception:
                            pass
                break
        if ble_device:
            break
        await asyncio.sleep(0.1)

    await scanner.stop()

    if not ble_device:
        ble_device = await BleakScanner.find_device_by_address(address, timeout=timeout)

    if not ble_device:
        return None

    return ble_device, ad_data

