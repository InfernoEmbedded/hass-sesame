"""Unit tests for Nordic Secure DFU module."""

import asyncio
import binascii
import io
import json
import struct
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from custom_components.sesame_ble.nordic_dfu import (
    DFU_CONTROL_POINT_UUID,
    DFU_PACKET_UUID,
    DfuError,
    DfuRemoteError,
    NordicSecureDfuClient,
    OBJ_COMMAND,
    OBJ_DATA,
    OP_CALCULATE_CHECKSUM,
    OP_CREATE,
    OP_EXECUTE,
    OP_RESPONSE,
    OP_SELECT_OBJECT,
    OP_SET_PRN,
    RES_EXTENDED_ERROR,
    RES_INSUFFICIENT_RESOURCES,
    RES_SUCCESS,
    get_bootloader_mac,
    parse_dfu_zip,
    perform_nordic_dfu,
)


def create_test_zip(
    manifest: dict | None = None,
    init_data: bytes = b"init_packet_content",
    bin_data: bytes = b"firmware_binary_content" * 100,
    include_manifest: bool = True,
    init_name: str = "firmware.dat",
    bin_name: str = "firmware.bin",
) -> bytes:
    """Creates an in-memory zip file for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if include_manifest:
            if manifest is None:
                manifest = {
                    "manifest": {
                        "application": {
                            "bin_file": bin_name,
                            "dat_file": init_name,
                        }
                    }
                }
            zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(init_name, init_data)
        zf.writestr(bin_name, bin_data)
    return buf.getvalue()


def test_get_bootloader_mac():
    """Verify MAC address increment for Nordic unbonded DFU bootloader."""
    assert get_bootloader_mac("AA:BB:CC:DD:EE:01") == "AA:BB:CC:DD:EE:02"
    assert get_bootloader_mac("AA:BB:CC:DD:EE:09") == "AA:BB:CC:DD:EE:0A"
    assert get_bootloader_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:00"
    assert get_bootloader_mac("invalid_mac") == "invalid_mac"


def test_parse_dfu_zip_with_manifest():
    """Verify parsing zip package with manifest.json."""
    init_content = b"\x01\x02\x03\x04"
    bin_content = b"APP_BINARY_PAYLOAD"
    zip_bytes = create_test_zip(init_data=init_content, bin_data=bin_content)

    parsed_init, parsed_bin = parse_dfu_zip(zip_bytes)
    assert parsed_init == init_content
    assert parsed_bin == bin_content


def test_parse_dfu_zip_without_manifest_fallback():
    """Verify fallback when manifest.json is absent but .dat and .bin files exist."""
    init_content = b"test_init_bytes"
    bin_content = b"test_bin_bytes"
    zip_bytes = create_test_zip(
        init_data=init_content,
        bin_data=bin_content,
        include_manifest=False,
    )

    parsed_init, parsed_bin = parse_dfu_zip(zip_bytes)
    assert parsed_init == init_content
    assert parsed_bin == bin_content


def test_parse_dfu_zip_missing_files_raises():
    """Verify error raised if required files are absent in archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(DfuError, match="does not contain required init and bin files"):
        parse_dfu_zip(buf.getvalue())


