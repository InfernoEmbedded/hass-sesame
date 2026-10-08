"""Nordic Semiconductor Secure DFU Protocol Implementation for Sesame BLE devices."""

import asyncio
import binascii
import io
import json
import logging
import struct
import zipfile
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

logger = logging.getLogger(__name__)

# Nordic Semiconductor Secure DFU UUIDs
DFU_SERVICE_UUID = "0000fe59-0000-1000-8000-00805f9b34fb"
DFU_CONTROL_POINT_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
DFU_PACKET_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"
DFU_BUTTONLESS_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"
DFU_BUTTONLESS_BOND_UUID = "8ec90004-f315-4f60-9fb8-838830daea50"

# DFU Control Point Operation Codes
OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALCULATE_CHECKSUM = 0x03
OP_EXECUTE = 0x04
OP_SELECT_OBJECT = 0x06
OP_RESPONSE = 0x60

# DFU Object Types
OBJ_COMMAND = 0x01  # Init packet (.dat)
OBJ_DATA = 0x02     # Application firmware image (.bin)

# DFU Result Codes
RES_SUCCESS = 0x01
RES_OP_NOT_SUPPORTED = 0x02
RES_INVALID_PARAM = 0x03
RES_INSUFFICIENT_RESOURCES = 0x04
RES_INVALID_OBJECT = 0x05
RES_UNSUPPORTED_TYPE = 0x07
RES_OPERATION_NOT_PERMITTED = 0x08
RES_OPERATION_FAILED = 0x0A
RES_EXTENDED_ERROR = 0x0B

RESULT_CODE_NAMES = {
    RES_SUCCESS: "Success",
    RES_OP_NOT_SUPPORTED: "Op Code not supported",
    RES_INVALID_PARAM: "Invalid parameter",
    RES_INSUFFICIENT_RESOURCES: "Insufficient resources",
    RES_INVALID_OBJECT: "Invalid object",
    RES_UNSUPPORTED_TYPE: "Unsupported type",
    RES_OPERATION_NOT_PERMITTED: "Operation not permitted",
    RES_OPERATION_FAILED: "Operation failed",
    RES_EXTENDED_ERROR: "Extended error",
}

EXTENDED_ERROR_NAMES = {
    0x02: "Wrong command format",
    0x03: "Unknown command",
    0x04: "Init command invalid",
    0x05: "Firmware version failure",
    0x06: "Hardware version failure",
    0x07: "SoftDevice version failure",
    0x08: "Signature missing",
    0x09: "Wrong hash type",
    0x0A: "Hash failed",
    0x0B: "Wrong signature type",
    0x0C: "Verification failed",
    0x0D: "Insufficient space",
}


class DfuError(Exception):
    """Exception raised for Nordic DFU failures."""


class DfuRemoteError(DfuError):
    """Exception raised when the remote DFU bootloader returns an error code."""

    def __init__(self, op_code: int, result_code: int, ext_code: int | None = None) -> None:
        name = RESULT_CODE_NAMES.get(result_code, f"Unknown ({result_code})")
        msg = f"Remote DFU error during OpCode 0x{op_code:02X}: {name}"
        if result_code == RES_EXTENDED_ERROR and ext_code is not None:
            ext_name = EXTENDED_ERROR_NAMES.get(ext_code, f"Unknown (0x{ext_code:02X})")
            msg += f" [Extended Error: {ext_name}]"
        super().__init__(msg)
        self.op_code = op_code
        self.result_code = result_code
        self.ext_code = ext_code


def get_bootloader_mac(mac: str) -> str:
    """Computes Nordic bootloader MAC address (original MAC with last byte incremented by 1).
    
    In unbonded DFU (keepBond=false), standard Nordic bootloaders increment the last byte
    of the BLE MAC address by 1 modulo 256.
    """
    parts = mac.strip().split(":")
    if len(parts) == 6:
        last_byte = (int(parts[5], 16) + 1) & 0xFF
        parts[5] = f"{last_byte:02X}"
        return ":".join(parts)
    return mac


