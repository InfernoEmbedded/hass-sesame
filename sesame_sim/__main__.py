"""Main CLI entrypoint for running the Sesame Hardware Simulation Environment."""

import argparse
import asyncio
import logging
import sys
import webbrowser
from .device_firmware import SimulatedSesame6Pro, SimulatedSesameTouch2Pro
from .server import SimulationServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sesame_sim")


async def main_async(args: argparse.Namespace) -> None:
    lock = None
    keypad = None

    if args.model in ("sesame6_pro", "both"):
        logger.info("Initializing simulated Sesame 6 Pro (Lock)...")
        lock = SimulatedSesame6Pro()

    if args.model in ("sesame_touch_2_pro", "both"):
        logger.info("Initializing simulated Sesame Touch 2 Pro (Keypad)...")
        keypad = SimulatedSesameTouch2Pro()

    server = SimulationServer(lock=lock, keypad=keypad, host=args.host, port=args.port)
    await server.start()

    url = f"http://{args.host}:{args.port}"
    print("\n" + "=" * 64)
    print("  🚀 SESAME HARDWARE SIMULATOR READY")
    print(f"  👉 Web GUI: {url}")
    if lock:
        print(f"  🔐 Sesame 6 Pro:    {lock.ble_address} (Secret: {lock.secret_key.hex()})")
    if keypad:
        print(f"  🔢 Touch 2 Pro:     {keypad.ble_address} (Secret: {keypad.secret_key.hex()})")
    print("=" * 64 + "\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Run indefinitely
    while True:
        await asyncio.sleep(3600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sesame Hardware Simulation Environment")
    parser.add_argument(
        "--model",
        choices=["sesame6_pro", "sesame_touch_2_pro", "both"],
        default="both",
        help="Device models to simulate (default: both)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP/WS server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8088, help="HTTP/WS server port (default: 8088)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open web browser")

    args = parser.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
