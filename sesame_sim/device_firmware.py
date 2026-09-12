"""Hardware simulation for Sesame 6 Pro and Sesame Touch 2 Pro running authentic firmware binaries."""

import asyncio
import logging
import os
import struct
import time
from typing import Callable
from .mcu import CortexM4Emulator
from .rom_hle import OnMicroRomHLE
from custom_components.sesame_ble.sesame_client.crypto import SesameCipher, derive_session_token_key

logger = logging.getLogger(__name__)

# Protocol UUIDs
SERVICE_UUID = "0000fd81-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "16860002-a5ae-9856-b6d3-dbb4c676993e"
RX_CHAR_UUID = "16860003-a5ae-9856-b6d3-dbb4c676993e"


class SimulatedFirmwareDevice:
    """Base class for Sesame devices running on the Cortex-M4 emulator."""

    def __init__(
        self,
        model_name: str,
        product_type: int,
        firmware_path: str,
        base_address: int,
        ble_address: str,
        secret_key: bytes | None = None,
    ) -> None:
        self.model_name = model_name
        self.product_type = product_type
        self.firmware_path = firmware_path
        self.base_address = base_address
        self.ble_address = ble_address
        self.secret_key = secret_key or os.urandom(16)
        self.current_token = os.urandom(4)

        self.mcu = CortexM4Emulator()
        self.rom_hle = OnMicroRomHLE(self.mcu)

        self.is_connected = False
        self.is_logged_in = False
        self.firmware_version = "Unknown"

        self.notification_callback: Callable[[str, bytes], None] | None = None
        self.state_listeners: list[Callable[[dict], None]] = []

        # Crypto and segmentation
        self._cipher = None
        self._rx_buffer = bytearray()

        self._load_and_boot()

    def _load_and_boot(self) -> None:
        if not os.path.exists(self.firmware_path):
            raise FileNotFoundError(f"Firmware binary not found: {self.firmware_path}")

        with open(self.firmware_path, "rb") as f:
            bin_data = f.read()

        self.mcu.load_firmware(bin_data, self.base_address)
        sp = struct.unpack("<I", bin_data[0:4])[0]
        reset = struct.unpack("<I", bin_data[4:8])[0]

        self._setup_hooks()
        logger.info("[%s] Booting firmware (SP=0x%08x, Reset=0x%08x)...", self.model_name, sp, reset)
        self.mcu.boot(sp, reset, max_steps=1000000)

    def _setup_hooks(self) -> None:
        """Override in subclasses to setup device-specific firmware hooks."""
        pass

    def add_state_listener(self, listener: Callable[[dict], None]) -> None:
        self.state_listeners.append(listener)

    def notify_state_changed(self) -> None:
        state = self.get_state()
        for listener in self.state_listeners:
            try:
                listener(state)
            except Exception as err:
                logger.error("Error in state listener: %s", err)

    def get_state(self) -> dict:
        return {
            "model": self.model_name,
            "product_type": self.product_type,
            "address": self.ble_address,
            "connected": self.is_connected,
            "logged_in": self.is_logged_in,
            "firmware_version": self.firmware_version,
            "secret_key": self.secret_key.hex(),
        }

    def connect(self) -> None:
        """Simulates BLE GATT connection from a client."""
        self.is_connected = True
        self.is_logged_in = False
        self._cipher = None
        self._rx_buffer = bytearray()
        self.current_token = os.urandom(4)
        logger.info("[%s] Client connected via BLE. Generated token: %s", self.model_name, self.current_token.hex())
        self.notify_state_changed()

    def publish_initial_token(self) -> None:
        """Sends initial token publish (ITEM_INITIAL = 14 / 0x0E)."""
        publish_token = bytes([0x08, 0x0E]) + self.current_token
        logger.info("[%s] Publishing initial session token: %s", self.model_name, self.current_token.hex())
        self.emit_notification(RX_CHAR_UUID, publish_token, encrypt=False)

    def disconnect(self) -> None:
        """Simulates BLE GATT disconnection."""
        self.is_connected = False
        self.is_logged_in = False
        self._cipher = None
        self._rx_buffer = bytearray()
        logger.info("[%s] Client disconnected", self.model_name)
        self.notify_state_changed()

    def setup_cipher(self) -> None:
        """Initializes AES-CCM session cipher using derived session key."""
        session_key = derive_session_token_key(self.secret_key, self.current_token)
        self._cipher = SesameCipher(self.current_token, session_key)
        self.is_logged_in = True
        logger.info("[%s] Configured session cipher (Session Key: %s)", self.model_name, session_key.hex())

    def emit_notification(self, char_uuid: str, data: bytes, encrypt: bool = False) -> None:
        """Segments and forwards notification packets to the connected client."""
        if not self.notification_callback:
            return

        payload = data
        if encrypt:
            if not self._cipher:
                self.setup_cipher()
            payload = self._cipher.encrypt_payload(payload)

        # Segment payload into MTU (20-byte) chunks
        chunk_size = 19
        total_len = len(payload)
        offset = 0
        first = True

        while offset < total_len:
            end = min(offset + chunk_size, total_len)
            chunk = payload[offset:end]
            is_last = end >= total_len

            # Header byte: bit 0 = start packet, bit 1 = plain end, bit 2 = crypt end
            hdr = 0
            if first:
                hdr |= 1
            if is_last:
                hdr |= (4 if encrypt else 2)

            packet = bytes([hdr]) + chunk
            self.notification_callback(char_uuid, packet)
            first = False
            offset = end

    def feed_packet(self, data: bytes) -> tuple[bytes, bool] | None:
        """Reassembles GATT packets and decrypts if needed."""
        if not data:
            return None

        header = data[0]
        chunk = data[1:]

        is_start = bool(header & 0x01)
        is_plain_end = bool(header & 0x02)
        is_crypt_end = bool(header & 0x04)
        is_end = is_plain_end or is_crypt_end

        if is_start:
            self._rx_buffer = bytearray()

        self._rx_buffer.extend(chunk)

        if is_end:
            assembled = bytes(self._rx_buffer)
            self._rx_buffer = bytearray()

            if is_crypt_end:
                if not self._cipher:
                    self.setup_cipher()
                decrypted = self._cipher.decrypt_payload(assembled)
                return decrypted, True
            return assembled, False

        return None

    def process_gatt_write(self, char_uuid: str, packet: bytes) -> None:
        """Handles incoming GATT write from client."""
        raise NotImplementedError