@pytest.mark.asyncio
async def test_nordic_dfu_client_full_sequence():
    """Verify the complete Nordic DFU handshake and data transfer sequence."""
    mock_client = MagicMock()
    mock_client.mtu_size = 247
    mock_client.write_gatt_char = AsyncMock()
    mock_client.start_notify = AsyncMock()
    mock_client.stop_notify = AsyncMock()

    progress_updates = []
    dfu = NordicSecureDfuClient(mock_client, progress_callback=progress_updates.append)

    notification_handler = None

    async def fake_start_notify(uuid, cb):
        nonlocal notification_handler
        notification_handler = cb

    mock_client.start_notify.side_effect = fake_start_notify

    init_packet = b"INITIALIZATION_PACKET"
    firmware_binary = b"FIRMWARE_CHUNK_DATA_FOR_TESTING" * 50  # 1600 bytes
    total_len = len(firmware_binary)
    max_object_size = 512
    prn_target = 0

    # Simulate bootloader responses via Control Point notifications
    async def fake_write_gatt_char(uuid, data, response=True):
        nonlocal prn_target
        if uuid == DFU_CONTROL_POINT_UUID:
            op = data[0]
            if op == OP_SELECT_OBJECT:
                obj_type = data[1]
                if obj_type == OBJ_COMMAND:
                    # max_size=512, offset=0, crc=0
                    resp = bytes([OP_RESPONSE, OP_SELECT_OBJECT, RES_SUCCESS]) + struct.pack("<III", 512, 0, 0)
                else:
                    # max_size=512, offset=0, crc=0
                    resp = bytes([OP_RESPONSE, OP_SELECT_OBJECT, RES_SUCCESS]) + struct.pack("<III", max_object_size, 0, 0)
                notification_handler(None, bytearray(resp))

            elif op == OP_CREATE:
                resp = bytes([OP_RESPONSE, OP_CREATE, RES_SUCCESS])
                notification_handler(None, bytearray(resp))

            elif op == OP_SET_PRN:
                prn_target = struct.unpack("<H", data[1:3])[0]
                resp = bytes([OP_RESPONSE, OP_SET_PRN, RES_SUCCESS])
                notification_handler(None, bytearray(resp))

            elif op == OP_CALCULATE_CHECKSUM:
                # Calculate current cumulative offset and crc based on write_packet calls
                data_packets = [
                    call[0][1] for call in mock_client.write_gatt_char.call_args_list
                    if call[0][0] == DFU_PACKET_UUID
                ]
                sent_bytes = b"".join(data_packets)
                if sent_bytes.startswith(init_packet) and len(sent_bytes) == len(init_packet):
                    # Init packet checksum
                    resp = bytes([OP_RESPONSE, OP_CALCULATE_CHECKSUM, RES_SUCCESS]) + struct.pack(
                        "<II", len(init_packet), binascii.crc32(init_packet)
                    )
                else:
                    # Firmware binary checksum (exclude init_packet bytes from count)
                    fw_sent = sent_bytes[len(init_packet):]
                    resp = bytes([OP_RESPONSE, OP_CALCULATE_CHECKSUM, RES_SUCCESS]) + struct.pack(
                        "<II", len(fw_sent), binascii.crc32(fw_sent)
                    )
                notification_handler(None, bytearray(resp))

            elif op == OP_EXECUTE:
                resp = bytes([OP_RESPONSE, OP_EXECUTE, RES_SUCCESS])
                notification_handler(None, bytearray(resp))

        elif uuid == DFU_PACKET_UUID:
            data_packets = [
                call[0][1] for call in mock_client.write_gatt_char.call_args_list
                if call[0][0] == DFU_PACKET_UUID
            ]
            sent_bytes = b"".join(data_packets)
            if len(sent_bytes) > len(init_packet):
                fw_sent = sent_bytes[len(init_packet):]
                pkt_num = (len(fw_sent) + 19) // 20
                if prn_target > 0 and pkt_num % prn_target == 0:
                    resp = bytes([OP_RESPONSE, OP_CALCULATE_CHECKSUM, RES_SUCCESS]) + struct.pack(
                        "<II", len(fw_sent), binascii.crc32(fw_sent)
                    )
                    notification_handler(None, bytearray(resp))

    mock_client.write_gatt_char.side_effect = fake_write_gatt_char

    zip_bytes = create_test_zip(init_data=init_packet, bin_data=firmware_binary)
    await dfu.update(zip_bytes)

    # Verify notifications were started and stopped
    mock_client.start_notify.assert_awaited_once_with(DFU_CONTROL_POINT_UUID, dfu._on_notification)
    mock_client.stop_notify.assert_awaited_once_with(DFU_CONTROL_POINT_UUID)

    # Verify progress was updated
    assert len(progress_updates) > 0
    assert progress_updates[-1] == 100.0


