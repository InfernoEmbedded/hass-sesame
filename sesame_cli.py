#!/usr/bin/env python3
"""Standalone command-line interface to interact with Candy House Sesame BLE devices.

Uses the local Bluetooth hardware via bleak to scan, connect, authenticate, and
control physical Sesame locks and keypads.
"""

import argparse
import asyncio
import logging
import os
import sys
from uuid import UUID

# Set up logging to print to console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Insert the custom_components/sesame_ble directory into sys.path to resolve sesame_client imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "custom_components/sesame_ble")))

try:
    from sesame_client import (
        ProductModels,
        SesameAdData,
        SesameQRCode,
        SesameLock,
        SesameKeypad,
        is_keypad_model,
        create_sesame_device,
        scan_sesame_devices,
        find_sesame_device,
    )
except ImportError as err:
    print(f"Error: Required dependency missing. Make sure you run this in the virtual environment: {err}", file=sys.stderr)
    sys.exit(1)


async def scan_devices():
    """Scans for nearby Bluetooth devices and filters for Sesame hardware."""
    print("Scanning for Sesame devices... (scanning for 5 seconds)")

    def detection_callback(device, ad_data):
        try:
            model_name = ProductModels(ad_data.model_id).name
        except ValueError:
            model_name = f"Unknown ({ad_data.model_id})"
        print(f"Device Found:")
        print(f"  Name: {device.name}")
        print(f"  MAC Address: {device.address}")
        print(f"  Model: {model_name} (ID: {ad_data.model_id})")
        print(f"  UUID: {ad_data.device_uuid}")
        print(f"  Registered: {ad_data.is_registered}")
        print("-" * 40)

    try:
        found_devices = await scan_sesame_devices(timeout=5.0, callback=detection_callback)
    except Exception as e:
        print(f"Failed to scan: {e}", file=sys.stderr)
        return

    if not found_devices:
        print("No Sesame devices discovered in range.")


