import asyncio
import logging
import sys
import os

# Set up logging to stdout
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("debug_script")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "custom_components/sesame_ble")))

from bleak import BleakClient, BleakScanner
from sesame_client import SesameQRCode, ProductModels
from sesame_client.device import SesameAdData, SesameCipher, derive_session_token_key, SesameSegmentLayer

MAC_ADDRESS = "DE:34:B7:06:2E:56"
QR_URL = "ssm://UI?t=sk&sk=Gok0%2BolQTqG3CZO5H%2FRAeqkAAAAAAABBUDUwMDA2AgDxflFWBgF4&l=0&n=Sesame%20Touch%202%20Pro"

RX_CHAR_UUID = "16860003-a5ae-9856-b6d3-dbb4c676993e"
TX_CHAR_UUID = "16860002-a5ae-9856-b6d3-dbb4c676993e"

async def main():
    qr_info = SesameQRCode.from_url(QR_URL)
    secret_key = qr_info.secret_key
    logger.info("Parsed QR secret_key: %s", secret_key.hex())

    logger.info("Finding Bluetooth device...")
    device = await BleakScanner.find_device_by_address(MAC_ADDRESS, timeout=5.0)
    if not device:
        logger.error("Device not found")
        return

    logger.info("Connecting to %s...", MAC_ADDRESS)
    async with BleakClient(device) as client:
        logger.info("Connected!")
        
        token_fut = asyncio.get_running_loop().create_future()
        response_fut = asyncio.get_running_loop().create_future()
        segmenter = SesameSegmentLayer()
        cipher = None

        def notification_handler(char, data):
            nonlocal cipher
            logger.info("RAW NOTIFICATION RECEIVED: %s", data.hex())
            res = segmenter.feed_packet(bytes(data))
            if not res:
                return
            payload, is_complete, is_encrypted = res
            logger.info("Feed packet result: payload=%s, is_encrypted=%s", payload.hex(), is_encrypted)
            
            if is_encrypted:
                if cipher is None:
                    logger.warning("Received encrypted message before cipher is initialized!")
                    return
                try:
                    decrypted = cipher.decrypt_payload(payload)
                    logger.info("DECRYPTED PAYLOAD: %s", decrypted.hex())
                    if not response_fut.done():
                        response_fut.set_result((payload, decrypted))
                except Exception as e:
                    logger.error("Decryption failed: %s", e)
                    if not response_fut.done():
                        response_fut.set_exception(e)
            else:
                op_code = payload[0]
                if op_code == 0x08: # OP_PUBLISH
                    item_code = payload[1]
                    if item_code == 14: # ITEM_INITIAL
                        token = payload[2:]
                        logger.info("Received session token: %s", token.hex())
                        token_fut.set_result(token)

        await client.start_notify(RX_CHAR_UUID, notification_handler)
        
        logger.info("Waiting for token...")
        token = await asyncio.wait_for(token_fut, timeout=5.0)
        
        session_key = derive_session_token_key(secret_key, token)
        logger.info("Derived Session Key: %s", session_key.hex())
        
        cipher = SesameCipher(token, session_key)
        
        # Send login command: [ITEM_LOGIN] [session_key[:4]]
        # which is 0x02, followed by session_key[:4]
        login_payload = bytes([0x02]) + session_key[:4]
        logger.info("Sending login command: %s", login_payload.hex())
        
        # Segment command (unencrypted)
        packets = segmenter.segment_payload(login_payload, encrypt=False)
        for packet in packets:
            logger.info("Writing packet: %s", packet.hex())
            await client.write_gatt_char(TX_CHAR_UUID, packet, response=False)
            
        logger.info("Waiting for login response...")
        try:
            raw_res, decrypted_res = await asyncio.wait_for(response_fut, timeout=5.0)
            logger.info("Login successful!")
        except Exception as e:
            logger.error("Login failed during wait: %s", e)

if __name__ == "__main__":
    asyncio.run(main())