@pytest.mark.asyncio
async def test_nordic_dfu_remote_error_handling():
    """Verify that remote DFU error codes raise DfuRemoteError with description."""
    mock_client = MagicMock()
    mock_client.write_gatt_char = AsyncMock()
    mock_client.start_notify = AsyncMock()

    dfu = NordicSecureDfuClient(mock_client)
    notification_handler = None

    async def fake_start_notify(uuid, cb):
        nonlocal notification_handler
        notification_handler = cb

    mock_client.start_notify.side_effect = fake_start_notify
    await mock_client.start_notify(DFU_CONTROL_POINT_UUID, dfu._on_notification)

    # Simulate remote error during Select Object
    async def fake_write_error(uuid, data, response=True):
        if uuid == DFU_CONTROL_POINT_UUID:
            resp = bytes([OP_RESPONSE, OP_SELECT_OBJECT, RES_INSUFFICIENT_RESOURCES])
            notification_handler(None, bytearray(resp))

    mock_client.write_gatt_char.side_effect = fake_write_error

    with pytest.raises(DfuRemoteError) as exc_info:
        await dfu.select_object(OBJ_COMMAND)

    assert exc_info.value.op_code == OP_SELECT_OBJECT
    assert exc_info.value.result_code == RES_INSUFFICIENT_RESOURCES
    assert "Insufficient resources" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nordic_dfu_extended_error_handling():
    """Verify that extended DFU error codes include extended details."""
    mock_client = MagicMock()
    mock_client.write_gatt_char = AsyncMock()
    dfu = NordicSecureDfuClient(mock_client)

    notification_handler = dfu._on_notification

    async def fake_write_extended_error(uuid, data, response=True):
        if uuid == DFU_CONTROL_POINT_UUID:
            # OpCode Response, Select Object, Extended Error, 0x05 (FW version failure)
            resp = bytes([OP_RESPONSE, OP_SELECT_OBJECT, RES_EXTENDED_ERROR, 0x05])
            notification_handler(None, bytearray(resp))

    mock_client.write_gatt_char.side_effect = fake_write_extended_error

    with pytest.raises(DfuRemoteError) as exc_info:
        await dfu.select_object(OBJ_COMMAND)

    assert exc_info.value.result_code == RES_EXTENDED_ERROR
    assert exc_info.value.ext_code == 0x05
    assert "Firmware version failure" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nordic_dfu_data_object_size():
    """Verify data object sizing when reported max_object_size is large."""
    mock_client = MagicMock()
    mock_client.mtu_size = 247
    mock_client.write_gatt_char = AsyncMock()
    dfu = NordicSecureDfuClient(mock_client)

    notification_handler = dfu._on_notification

    test_data = b"A" * 8192
    created_sizes = []
    current_offset = 0

    packet_count = 0
    bytes_streamed = 0
    prn_val = 1

    async def fake_write(uuid, data, response=True):
        nonlocal current_offset, packet_count, bytes_streamed, prn_val
        if uuid == DFU_PACKET_UUID:
            packet_count += 1
            bytes_streamed += len(data)
            if prn_val > 0 and packet_count % prn_val == 0:
                resp = bytes([OP_RESPONSE, OP_CALCULATE_CHECKSUM, RES_SUCCESS]) + struct.pack(
                    "<II", bytes_streamed, binascii.crc32(b"A" * bytes_streamed)
                )
                notification_handler(None, bytearray(resp))
            return

        if uuid == DFU_CONTROL_POINT_UUID:
            op = data[0]
            if op == OP_SELECT_OBJECT:
                # Return max_size = 268435456 (0x10000000)
                resp = bytes([OP_RESPONSE, OP_SELECT_OBJECT, RES_SUCCESS]) + struct.pack("<III", 268435456, 0, 0)
                notification_handler(None, bytearray(resp))
            elif op == OP_SET_PRN:
                prn_val = struct.unpack("<H", data[1:3])[0]
                resp = bytes([OP_RESPONSE, OP_SET_PRN, RES_SUCCESS])
                notification_handler(None, bytearray(resp))
            elif op == OP_CREATE:
                size = struct.unpack("<I", data[2:6])[0]
                current_offset += size
                bytes_streamed = 0
                packet_count = 0
                resp = bytes([OP_RESPONSE, OP_CREATE, RES_SUCCESS])
                notification_handler(None, bytearray(resp))
            elif op == OP_CALCULATE_CHECKSUM:
                resp = bytes([OP_RESPONSE, OP_CALCULATE_CHECKSUM, RES_SUCCESS]) + struct.pack("<II", bytes_streamed, binascii.crc32(b"A" * bytes_streamed))
                notification_handler(None, bytearray(resp))
            elif op == OP_EXECUTE:
                resp = bytes([OP_RESPONSE, OP_EXECUTE, RES_SUCCESS])
                notification_handler(None, bytearray(resp))

    mock_client.write_gatt_char.side_effect = fake_write

    original_create = dfu.create_object
    async def track_create(obj_type, size):
        if obj_type == OBJ_DATA:
            created_sizes.append(size)
        await original_create(obj_type, size)

    dfu.create_object = track_create

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await dfu.send_firmware_data(test_data)

    # When bootloader reports 0x10000000 max_size, a single data object of total_len is created
    assert created_sizes == [8192]