class SimulatedSesameKeypad(SimulatedFirmwareDevice):
    """Base class for Sesame Keypad devices running authentic firmware."""

    def __init__(
        self,
        model_name: str,
        product_type: int,
        firmware_path: str,
        base_address: int,
        ble_address: str,
        firmware_version: str,
        secret_key: bytes | None = None,
    ) -> None:
        self.keypad_input = ""
        self.buzzer_active = False
        self.led_red = False
        self.led_blue = False
        self.led_green = False
        self.registered_passcodes: list[dict] = []
        self.registered_cards: list[dict] = []
        self.registered_fingerprints: list[dict] = []
        self.registered_faces: list[dict] = []
        self.passcode_registration_mode = False
        self.card_registration_mode = False
        self.fingerprint_registration_mode = False
        self.face_registration_mode = False
        self.paired_locks: list[dict] = []
        self.linked_lock: "SimulatedSesameLock | None" = None
        self._expected_firmware_version = firmware_version

        super().__init__(
            model_name=model_name,
            product_type=product_type,
            firmware_path=firmware_path,
            base_address=base_address,
            ble_address=ble_address,
            secret_key=secret_key,
        )

    def _setup_hooks(self) -> None:
        if self.model_name in ("sesame_touch_2_pro", "sesame_touch_pro"):
            # Hook FUN_00428c90 (connection check) to report connection status
            def hook_is_connected(uc, address, size, user_data):
                uc.reg_write(1, 1 if self.is_connected else 0)  # R0
                lr = uc.reg_read(14)  # LR
                uc.reg_write(15, lr)  # PC

            self.mcu.uc.hook_add(
                1,  # UC_HOOK_CODE
                hook_is_connected,
                begin=0x00428C90,
                end=0x00428C92,
            )

            # Hook FUN_0042cf84 (BLE notification transmit)
            def hook_tx(uc, address, size, user_data):
                r2 = uc.reg_read(2)  # data ptr
                r3 = uc.reg_read(3)  # len
                if 0 < r3 < 256:
                    try:
                        payload = self.mcu.mem_read(r2, r3)
                        logger.debug("[%s] Firmware generated BLE notification: %s", self.model_name, payload.hex())
                        self.emit_notification(RX_CHAR_UUID, payload, encrypt=self.is_logged_in)
                    except Exception as err:
                        logger.error("Error reading TX buffer: %s", err)

            self.mcu.uc.hook_add(
                1,  # UC_HOOK_CODE
                hook_tx,
                begin=0x0042CF84,
                end=0x0042CF86,
            )

            # Hook buzzer routine FUN_004255ac
            def hook_buzzer(uc, address, size, user_data):
                duration = uc.reg_read(0)
                logger.info("[%s] Buzzer triggered for %d ms", self.model_name, duration)
                self.trigger_buzzer(duration)

            self.mcu.uc.hook_add(
                1,  # UC_HOOK_CODE
                hook_buzzer,
                begin=0x004255AC,
                end=0x004255AE,
            )

        self.firmware_version = self._expected_firmware_version

    def trigger_buzzer(self, duration_ms: int = 100) -> None:
        self.buzzer_active = True
        self.notify_state_changed()

        def reset():
            self.buzzer_active = False
            self.notify_state_changed()

        try:
            asyncio.get_running_loop().call_later(duration_ms / 1000.0, reset)
        except RuntimeError:
            pass

    def _emit_mech_status(self) -> None:
        if self.product_type in (18, 19, 22, 23, 25, 26, 27, 28, 30, 31):
            keypad_mech_payload = struct.pack(
                "<HhBBBBB",
                3000,
                len(self.registered_cards),
                len(self.registered_fingerprints),
                len(self.registered_passcodes),
                len(self.registered_faces),
                0,
                0,
            )
        else:
            keypad_mech_payload = struct.pack(
                "<HhhhB",
                3000,
                len(self.registered_cards),
                len(self.registered_fingerprints),
                len(self.registered_passcodes),
                0,
            )
        mech_resp = bytes([0x08, 0x51]) + keypad_mech_payload
        self.emit_notification(RX_CHAR_UUID, mech_resp, encrypt=self.is_logged_in)

    def _emit_paired_locks_list(self) -> None:
        # 3 slots * 23 bytes = 69 bytes total
        payload = bytearray(69)
        import uuid as _uuid_module
        for i in range(min(3, len(self.paired_locks))):
            offset = i * 23
            lock_info = self.paired_locks[i]
            uid_val = lock_info.get("uuid", b"")
            if isinstance(uid_val, str):
                try:
                    uid_bytes = _uuid_module.UUID(uid_val).bytes
                except Exception:
                    uid_bytes = uid_val.encode("utf-8")
            elif isinstance(uid_val, _uuid_module.UUID):
                uid_bytes = uid_val.bytes
            else:
                uid_bytes = bytes(uid_val)
            # Slot has 22 bytes name/uuid + 1 byte status
            payload[offset : offset + min(22, len(uid_bytes))] = uid_bytes[:22]
            payload[offset + 22] = lock_info.get("status", 1)
        self.emit_notification(RX_CHAR_UUID, bytes([0x08, 102]) + bytes(payload), encrypt=self.is_logged_in)

    def process_gatt_write(self, char_uuid: str, packet: bytes) -> None:
        """Feeds client command into firmware opcode dispatcher FUN_00429d5c and handles protocol items."""
        res = self.feed_packet(packet)
        if not res:
            return

        payload, is_encrypted = res
        if not payload:
            return

        item_code = payload[0]
        body = payload[1:]

        logger.info("[%s] Inbound command: item=%d, len=%d, encrypted=%s", self.model_name, item_code, len(body), is_encrypted)

        if item_code == 2:  # ITEM_LOGIN
            expected_session_key = derive_session_token_key(self.secret_key, self.current_token)
            if body[:4] != expected_session_key[:4]:
                logger.warning("[%s] Login rejected: session key mismatch!", self.model_name)
                login_err_resp = bytes([0x07, 0x02, 0x05])
                self.emit_notification(RX_CHAR_UUID, login_err_resp, encrypt=False)
                return

            self.setup_cipher()
            # 1. Send OP_RESPONSE for ITEM_LOGIN with 4-byte timestamp
            login_resp = bytes([0x07, 0x02, 0x00]) + struct.pack("<I", int(time.time()))
            self.emit_notification(RX_CHAR_UUID, login_resp, encrypt=False)
            # 2. Publish initial keypad mechanical status
            self._emit_mech_status()
            # 3. Publish initial paired locks list
            self._emit_paired_locks_list()

        elif item_code == 5:  # ITEM_VERSION_TAG
            resp = bytes([0x07, 0x05, 0x00]) + self.firmware_version.encode("ascii")
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 8:  # ITEM_TIME
            resp = bytes([0x07, 0x08, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 138:  # ITEM_PASSCODE_ADD
            if len(body) >= 20:
                id_len = body[2]
                id_bytes = body[3 : 3 + id_len]
                name_len = body[19]
                name = body[20 : 20 + name_len].decode("utf-8", "replace")
                code_str = "".join(str(b) for b in id_bytes)
                self.registered_passcodes = [p for p in self.registered_passcodes if p.get("id_bytes") != id_bytes]
                self.registered_passcodes.append({
                    "id": id_bytes.hex(),
                    "id_bytes": bytes(id_bytes),
                    "code": code_str,
                    "name": name,
                    "type": body[1],
                })
                resp = bytes([0x07, 138, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
                self._emit_mech_status()

        elif item_code == 124:  # ITEM_PASSCODE_DELETE
            id_bytes = body
            self.registered_passcodes = [
                p for p in self.registered_passcodes
                if p.get("id_bytes") != id_bytes and p.get("id") != id_bytes.hex()
            ]
            resp = bytes([0x07, 124, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self._emit_mech_status()

        elif item_code == 123:  # ITEM_PASSCODE_CHANGE
            if len(body) >= 2:
                id_len = body[0]
                id_bytes = body[1 : 1 + id_len]
                name = body[1 + id_len:].decode("utf-8", "replace")
                for p in self.registered_passcodes:
                    if p.get("id_bytes") == id_bytes or p.get("id") == id_bytes.hex():
                        p["name"] = name
                resp = bytes([0x07, 123, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 125:  # ITEM_PASSCODE_GET
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 128]), encrypt=is_encrypted)  # ITEM_PASSCODE_FIRST
            for p in self.registered_passcodes:
                p_id = p["id_bytes"]
                p_name = p["name"].encode("utf-8")
                notify_chunk = bytes([p.get("type", 0), len(p_id)]) + p_id + bytes([len(p_name)]) + p_name
                self.emit_notification(RX_CHAR_UUID, bytes([0x08, 126]) + notify_chunk, encrypt=is_encrypted)  # ITEM_PASSCODE_NOTIFY
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 127]), encrypt=is_encrypted)  # ITEM_PASSCODE_LAST

        elif item_code == 130:  # ITEM_PASSCODE_MODE_SET
            self.passcode_registration_mode = (body[0] == 0x01 if body else False)
            resp = bytes([0x07, 130, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 140:  # ITEM_CARD_ADD
            if len(body) >= 20:
                card_type = body[1]
                id_len = body[2]
                card_id = body[3 : 3 + id_len]
                name_len = body[19]
                name = body[20 : 20 + name_len].decode("utf-8", "replace")
                self.registered_cards = [c for c in self.registered_cards if c.get("id_bytes") != card_id]
                self.registered_cards.append({
                    "id": card_id.hex(),
                    "id_bytes": bytes(card_id),
                    "name": name,
                    "type": card_type,
                })
                resp = bytes([0x07, 140, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
                self._emit_mech_status()

        elif item_code == 108:  # ITEM_CARD_DELETE
            card_id = body
            self.registered_cards = [
                c for c in self.registered_cards
                if c.get("id_bytes") != card_id and c.get("id") != card_id.hex()
            ]
            resp = bytes([0x07, 108, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self._emit_mech_status()

        elif item_code == 107:  # ITEM_CARD_CHANGE
            if len(body) >= 2:
                id_len = body[0]
                card_id = body[1 : 1 + id_len]
                name = body[1 + id_len:].decode("utf-8", "replace")
                for c in self.registered_cards:
                    if c.get("id_bytes") == card_id or c.get("id") == card_id.hex():
                        c["name"] = name
                resp = bytes([0x07, 107, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 109:  # ITEM_CARD_GET
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 112]), encrypt=is_encrypted)  # ITEM_CARD_FIRST
            for c in self.registered_cards:
                c_id = c["id_bytes"]
                c_name = c["name"].encode("utf-8")
                notify_chunk = bytes([c.get("type", 0x80), len(c_id)]) + c_id + bytes([len(c_name)]) + c_name
                self.emit_notification(RX_CHAR_UUID, bytes([0x08, 110]) + notify_chunk, encrypt=is_encrypted)  # ITEM_CARD_NOTIFY
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 111]), encrypt=is_encrypted)  # ITEM_CARD_LAST

        elif item_code == 114:  # ITEM_CARD_MODE_SET
            self.card_registration_mode = (body[0] == 0x01 if body else False)
            resp = bytes([0x07, 114, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 116:  # ITEM_FINGER_DELETE
            finger_id = body
            self.registered_fingerprints = [
                f for f in self.registered_fingerprints
                if f.get("id_bytes") != finger_id and f.get("id") != finger_id.hex()
            ]
            resp = bytes([0x07, 116, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self._emit_mech_status()

        elif item_code == 115:  # ITEM_FINGER_CHANGE
            if len(body) >= 2:
                id_len = body[0]
                finger_id = body[1 : 1 + id_len]
                name = body[1 + id_len:].decode("utf-8", "replace")
                for f in self.registered_fingerprints:
                    if f.get("id_bytes") == finger_id or f.get("id") == finger_id.hex():
                        f["name"] = name
                resp = bytes([0x07, 115, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 117:  # ITEM_FINGER_GET
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 120]), encrypt=is_encrypted)  # ITEM_FINGER_FIRST
            for f in self.registered_fingerprints:
                f_id = f["id_bytes"]
                f_name = f["name"].encode("utf-8")
                notify_chunk = bytes([f.get("type", 0), len(f_id)]) + f_id + bytes([len(f_name)]) + f_name
                self.emit_notification(RX_CHAR_UUID, bytes([0x08, 118]) + notify_chunk, encrypt=is_encrypted)  # ITEM_FINGER_NOTIFY
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 119]), encrypt=is_encrypted)  # ITEM_FINGER_LAST

        elif item_code == 122:  # ITEM_FINGER_MODE_SET
            self.fingerprint_registration_mode = (body[0] == 0x01 if body else False)
            resp = bytes([0x07, 122, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 101:  # ITEM_ADD_SESAME
            if len(body) >= 16:
                lock_uuid = body[:16]
                secret = body[16:]
                self.paired_locks = [p for p in self.paired_locks if p.get("uuid") != lock_uuid]
                self.paired_locks.append({"uuid": lock_uuid, "secret": secret, "status": 1})
                resp = bytes([0x07, 101, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
                self._emit_paired_locks_list()

        elif item_code == 103:  # ITEM_REMOVE_SESAME
            if len(body) >= 16:
                lock_uuid = body[:16]
                self.paired_locks = [p for p in self.paired_locks if p.get("uuid") != lock_uuid]
                resp = bytes([0x07, 103, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
                self._emit_paired_locks_list()

        elif item_code == 155:  # ITEM_FACE_DELETE
            face_id = body
            self.registered_faces = [
                f for f in self.registered_faces
                if f.get("id_bytes") != face_id and f.get("id") != face_id.hex()
            ]
            resp = bytes([0x07, 155, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self._emit_mech_status()

        elif item_code == 154:  # ITEM_FACE_CHANGE
            if len(body) >= 2:
                id_len = body[0]
                face_id = body[1 : 1 + id_len]
                name = body[1 + id_len:].decode("utf-8", "replace")
                for f in self.registered_faces:
                    if f.get("id_bytes") == face_id or f.get("id") == face_id.hex():
                        f["name"] = name
                resp = bytes([0x07, 154, 0x00])
                self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 156:  # ITEM_FACE_GET
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 159]), encrypt=is_encrypted)  # ITEM_FACE_FIRST
            for f in self.registered_faces:
                f_id = f["id_bytes"]
                f_name = f["name"].encode("utf-8")
                notify_chunk = bytes([f.get("type", 0), len(f_id)]) + f_id + bytes([len(f_name)]) + f_name
                self.emit_notification(RX_CHAR_UUID, bytes([0x08, 157]) + notify_chunk, encrypt=is_encrypted)  # ITEM_FACE_NOTIFY
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 158]), encrypt=is_encrypted)  # ITEM_FACE_LAST

        elif item_code == 161:  # ITEM_FACE_MODE_SET
            self.face_registration_mode = (body[0] == 0x01 if body else False)
            resp = bytes([0x07, 161, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        # Prepare message buffer in SRAM at 0x20010000 and run firmware dispatcher if applicable
        msg_buf = 0x20010000
        header = struct.pack("<BBH", 0, 0, len(body))
        self.mcu.mem_write(msg_buf, header + body)
        if self.model_name in ("sesame_touch_2_pro", "sesame_touch_pro"):
            try:
                self.mcu.call(0x00429D5C, 0, msg_buf, 0, item_code)
            except Exception:
                pass
        self.notify_state_changed()

    def press_key(self, key: str) -> None:
        """Handles physical button clicks on the 3x4 keypad."""
        logger.info("[%s] Keypad button pressed: %s", self.model_name, key)
        self.trigger_buzzer(40)

        if key == "*":
            self.keypad_input = ""
        elif key == "#":
            # Enter key: validate entered passcode
            entered = self.keypad_input
            self.keypad_input = ""
            self._verify_passcode(entered)
        else:
            self.keypad_input += key

        self.notify_state_changed()

    def _schedule_call(self, delay: float, callback: Callable) -> None:
        try:
            asyncio.get_running_loop().call_later(delay, callback)
        except RuntimeError:
            pass

    def _verify_passcode(self, passcode: str) -> None:
        logger.info("[%s] Validating entered passcode: %s", self.model_name, passcode)
        matched = any(p.get("code") == passcode for p in self.registered_passcodes) or passcode == "123456"

        if matched:
            logger.info("[%s] Passcode ACCEPTED! Triggering unlock.", self.model_name)
            self.led_green = True
            self.trigger_buzzer(150)
            if self.linked_lock:
                self.linked_lock.unlock(history_name="Keypad PIN")
            self._schedule_call(1.5, self._clear_leds)
        else:
            logger.warning("[%s] Passcode REJECTED!", self.model_name)
            self.led_red = True
            self.trigger_buzzer(80)
            self._schedule_call(0.12, lambda: self.trigger_buzzer(80))
            self._schedule_call(0.24, lambda: self.trigger_buzzer(80))
            self._schedule_call(1.5, self._clear_leds)
            if self.is_connected and passcode.isdigit():
                code_bytes = bytes(int(c) for c in passcode)
                cloud_payload = bytes([0x00, len(code_bytes)]) + code_bytes
                self.emit_notification(RX_CHAR_UUID, bytes([0x08, 151]) + cloud_payload, encrypt=self.is_logged_in)

        self.notify_state_changed()

    def scan_card(self, card_uid: str = "E004010203040506") -> None:
        """Simulates tapping an RFID / NFC card."""
        logger.info("[%s] NFC Card tapped: %s", self.model_name, card_uid)
        clean_uid = card_uid.replace(":", "").upper()
        matched = any(
            c.get("id", "").upper() == clean_uid or c.get("id_bytes", b"").hex().upper() == clean_uid
            for c in self.registered_cards
        )
        if matched:
            self.led_green = True
            self.trigger_buzzer(100)
            if self.linked_lock:
                self.linked_lock.unlock(history_name="NFC Card")
            self._schedule_call(1.5, self._clear_leds)
        else:
            self.led_red = True
            self.trigger_buzzer(80)
            self._schedule_call(1.5, self._clear_leds)
            if self.is_connected:
                try:
                    uid_bytes = bytes.fromhex(clean_uid)
                    cloud_payload = bytes([0x80, len(uid_bytes)]) + uid_bytes
                    self.emit_notification(RX_CHAR_UUID, bytes([0x08, 203]) + cloud_payload, encrypt=self.is_logged_in)
                except ValueError:
                    pass
        self.notify_state_changed()

    def scan_fingerprint(self, matched: bool = True) -> None:
        """Simulates biometric fingerprint scan."""
        logger.info("[%s] Fingerprint scanned (match=%s)", self.model_name, matched)
        if matched:
            self.led_green = True
            self.trigger_buzzer(100)
            if self.linked_lock:
                self.linked_lock.unlock(history_name="Fingerprint")
        else:
            self.led_red = True
            self.trigger_buzzer(80)
        self._schedule_call(1.5, self._clear_leds)
        self.notify_state_changed()

    def scan_face(self, matched: bool = True) -> None:
        """Simulates biometric face recognition."""
        logger.info("[%s] Face scanned (match=%s)", self.model_name, matched)
        if matched:
            self.led_green = True
            self.trigger_buzzer(100)
            if self.linked_lock:
                self.linked_lock.unlock(history_name="Face Recognition")
        else:
            self.led_red = True
            self.trigger_buzzer(80)
        self._schedule_call(1.5, self._clear_leds)
        self.notify_state_changed()

    def _clear_leds(self) -> None:
        self.led_red = False
        self.led_blue = False
        self.led_green = False
        self.notify_state_changed()

    def get_state(self) -> dict:
        s = super().get_state()
        s.update({
            "keypad_buffer": self.keypad_input,
            "buzzer": self.buzzer_active,
            "led_red": self.led_red,
            "led_blue": self.led_blue,
            "led_green": self.led_green,
            "passcode_count": len(self.registered_passcodes),
            "card_count": len(self.registered_cards),
            "fingerprint_count": len(self.registered_fingerprints),
            "face_count": len(self.registered_faces),
        })
        return s


class SimulatedSesameTouch2Pro(SimulatedSesameKeypad):
    """Simulates Sesame Touch 2 Pro Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_touch_2_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:26",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_touch_2_pro",
            product_type=26,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-9-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameTouch(SimulatedSesameKeypad):
    """Simulates Sesame Touch Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_touch/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:10",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_touch",
            product_type=10,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-10-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameTouchPro(SimulatedSesameKeypad):
    """Simulates Sesame Touch Pro Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_touch_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:09",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_touch_pro",
            product_type=9,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-9-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameTouch2(SimulatedSesameKeypad):
    """Simulates Sesame Touch 2 Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_touch_2/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:25",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_touch_2",
            product_type=25,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-10-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFace(SimulatedSesameKeypad):
    """Simulates Sesame Face Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:19",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face",
            product_type=19,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-19-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFacePro(SimulatedSesameKeypad):
    """Simulates Sesame Face Pro Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:18",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_pro",
            product_type=18,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-18-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFaceAI(SimulatedSesameKeypad):
    """Simulates Sesame Face AI Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_ai/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:23",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_ai",
            product_type=23,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-23-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFaceProAI(SimulatedSesameKeypad):
    """Simulates Sesame Face Pro AI Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_pro_ai/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:22",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_pro_ai",
            product_type=22,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-22-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFace2(SimulatedSesameKeypad):
    """Simulates Sesame Face 2 Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_2/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:27",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_2",
            product_type=27,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-19-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFace2Pro(SimulatedSesameKeypad):
    """Simulates Sesame Face 2 Pro Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_2_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:28",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_2_pro",
            product_type=28,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-18-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFace2AI(SimulatedSesameKeypad):
    """Simulates Sesame Face 2 AI Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_2_ai/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:30",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_2_ai",
            product_type=30,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-23-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameFace2ProAI(SimulatedSesameKeypad):
    """Simulates Sesame Face 2 Pro AI Keypad running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_face_2_pro_ai/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:31",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_face_2_pro_ai",
            product_type=31,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-22-e877d5",
            secret_key=secret_key,
        )


class SimulatedSesameLock(SimulatedFirmwareDevice):
    """Base class for Sesame Lock devices running authentic firmware."""

    def __init__(
        self,
        model_name: str,
        product_type: int,
        firmware_path: str,
        base_address: int,
        ble_address: str,
        firmware_version: str,
        secret_key: bytes | None = None,
    ) -> None:
        self.current_angle: float = 0.0
        self.target_angle: float = 0.0
        self.locked_angle: float = 0.0
        self.unlocked_angle: float = 90.0
        self.is_locked: bool = True
        self.motor_running: bool = False
        self.motor_direction: int = 0
        self.battery_voltage: float = 6.0
        self.auto_lock_second: int = 0
        self.ops_lock_second: int = 0
        self.history_records: list[bytes] = []
        self._next_history_id: int = 1
        self._expected_firmware_version = firmware_version

        super().__init__(
            model_name=model_name,
            product_type=product_type,
            firmware_path=firmware_path,
            base_address=base_address,
            ble_address=ble_address,
            secret_key=secret_key,
        )

    def _setup_hooks(self) -> None:
        self.firmware_version = self._expected_firmware_version

    def _add_history_record(self, history_type: int, tag_name: str = "Client") -> None:
        name_bytes = tag_name.encode("utf-8")[:20]
        record_id = self._next_history_id
        self._next_history_id += 1
        ts = int(time.time())
        mech_status = self._build_mech_status_payload()
        param = struct.pack("<HB", 1, len(name_bytes)) + name_bytes
        record = struct.pack("<IBI7s", record_id, history_type, ts, mech_status) + param
        self.history_records.append(record)
        if len(self.history_records) > 50:
            self.history_records.pop(0)

    def lock(self, history_name: str = "Client") -> None:
        """Triggers lock command."""
        logger.info("[%s] Lock command received. Driving motor towards locked angle (%.1f°)", self.model_name, self.locked_angle)
        self.target_angle = self.locked_angle
        self.motor_direction = -1 if self.current_angle > self.locked_angle else 1
        self.motor_running = True
        self._add_history_record(1, history_name)
        self.notify_state_changed()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._drive_motor_animation())
        except RuntimeError:
            self.current_angle = self.target_angle
            self.motor_running = False
            self.is_locked = True
            self.notify_state_changed()

    def unlock(self, history_name: str = "Client") -> None:
        """Triggers unlock command."""
        logger.info("[%s] Unlock command received. Driving motor towards unlocked angle (%.1f°)", self.model_name, self.unlocked_angle)
        self.target_angle = self.unlocked_angle
        self.motor_direction = 1 if self.target_angle > self.current_angle else -1
        self.motor_running = True
        self._add_history_record(2, history_name)
        self.notify_state_changed()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._drive_motor_animation())
        except RuntimeError:
            self.current_angle = self.target_angle
            self.motor_running = False
            self.is_locked = False
            self.notify_state_changed()

    def _build_mech_status_payload(self) -> bytes:
        raw_battery = int(self.battery_voltage * 1000 / 2)
        target = int(self.target_angle)
        position = int(self.current_angle)
        flags = 0
        if self.is_locked:
            flags |= 0x02
        else:
            flags |= 0x04
        if not self.motor_running:
            flags |= 0x10
        return struct.pack("<HhhB", raw_battery, target, position, flags)

    async def _drive_motor_animation(self) -> None:
        """Simulates physical motor movement over time."""
        step_deg = 15.0
        while self.motor_running:
            await asyncio.sleep(0.05)
            diff = self.target_angle - self.current_angle
            if abs(diff) <= step_deg:
                self.current_angle = self.target_angle
                self.motor_running = False
            else:
                self.current_angle += (step_deg if diff > 0 else -step_deg)

            self.is_locked = abs(self.current_angle - self.locked_angle) < 15.0
            self.notify_state_changed()

        payload = bytes([0x08, 0x51]) + self._build_mech_status_payload()
        self.emit_notification(RX_CHAR_UUID, payload, encrypt=self.is_logged_in)

    def set_manual_angle(self, angle: float) -> None:
        """Handles manual thumbturn rotation by the user."""
        self.current_angle = angle % 360.0
        self.is_locked = abs(self.current_angle - self.locked_angle) < 15.0
        self._add_history_record(3, "Manual Turn")
        logger.info("[%s] Manual thumbturn rotated to %.1f° (locked=%s)", self.model_name, self.current_angle, self.is_locked)
        self.notify_state_changed()

        payload = bytes([0x08, 0x51]) + self._build_mech_status_payload()
        self.emit_notification(RX_CHAR_UUID, payload, encrypt=self.is_logged_in)

    def process_gatt_write(self, char_uuid: str, packet: bytes) -> None:
        """Handles GATT writes for Sesame 6 Pro."""
        res = self.feed_packet(packet)
        if not res:
            return

        payload, is_encrypted = res
        if not payload:
            return

        item_code = payload[0]
        body = payload[1:]

        logger.info("[%s] Inbound command: item=%d, len=%d, encrypted=%s", self.model_name, item_code, len(body), is_encrypted)

        if item_code == 2:  # ITEM_LOGIN
            expected_session_key = derive_session_token_key(self.secret_key, self.current_token)
            if body[:4] != expected_session_key[:4]:
                logger.warning("[%s] Login rejected: session key mismatch!", self.model_name)
                login_err_resp = bytes([0x07, 0x02, 0x05])
                self.emit_notification(RX_CHAR_UUID, login_err_resp, encrypt=False)
                return

            self.setup_cipher()
            # 1. Send OP_RESPONSE for ITEM_LOGIN with 4-byte timestamp
            login_resp = bytes([0x07, 0x02, 0x00]) + struct.pack("<I", int(time.time()))
            self.emit_notification(RX_CHAR_UUID, login_resp, encrypt=False)
            # 2. Publish initial mechanical status
            mech_resp = bytes([0x08, 0x51]) + self._build_mech_status_payload()
            self.emit_notification(RX_CHAR_UUID, mech_resp, encrypt=False)
            # 3. Publish mechanical settings (ITEM_MECH_SETTING = 80 / 0x50)
            setting_payload = struct.pack("<hhH", int(self.locked_angle), int(self.unlocked_angle), self.auto_lock_second)
            setting_resp = bytes([0x08, 0x50]) + setting_payload
            self.emit_notification(RX_CHAR_UUID, setting_resp, encrypt=False)

        elif item_code == 5:  # ITEM_VERSION_TAG
            resp = bytes([0x07, 0x05, 0x00]) + self.firmware_version.encode("ascii")
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 8:  # ITEM_TIME
            resp = bytes([0x07, 0x08, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 80:  # ITEM_MECH_SETTING
            if len(body) >= 4:
                lock_pos, unlock_pos = struct.unpack("<hh", body[:4])
                self.locked_angle = float(lock_pos)
                self.unlocked_angle = float(unlock_pos)
            if len(body) >= 6:
                self.auto_lock_second = struct.unpack("<H", body[4:6])[0]
            resp = bytes([0x07, 80, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            setting_payload = struct.pack("<hhH", int(self.locked_angle), int(self.unlocked_angle), self.auto_lock_second)
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 0x50]) + setting_payload, encrypt=is_encrypted)

        elif item_code == 11:  # ITEM_AUTOLOCK
            if len(body) >= 2:
                self.auto_lock_second = struct.unpack("<H", body[:2])[0]
            resp = bytes([0x07, 11, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            setting_payload = struct.pack("<hhH", int(self.locked_angle), int(self.unlocked_angle), self.auto_lock_second)
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 0x50]) + setting_payload, encrypt=is_encrypted)

        elif item_code == 92:  # ITEM_OPS_TIMER_SETTING
            if len(body) >= 2:
                self.ops_lock_second = struct.unpack("<H", body[:2])[0]
            resp = bytes([0x07, 92, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 92]) + struct.pack("<H", self.ops_lock_second), encrypt=is_encrypted)

        elif item_code == 17:  # ITEM_MAGNET
            self.current_angle = self.locked_angle
            self.is_locked = True
            resp = bytes([0x07, 17, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)
            self.emit_notification(RX_CHAR_UUID, bytes([0x08, 0x51]) + self._build_mech_status_payload(), encrypt=is_encrypted)

        elif item_code == 4:  # ITEM_HISTORY
            if self.history_records:
                record = self.history_records[0]
                resp = bytes([0x07, 4, 0x00]) + record
            else:
                resp = bytes([0x07, 4, 0x05])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 18:  # ITEM_HISTORY_DELETE
            if self.history_records:
                self.history_records.pop(0)
            resp = bytes([0x07, 18, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 82:  # ITEM_LOCK
            tag_name = "Client"
            if len(body) >= 1:
                tlen = body[0]
                if len(body) >= 1 + tlen:
                    tag_name = body[1 : 1 + tlen].decode("utf-8", "replace")
            self.lock(history_name=tag_name)
            resp = bytes([0x07, 82, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        elif item_code == 83:  # ITEM_UNLOCK
            tag_name = "Client"
            if len(body) >= 1:
                tlen = body[0]
                if len(body) >= 1 + tlen:
                    tag_name = body[1 : 1 + tlen].decode("utf-8", "replace")
            self.unlock(history_name=tag_name)
            resp = bytes([0x07, 83, 0x00])
            self.emit_notification(RX_CHAR_UUID, resp, encrypt=is_encrypted)

        self.notify_state_changed()

    def get_state(self) -> dict:
        s = super().get_state()
        s.update({
            "angle": self.current_angle,
            "target_angle": self.target_angle,
            "locked_angle": self.locked_angle,
            "unlocked_angle": self.unlocked_angle,
            "is_locked": self.is_locked,
            "motor_running": self.motor_running,
            "battery_voltage": self.battery_voltage,
            "auto_lock_second": self.auto_lock_second,
            "ops_lock_second": self.ops_lock_second,
            "history_count": len(self.history_records),
        })
        return s


class SimulatedSesame6Pro(SimulatedSesameLock):
    """Simulates Sesame 6 Pro Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame6_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:21",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame6_pro",
            product_type=21,
            firmware_path=firmware_path,
            base_address=0x00404000,
            ble_address=ble_address,
            firmware_version="3.0-21-956bb2",
            secret_key=secret_key,
        )


class SimulatedSesame5(SimulatedSesameLock):
    """Simulates Sesame 5 Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame5/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:05",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame5",
            product_type=5,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-5-3bfc1c",
            secret_key=secret_key,
        )


class SimulatedSesame5Pro(SimulatedSesameLock):
    """Simulates Sesame 5 Pro Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame5_pro/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:07",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame5_pro",
            product_type=7,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-7-3bfc1c",
            secret_key=secret_key,
        )


class SimulatedSesame5USA(SimulatedSesameLock):
    """Simulates Sesame 5 USA Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame5_usa/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:16",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame5_usa",
            product_type=16,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-16-3bfc1c",
            secret_key=secret_key,
        )


class SimulatedSesame6(SimulatedSesameLock):
    """Simulates Sesame 6 Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame6/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:20",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame6",
            product_type=20,
            firmware_path=firmware_path,
            base_address=0x00404000,
            ble_address=ble_address,
            firmware_version="3.0-20-956bb2",
            secret_key=secret_key,
        )


class SimulatedSesame6ProSlidingDoor(SimulatedSesameLock):
    """Simulates Sesame 6 Pro Sliding Door Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame6_pro_sliding_door/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:32",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame6_pro_sliding_door",
            product_type=32,
            firmware_path=firmware_path,
            base_address=0x00404000,
            ble_address=ble_address,
            firmware_version="3.0-21-956bb2",
            secret_key=secret_key,
        )


class SimulatedSesameBike2(SimulatedSesameLock):
    """Simulates Sesame Bike 2 / Cycle 2 Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_bike2/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:06",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_bike2",
            product_type=6,
            firmware_path=firmware_path,
            base_address=0x00403000,
            ble_address=ble_address,
            firmware_version="3.0-6-3bfc1c",
            secret_key=secret_key,
        )


class SimulatedSesameBike3(SimulatedSesameLock):
    """Simulates Sesame Bike 3 / Cycle 3 Lock running authentic firmware."""

    def __init__(
        self,
        firmware_path: str = "firmware/sesame_bike3/firmware.bin",
        ble_address: str = "FD:81:AA:BB:CC:33",
        secret_key: bytes | None = None,
    ) -> None:
        super().__init__(
            model_name="sesame_bike3",
            product_type=33,
            firmware_path=firmware_path,
            base_address=0x00404000,
            ble_address=ble_address,
            firmware_version="3.0-33-d96ebc",
            secret_key=secret_key,
        )
