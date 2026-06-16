import asyncio
import logging
import sys
import os
import time
import struct
from uuid import UUID

# Set up logging to stdout
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("register_script")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "custom_components/sesame_ble")))

from bleak import BleakClient, BleakScanner
from sesame_client import SesameQRCode, ProductModels
from sesame_client.device import SesameAdData, SesameCipher, SesameSegmentLayer
from sesame_client.crypto import generate_ecc_keypair, derive_device_secret

MAC_ADDRESS = "DE:34:B7:06:2E:56"
RX_CHAR_UUID = "16860003-a5ae-9856-b6d3-dbb4c676993e"
TX_CHAR_UUID = "16860002-a5ae-9856-b6d3-dbb4c676993e"

async def main():
    logger.info("Finding Bluetooth device...")
    device = await BleakScanner.find_device_by_address(MAC_ADDRESS, timeout=5.0)
    if not device:
        logger.error("Device not found")
        return

    # Check advertisement data
    ad_data = device.details.get("props", {})
    m_data = ad_data.get("ManufacturerData", {})
    if 1370 in m_data:
        raw_m = m_data[1370]
        # Decode: <HB16s
        model_val, registered_val, uuid_bytes = struct.unpack("<HB16s", raw_m)
        device_uuid = UUID(bytes=uuid_bytes)
        logger.info("Ad Data: Model=%d, Registered=%s, UUID=%s", model_val, bool(registered_val), device_uuid)
    else:
        logger.warning("No ManufacturerData from Candy House (1370)")
        device_uuid = None

    logger.info("Connecting to %s...", MAC_ADDRESS)
    async with BleakClient(device) as client:
        logger.info("Connected!")
        
        token_fut = asyncio.get_running_loop().create_future()
        response_fut = asyncio.get_running_loop().create_future()
        segmenter = SesameSegmentLayer()

        def notification_handler(char, data):
            logger.info("RAW NOTIFICATION RECEIVED: %s", data.hex())
            res = segmenter.feed_packet(bytes(data))
            if not res:
                return
            payload, is_complete, is_encrypted = res
            logger.info("Feed packet result: payload=%s, is_encrypted=%s", payload.hex(), is_encrypted)
            
            if is_encrypted:
                logger.info("ENCRYPTED PAYLOAD RECEIVED: %s", payload.hex())
            else:
                op_code = payload[0]
                if op_code == 0x08: # OP_PUBLISH
                    item_code = payload[1]
                    if item_code == 14: # ITEM_INITIAL
                        token = payload[2:]
                        logger.info("Received session token: %s", token.hex())
                        if not token_fut.done():
                            token_fut.set_result(token)
                elif op_code == 0x07: # OP_RESPONSE
                    item_code = payload[1]
                    res_code = payload[2]
                    logger.info("Received response: item=%d, res=%d, payload=%s", item_code, res_code, payload[3:].hex())
                    if not response_fut.done():
                        response_fut.set_result(payload)

        await client.start_notify(RX_CHAR_UUID, notification_handler)
        
        logger.info("Waiting for token...")
        token = await asyncio.wait_for(token_fut, timeout=5.0)
        
        # Generate ECC keys for registration
        app_pubkey, app_privkey = generate_ecc_keypair()
        logger.info("Generated App Public Key: %s", app_pubkey.hex())
        
        # Registration payload: [ITEM_REGISTRATION] + [app_pubkey (64 bytes)] + [timestamp (4 bytes)]
        # ITEM_REGISTRATION is 0x01
        timestamp = int(time.time()).to_bytes(4, byteorder="little")
        reg_payload = bytes([0x01]) + app_pubkey + timestamp
        
        logger.info("Sending registration command (len %d)...", len(reg_payload))
        packets = segmenter.segment_payload(reg_payload, encrypt=False)
        for packet in packets:
            logger.info("Writing packet: %s", packet.hex())
            await client.write_gatt_char(TX_CHAR_UUID, packet, response=False)
            
        logger.info("Waiting for registration response...")
        try:
            res_payload = await asyncio.wait_for(response_fut, timeout=10.0)
            res_code = res_payload[2]
            if res_code != 0:
                logger.error("Registration failed with result code: %d", res_code)
                return
            
            # Response payload starts from index 3
            payload_data = res_payload[3:]
            logger.info("Registration payload data (len %d): %s", len(payload_data), payload_data.hex())
            
            # Extract publicKeyS (64 bytes)
            if len(payload_data) >= 64:
                # If there are extra bytes (like mechSetting/mechStatus), they would follow publicKeyS or precede it.
                # For Sesame 5, they precede it: payload_data starts at index 0, publicKeyS is at index 13.
                # Let's see how long the payload is.
                if len(payload_data) == 64:
                    # Only public key
                    pubkey_s = payload_data[:64]
                elif len(payload_data) == 77:
                    # Standard lock payload: mechStatus (7), mechSetting (6), publicKeyS (64)
                    # Wait, let's verify if the offsets are 13 to 77
                    pubkey_s = payload_data[13:77]
                else:
                    # Fallback/guess: if length is different, try both options or inspect.
                    # Since public key is 64 bytes, let's check if it's the last 64 bytes.
                    logger.warning("Unexpected payload length: %d", len(payload_data))
                    pubkey_s = payload_data[-64:]
                
                logger.info("Extracted Device Public Key: %s", pubkey_s.hex())
                
                secret_key = derive_device_secret(pubkey_s, app_privkey)
                logger.info("SUCCESSFULLY DERIVED SECRET KEY: %s", secret_key.hex())
                
                if device_uuid:
                    qr = SesameQRCode(
                        device_name="Sesame Touch 2 Pro",
                        key_level=0,
                        model_id=26,
                        device_uuid=device_uuid,
                        secret_key=secret_key
                    )
                    logger.info("New QR URL: %s", qr.to_url())
            else:
                logger.error("Payload too short to extract public key")
                
        except Exception as e:
            logger.error("Registration failed: %s", e)

if __name__ == "__main__":
    asyncio.run(main())