def parse_dfu_zip(zip_bytes: bytes) -> tuple[bytes, bytes]:
    """Parses a Nordic DFU zip archive and extracts (init_packet, firmware_data).
    
    Follows Nordic DFU manifest.json structure or falls back to finding .dat and .bin files.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        namelist = zf.namelist()
        init_file = None
        bin_file = None

        if "manifest.json" in namelist:
            try:
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
                manifest = manifest_data.get("manifest", {})
                app_info = manifest.get("application") or manifest.get("bootloader") or manifest.get("softdevice")
                if app_info:
                    init_file = app_info.get("dat_file")
                    bin_file = app_info.get("bin_file")
            except Exception as err:
                logger.warning("Could not parse manifest.json: %s. Using file scanning.", err)

        if not init_file or not bin_file:
            for name in namelist:
                if name.endswith(".dat"):
                    init_file = name
                elif name.endswith(".bin"):
                    bin_file = name

        if not init_file or not bin_file:
            raise DfuError(f"DFU package does not contain required init and bin files. Found: {namelist}")

        init_packet = zf.read(init_file)
        firmware_data = zf.read(bin_file)
        logger.info(
            "Parsed DFU package: init=%s (%d bytes), bin=%s (%d bytes)",
            init_file,
            len(init_packet),
            bin_file,
            len(firmware_data),
        )
        return init_packet, firmware_data


class NordicSecureDfuClient:
    """Implements the Nordic Semiconductor Secure DFU BLE protocol."""

    def __init__(
        self,
        client: BleakClient,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        self.client = client
        self.progress_callback = progress_callback
        self._pending_response_op: int | None = None
        self._pending_future: asyncio.Future[bytes] | None = None
        self._notification_lock = asyncio.Lock()
        self._prn_queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        self._is_active = False

    def _on_notification(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        del char
        raw = bytes(data)
        logger.info("DFU Notification received: %s", raw.hex())

        # Check for unsolicited PRN notifications in raw format: 0x11 <offset:4> <crc:4>
        if len(raw) >= 9 and raw[0] == 0x11:
            offset, crc = struct.unpack("<II", raw[1:9])
            logger.info("PRN notification received (0x11): offset=%d, crc=0x%08X", offset, crc)
            self._prn_queue.put_nowait((offset, crc))
            return

        if len(raw) < 3 or raw[0] != OP_RESPONSE:
            logger.warning("Unexpected DFU notification: %s", raw.hex())
            return

        op_code = raw[1]
        result_code = raw[2]

        # Check for unsolicited PRN notifications (OP_CALCULATE_CHECKSUM 0x03, 0x08, or OP_SET_PRN with offset and CRC)
        if op_code in (OP_CALCULATE_CHECKSUM, 0x08, OP_SET_PRN) and (self._pending_response_op is None or self._pending_response_op != op_code):
            if len(raw) >= 11:
                offset, crc = struct.unpack("<II", raw[3:11])
                logger.info("PRN notification received (0x60 0x03): offset=%d, crc=0x%08X", offset, crc)
                self._prn_queue.put_nowait((offset, crc))
                return
            elif self._pending_response_op is None:
                logger.debug("Ignored non-PRN unsolicited notification: %s", raw.hex())
                return

        if self._pending_future and not self._pending_future.done():
            if self._pending_response_op is None or self._pending_response_op == op_code:
                if result_code != RES_SUCCESS:
                    ext_code = raw[3] if len(raw) > 3 else None
                    self._pending_future.set_exception(
                        DfuRemoteError(op_code, result_code, ext_code)
                    )
                else:
                    self._pending_future.set_result(raw)

    async def _send_op_code(
        self,
        op_code: int,
        params: bytes = b"",
        timeout: float = 30.0,
    ) -> bytes:
        """Sends a request to the Control Point characteristic and awaits response notification."""
        async with self._notification_lock:
            loop = asyncio.get_running_loop()
            self._pending_response_op = op_code
            self._pending_future = loop.create_future()

            payload = bytes([op_code]) + params
            logger.debug("Writing DFU Control Point: %s", payload.hex())
            await self.client.write_gatt_char(DFU_CONTROL_POINT_UUID, payload, response=True)

            try:
                return await asyncio.wait_for(self._pending_future, timeout=timeout)
            finally:
                self._pending_future = None
                self._pending_response_op = None

    async def _wait_for_prn(self, timeout: float = 30.0) -> tuple[int, int]:
        """Waits for Packet Receipt Notification (PRN) from device."""
        return await asyncio.wait_for(self._prn_queue.get(), timeout=timeout)

    async def select_object(self, obj_type: int) -> tuple[int, int, int]:
        """Sends Select Object command (OpCode 6). Returns (max_size, offset, crc32)."""
        logger.debug("Selecting object type %d", obj_type)
        resp = await self._send_op_code(OP_SELECT_OBJECT, bytes([obj_type]))
        if len(resp) < 15:
            raise DfuError(f"Select Object response too short: {resp.hex()}")
        max_size, offset, crc = struct.unpack("<III", resp[3:15])
        logger.debug(
            "Selected object type %d: max_size=%d, offset=%d, crc=0x%08X",
            obj_type,
            max_size,
            offset,
            crc,
        )
        return max_size, offset, crc

    async def create_object(self, obj_type: int, size: int) -> None:
        """Sends Create Object command (OpCode 1)."""
        logger.debug("Creating object type %d of size %d", obj_type, size)
        await self._send_op_code(OP_CREATE, bytes([obj_type]) + struct.pack("<I", size))

    async def set_prn(self, prn: int) -> None:
        """Sends Set Packet Receipt Notification command (OpCode 2)."""
        logger.debug("Setting PRN to %d", prn)
        await self._send_op_code(OP_SET_PRN, struct.pack("<H", prn))

    async def calculate_checksum(self) -> tuple[int, int]:
        """Sends Calculate Checksum command (OpCode 3). Returns (offset, crc32)."""
        resp = await self._send_op_code(OP_CALCULATE_CHECKSUM)
        if len(resp) < 11:
            raise DfuError(f"Calculate Checksum response too short: {resp.hex()}")
        offset, crc = struct.unpack("<II", resp[3:11])
        return offset, crc

    async def execute(self, timeout: float = 30.0) -> None:
        """Sends Execute command (OpCode 4)."""
        logger.debug("Executing object")
        await self._send_op_code(OP_EXECUTE, timeout=timeout)

    async def write_packet(self, data: bytes, retries: int = 3) -> None:
        """Writes data packet to Packet characteristic without response with retry on congestion."""
        for attempt in range(retries):
            try:
                await self.client.write_gatt_char(DFU_PACKET_UUID, data, response=False)
                return
            except Exception as err:
                if attempt == retries - 1:
                    raise
                logger.warning(
                    "Packet write failed (attempt %d/%d): %s (is_connected=%s); retrying after 100ms...",
                    attempt + 1,
                    retries,
                    err,
                    getattr(self.client, "is_connected", None),
                )
                await asyncio.sleep(0.1)

    def _get_chunk_size(self) -> int:
        """Determines BLE payload packet size.
        
        The official Candy House Android app (DfuCenter / SecureDfuImpl) never requests
        MTU expansion during Secure DFU, keeping ATT MTU at 23 (20-byte payload).
        The OnMicro OM6621 / HS6621 chipset emulating Nordic DFU overflows and
        disconnects if packets larger than the GATT attribute buffer (64 bytes) or MTU
        (244 bytes) are streamed. Therefore, chunk size is strictly 20 bytes.
        """
        return 20

    async def send_init_packet(self, init_packet: bytes) -> None:
        """Sends signed init packet (Command Object)."""
        logger.info("Transferring init packet (%d bytes)...", len(init_packet))
        expected_crc = binascii.crc32(init_packet)

        # Select Command Object
        await self.select_object(OBJ_COMMAND)
        await asyncio.sleep(0.2)

        # Create Command Object
        await self.create_object(OBJ_COMMAND, len(init_packet))
        await asyncio.sleep(0.2)

        # Write init packet chunks
        chunk_size = self._get_chunk_size()
        for i in range(0, len(init_packet), chunk_size):
            chunk = init_packet[i : i + chunk_size]
            await self.write_packet(chunk)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.2)

        # Verify Checksum
        offset, crc = await self.calculate_checksum()
        if offset != len(init_packet) or crc != expected_crc:
            raise DfuError(
                f"Init packet checksum failed: expected offset={len(init_packet)}, crc=0x{expected_crc:08X}; "
                f"got offset={offset}, crc=0x{crc:08X}"
            )

        await asyncio.sleep(0.2)

        # Execute Command Object
        await self.execute()
        logger.info("Init packet executed successfully.")
        await asyncio.sleep(0.25)

    async def send_firmware_data(self, firmware_data: bytes) -> None:
        """Transfers the application binary in Data Object chunks with flash page aware pacing."""
        total_len = len(firmware_data)
        logger.info("Transferring firmware binary (%d bytes)...", total_len)

        max_object_size, curr_offset, curr_crc = await self.select_object(OBJ_DATA)
        logger.info(
            "Data object info from bootloader: max_size=%d, offset=%d, crc=0x%08X",
            max_object_size,
            curr_offset,
            curr_crc,
        )

        # Candy House single-bank bootloader partitions flash cache for the entire image
        # (reporting max_object_size = 268435456 / 0x10000000) and requires creating a single
        # data object covering the full image size (total_len). Creating smaller sub-objects
        # (e.g. 4096) triggers NRF_DFU_RES_CODE_INVALID_OBJECT (0x05).
        if max_object_size <= 0 or max_object_size >= total_len:
            max_object_size = total_len

        # Configure Packet Receipt Notifications (PRN = 12) matching the official Android app.
        # This provides hardware-paced flow control and ensures the bootloader buffer pool
        # never overflows while keeping total transfer time under ~60 seconds.
        prn_interval = 12
        try:
            await self.set_prn(prn_interval)
            logger.info("PRN enabled (interval=%d) for hardware-synchronized streaming", prn_interval)
        except Exception as err:
            logger.warning("Failed to set PRN to %d (%s); continuing with prn_interval=0", prn_interval, err)
            prn_interval = 0

        chunk_size = self._get_chunk_size()
        num_objects = (total_len + max_object_size - 1) // max_object_size

        logger.info(
            "Firmware transfer plan: %d bytes divided into %d data objects (max_size=%d, packet_size=%d, prn=%d)",
            total_len,
            num_objects,
            max_object_size,
            chunk_size,
            prn_interval,
        )

        start_obj = 0
        if curr_offset > 0 and curr_offset <= total_len and curr_offset % max_object_size == 0:
            expected_crc = binascii.crc32(firmware_data[:curr_offset]) & 0xFFFFFFFF
            if curr_crc == expected_crc:
                start_obj = curr_offset // max_object_size
                logger.info(
                    "Resuming firmware transfer from byte %d (object %d/%d, crc=0x%08X)",
                    curr_offset,
                    start_obj + 1,
                    num_objects,
                    curr_crc,
                )

        bytes_sent = start_obj * max_object_size

        for obj_idx in range(start_obj, num_objects):
            obj_start = obj_idx * max_object_size
            obj_end = min(obj_start + max_object_size, total_len)
            obj_data = firmware_data[obj_start:obj_end]
            obj_len = len(obj_data)

            max_obj_attempts = 3
            for obj_attempt in range(1, max_obj_attempts + 1):
                # 1. Create Data Object
                logger.info(
                    "Creating data object %d/%d (offset=%d, size=%d, attempt %d/%d)...",
                    obj_idx + 1,
                    num_objects,
                    obj_start,
                    obj_len,
                    obj_attempt,
                    max_obj_attempts,
                )
                await self.create_object(OBJ_DATA, obj_len)

                # Wait 400ms for flash controller to initialize target bank
                # (matching Sesame Android app setPrepareDataObjectDelay(400L))
                logger.info("Waiting 400ms for bootloader flash preparation...")
                await asyncio.sleep(0.4)

                # 2. Drain any leftover PRN notifications before streaming
                while not self._prn_queue.empty():
                    self._prn_queue.get_nowait()

                # 3. Stream packets for this data object with PRN hardware flow control
                bytes_sent_in_obj = 0
                total_pkts = (obj_len + chunk_size - 1) // chunk_size

                for pkt_idx, i in enumerate(range(0, obj_len, chunk_size)):
                    packet = obj_data[i : i + chunk_size]
                    if pkt_idx % 100 == 0 or pkt_idx == total_pkts - 1:
                        logger.info(
                            "Streaming packet %d/%d (offset=%d/%d, is_connected=%s)...",
                            pkt_idx + 1,
                            total_pkts,
                            obj_start + bytes_sent_in_obj,
                            total_len,
                            getattr(self.client, "is_connected", None),
                        )
                    await self.write_packet(packet)
                    bytes_sent_in_obj += len(packet)
                    current_total_sent = obj_start + bytes_sent_in_obj
                    if self.progress_callback and total_len > 0:
                        self.progress_callback((current_total_sent / total_len) * 100.0)

                    # Flow control: wait for PRN notification every prn_interval packets
                    if prn_interval > 0 and (pkt_idx + 1) % prn_interval == 0:
                        try:
                            prn_offset, prn_crc = await asyncio.wait_for(
                                self._prn_queue.get(), timeout=5.0
                            )
                            if (pkt_idx + 1) % (prn_interval * 10) == 0 or (pkt_idx + 1) == (total_pkts // prn_interval) * prn_interval:
                                logger.info(
                                    "PRN verified at packet %d/%d (offset=%d/%d, crc=0x%08X)",
                                    pkt_idx + 1,
                                    total_pkts,
                                    prn_offset,
                                    total_len,
                                    prn_crc,
                                )
                            else:
                                logger.debug(
                                    "PRN verified at packet %d/%d (offset=%d, crc=0x%08X)",
                                    pkt_idx + 1,
                                    total_pkts,
                                    prn_offset,
                                    prn_crc,
                                )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "PRN notification timed out at packet %d/%d; querying checksum...",
                                pkt_idx + 1,
                                total_pkts,
                            )
                            prn_offset, prn_crc = await self.calculate_checksum()
                            logger.info("Checksum fallback response: offset=%d, crc=0x%08X", prn_offset, prn_crc)
                    else:
                        await asyncio.sleep(0.015)

                # 4. Allow brief pause for flash controller to commit final buffer to physical flash
                logger.info(
                    "All packets transferred for object %d/%d (%d bytes). Waiting 0.15s for flash commit...",
                    obj_idx + 1,
                    num_objects,
                    obj_len,
                )
                await asyncio.sleep(0.15)

                # 5. Verify Final Checksum for this object
                expected_cumulative_crc = binascii.crc32(firmware_data[:obj_end]) & 0xFFFFFFFF
                logger.info(
                    "Verifying final checksum for %d bytes (expected CRC=0x%08X)...",
                    obj_end,
                    expected_cumulative_crc,
                )
                offset, crc = await self.calculate_checksum()
                logger.info("Final checksum response: offset=%d, crc=0x%08X", offset, crc)

                if offset == obj_end and crc == expected_cumulative_crc:
                    break  # Checksum passed

                logger.warning(
                    "Checksum mismatch on data object %d/%d (attempt %d/%d): "
                    "expected offset=%d, crc=0x%08X; got offset=%d, crc=0x%08X",
                    obj_idx + 1,
                    num_objects,
                    obj_attempt,
                    max_obj_attempts,
                    obj_end,
                    expected_cumulative_crc,
                    offset,
                    crc,
                )
                if obj_attempt == max_obj_attempts:
                    raise DfuError(
                        f"Checksum mismatch on data object {obj_idx + 1}/{num_objects}: "
                        f"expected offset={obj_end}, crc=0x{expected_cumulative_crc:08X}; "
                        f"got offset={offset}, crc=0x{crc:08X}"
                    )
                await asyncio.sleep(0.5)

            # 5. Execute Data Object
            is_last = (obj_idx == num_objects - 1)
            try:
                await self.execute(timeout=5.0 if is_last else 10.0)
                logger.info("Data object %d/%d executed successfully.", obj_idx + 1, num_objects)
                if is_last and self.progress_callback:
                    self.progress_callback(100.0)
            except Exception as err:
                if is_last:
                    # Final execute validates whole image and reboots into application mode,
                    # which often drops the BLE link immediately.
                    logger.info("Final execute triggered application reboot (connection closed as expected): %s", err)
                    if self.progress_callback:
                        self.progress_callback(100.0)
                    return
                raise

            bytes_sent = obj_end

        logger.info("Firmware transfer completed successfully (%d bytes).", total_len)

    async def update(self, zip_bytes: bytes) -> None:
        """Executes full Secure DFU sequence from raw zip bytes."""
        init_packet, firmware_data = parse_dfu_zip(zip_bytes)

        logger.info("Subscribing to DFU Control Point notifications...")
        await self.client.start_notify(DFU_CONTROL_POINT_UUID, self._on_notification)

        try:
            # Phase 1: Send Init Packet
            await self.send_init_packet(init_packet)

            # Phase 2: Send Firmware Data
            await self.send_firmware_data(firmware_data)
        finally:
            try:
                await self.client.stop_notify(DFU_CONTROL_POINT_UUID)
            except Exception:
                pass


async def perform_nordic_dfu(
    client: BleakClient,
    zip_bytes: bytes,
    progress_callback: Callable[[float], None] | None = None,
) -> None:
    """High-level helper to execute Nordic Secure DFU update over an established BleakClient."""
    dfu_client = NordicSecureDfuClient(client, progress_callback=progress_callback)
    await dfu_client.update(zip_bytes)