async def run_client(args):
    """Connects to a Sesame device and executes the requested action."""
    secret_key = args.secret
    qr_info = None

    if args.qr_url:
        try:
            qr_info = SesameQRCode.from_url(args.qr_url)
            secret_key = qr_info.secret_key.hex()
            print(f"Parsed QR Code URL:")
            print(f"  Device Name: {qr_info.device_name}")
            print(f"  Model ID: {qr_info.model_id} ({ProductModels(qr_info.model_id).name})")
            print(f"  UUID: {qr_info.device_uuid}")
            print(f"  Secret Key: {secret_key}")
        except Exception as e:
            print(f"Failed to parse QR Code URL: {e}", file=sys.stderr)
            return 1

    if args.simulator:
        if not args.address:
            args.address = "FD:81:AA:BB:CC:21"
        if not secret_key and not args.register:
            secret_key = "00112233445566778899aabbccddeeff"

    if not args.address:
        print("Error: --address is required to connect to a device.", file=sys.stderr)
        return 1

    if not secret_key and not args.register:
        print("Error: Either --secret, --qr-url, or --register must be provided to authenticate or register.", file=sys.stderr)
        return 1

    if args.simulator:
        from sesame_sim import (
            SimulatedSesame6Pro,
            SimulatedSesame5,
            SimulatedSesameTouch2Pro,
            VirtualBleakClient,
            VirtualBLEDevice,
        )
        import bleak

        is_keypad = "26" in (args.address or "")
        is_sesame5 = "05" in (args.address or "")
        sec_bytes = bytes.fromhex(secret_key) if secret_key else None
        if is_keypad:
            sim_device = SimulatedSesameTouch2Pro(secret_key=sec_bytes)
        elif is_sesame5:
            sim_device = SimulatedSesame5(secret_key=sec_bytes)
        else:
            sim_device = SimulatedSesame6Pro(secret_key=sec_bytes)
        secret_key = sim_device.secret_key.hex()
        model_id = sim_device.product_type
        ad_data = SesameAdData(
            model_id=model_id,
            is_registered=True,
            device_uuid=UUID("00000000-0000-0000-0000-000000000000"),
        )
        ble_device = VirtualBLEDevice(
            address=args.address or sim_device.ble_address,
            name=f"SESAME_{sim_device.model_name.upper()}",
            sim_device=sim_device,
        )

        try:
            import bleak_retry_connector

            async def virtual_establish(client_cls, dev, *a, **kw):
                c = VirtualBleakClient(dev, simulated_device=sim_device, **kw)
                await c.connect()
                return c

            bleak_retry_connector.establish_connection = virtual_establish
        except (ImportError, ModuleNotFoundError):
            pass
        bleak.BleakClient = VirtualBleakClient
    else:
        print(f"Finding Bluetooth device {args.address}...")
        result = await find_sesame_device(args.address, timeout=5.0)
        if not result:
            print(f"Error: Bluetooth device {args.address} not found.", file=sys.stderr)
            return 1

        ble_device, ad_data = result

    if not ad_data:
        # Fallback to model/UUID from parsed QR URL or defaults
        model_id = qr_info.model_id if qr_info else ProductModels.SESAME5.value
        device_uuid = qr_info.device_uuid if qr_info else UUID("00000000-0000-0000-0000-000000000000")
        ad_data = SesameAdData(model_id=model_id, is_registered=not args.register, device_uuid=device_uuid)

    try:
        model_name = ProductModels(ad_data.model_id).name
    except ValueError:
        model_name = f"Unknown ({ad_data.model_id})"
    print(f"Connecting to {model_name} at {args.address}...")

    # Status callback definition
    def status_callback(device, status):
        print("\n--- Device Status Update Received ---")
        batt_str = (
            f"{device.battery_voltage:.3f}V ({device.battery_percentage}%)"
            if getattr(device, "battery_voltage", None) is not None
            else "N/A"
        )
        if isinstance(device, SesameLock):
            print(f"  Battery Voltage: {batt_str}")
            print(f"  Lock State: {'LOCKED' if device.is_locked else 'UNLOCKED' if device.is_unlocked else 'UNKNOWN'}")
            print(f"  Angle: current={device.current_angle}°, target={device.target_angle}°")
            print(f"  Moving: {device.is_moving}")
            print(f"  Battery Critical: {device.is_battery_critical}")
        elif isinstance(device, SesameKeypad):
            print(f"  Battery Voltage: {batt_str}")
            print(f"  Passwords Count: {device.passcodes_count}")
            print(f"  Fingerprints Count: {device.fingerprints_count}")
            print(f"  Cards Count: {device.cards_count}")
            print(f"  Battery Critical: {device.is_battery_critical}")
        print("-" * 38)

    device = create_sesame_device(ble_device, ad_data, secret_key=secret_key, status_callback=status_callback)
    is_keypad = is_keypad_model(ad_data.model_id)

    try:
        await device.connect()
        if args.register:
            print("Connected! Registering device...")
            secret_key_hex = await device.register()
            print("\n" + "=" * 60)
            print("REGISTRATION SUCCESSFUL!")
            print(f"Derived Secret Key: {secret_key_hex}")
            print("=" * 60 + "\n")
            return 0

        print("Connected! Logging in...")
        device_time = await device.login()
        print(f"Login successful. Device time: {device_time}")

        # Await initial status update
        print("Awaiting initial status publish from device...")
        for _ in range(50):
            if device.mech_status is not None:
                break
            await asyncio.sleep(0.1)

        if device.mech_status is None:
            print("Warning: Device did not publish status within timeout.")

        # Execute requested commands
        if args.lock:
            if is_keypad:
                print("Error: Keypads do not support locking commands.", file=sys.stderr)
            else:
                print("Sending lock command...")
                await device.lock()
                print("Lock command sent.")

        elif args.unlock:
            if is_keypad:
                print("Error: Keypads do not support unlocking commands.", file=sys.stderr)
            else:
                print("Sending unlock command...")
                await device.unlock()
                print("Unlock command sent.")

        elif args.get_passcodes:
            if not is_keypad:
                print("Error: Lock devices do not support passcodes.", file=sys.stderr)
            else:
                print("Syncing passcode database...")
                passcodes = await device.get_passcodes()
                print("\nSynchronized Passcodes:")
                for code_id, details in passcodes.items():
                    print(f"  - PIN ID: {code_id}")
                    print(f"    Name: {details.get('name')}")
                    print(f"    Digits: {details.get('code')}")
                    print(f"    Type: {details.get('type')}")
                    print("-" * 20)

        elif args.add_passcode:
            if not is_keypad:
                print("Error: Lock devices do not support passcodes.", file=sys.stderr)
            else:
                code, name = args.add_passcode
                print(f"Adding passcode '{code}' as '{name}'...")
                await device.add_passcode(code, name)
                print("Passcode addition command sent.")

        elif args.delete_passcode:
            if not is_keypad:
                print("Error: Lock devices do not support passcodes.", file=sys.stderr)
            else:
                print(f"Deleting passcode '{args.delete_passcode}'...")
                # Retrieve passcodes first so self.passcodes is populated for _resolve_code
                await device.get_passcodes()
                await device.delete_passcode(args.delete_passcode)
                print("Passcode deletion command sent.")

        # Allow commands to finish processing
        if args.lock or args.unlock or args.get_passcodes or args.add_passcode or args.delete_passcode:
            await asyncio.sleep(2.0)

    except Exception as e:
        import traceback
        print("Error during execution:", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        print("Disconnecting...")
        await device.disconnect()
        print("Disconnected successfully.")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Standalone CLI to scan and control Sesame BLE devices.")
    parser.add_argument("--scan", action="store_true", help="Scan for nearby Sesame BLE devices")
    parser.add_argument("--simulator", action="store_true", help="Connect to the simulated hardware environment instead of physical BLE hardware")
    parser.add_argument("--address", help="Bluetooth MAC Address of the Sesame device")
    parser.add_argument("--secret", help="Hex secret key (32 characters)")
    parser.add_argument("--qr-url", help="ssm:// URL to extract model, UUID, and secret key")
    parser.add_argument("--register", action="store_true", help="Register a factory-reset device and get the secret key")
    parser.add_argument("--status", action="store_true", help="Retrieve device status")
    parser.add_argument("--lock", action="store_true", help="Lock the Sesame device")
    parser.add_argument("--unlock", action="store_true", help="Unlock the Sesame device")
    parser.add_argument("--get-passcodes", action="store_true", help="Sync and retrieve Keypad passcodes")
    parser.add_argument("--add-passcode", nargs=2, metavar=("CODE", "NAME"), help="Add a new passcode to the Keypad")
    parser.add_argument("--delete-passcode", metavar="CODE_OR_ID", help="Delete a Keypad passcode by its PIN or Hex ID")

    args = parser.parse_args()

    if not any([args.scan, args.address, args.qr_url, args.register]):
        parser.print_help()
        sys.exit(1)

    if args.scan:
        asyncio.run(scan_devices())
    else:
        sys.exit(asyncio.run(run_client(args)))


if __name__ == "__main__":
    main()
