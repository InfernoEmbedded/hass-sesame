import asyncio
import logging
import os
from typing import Any, Callable
from bleak.backends.characteristic import BleakGATTCharacteristic
from .device_firmware import SimulatedFirmwareDevice, TX_CHAR_UUID, RX_CHAR_UUID, SERVICE_UUID

logger = logging.getLogger(__name__)


from bleak import BleakClient


class VirtualBLEDevice:
    """Mock bleak.BLEDevice representing a simulated Sesame hardware unit."""

    def __init__(self, address: str, name: str, rssi: int = -50, sim_device: Any = None) -> None:
        self.address = address
        self.name = name
        self.rssi = rssi
        self.details = {}
        self._sim_device = sim_device

    def lower(self) -> str:
        return self.address.lower()

    def __str__(self) -> str:
        return self.address


class VirtualBleakClient(BleakClient):
    """Drop-in replacement for BleakClient that routes GATT operations to SimulatedFirmwareDevice."""

    def __init__(
        self,
        ble_device: VirtualBLEDevice | str,
        disconnected_callback: Callable[["VirtualBleakClient"], None] | None = None,
        simulated_device: SimulatedFirmwareDevice | None = None,
        **kwargs: Any,
    ) -> None:
        self._address = ble_device.address if hasattr(ble_device, "address") else str(ble_device)
        self._device = simulated_device or getattr(ble_device, "_sim_device", None)
        self._disconnected_callback = disconnected_callback
        self._notify_callbacks: dict[str, Callable[[BleakGATTCharacteristic, bytearray], None]] = {}
        self._is_connected = False

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._device.is_connected if self._device else self._is_connected

    async def connect(self, **kwargs: Any) -> bool:
        logger.info("[VirtualBleakClient] Connecting to %s (%s)", self._device.model_name, self.address)
        self._device.notification_callback = self._on_device_notification
        self._device.connect()
        return True

    async def disconnect(self) -> bool:
        logger.info("[VirtualBleakClient] Disconnecting from %s", self.address)
        self._device.disconnect()
        self._device.notification_callback = None
        if self._disconnected_callback:
            self._disconnected_callback(self)
        return True

    async def start_notify(
        self,
        char_specifier: str | BleakGATTCharacteristic,
        callback: Callable[[BleakGATTCharacteristic, bytearray], None],
        **kwargs: Any,
    ) -> None:
        uuid_str = str(char_specifier).lower()
        self._notify_callbacks[uuid_str] = callback
        logger.debug("[VirtualBleakClient] Registered notification listener on %s", uuid_str)
        if self._device and (uuid_str == RX_CHAR_UUID.lower() or "16860003" in uuid_str):
            self._device.publish_initial_token()

    async def stop_notify(self, char_specifier: str | BleakGATTCharacteristic) -> None:
        uuid_str = str(char_specifier).lower()
        self._notify_callbacks.pop(uuid_str, None)

    async def write_gatt_char(
        self,
        char_specifier: str | BleakGATTCharacteristic,
        data: bytes | bytearray,
        response: bool = False,
    ) -> None:
        uuid_str = str(char_specifier).lower()
        logger.debug("[VirtualBleakClient] Write to %s: %s", uuid_str, bytes(data).hex())
        self._device.process_gatt_write(uuid_str, bytes(data))

    def _on_device_notification(self, char_uuid: str, packet: bytes) -> None:
        uuid_str = char_uuid.lower()
        cb = self._notify_callbacks.get(uuid_str)
        if cb:
            # Create a lightweight fake characteristic object for Bleak compatibility
            char_obj = type("FakeGATTChar", (), {"uuid": uuid_str})()
            try:
                cb(char_obj, bytearray(packet))
            except Exception as err:
                logger.error("Error in GATT notification callback: %s", err)


class VirtualBleakScanner:
    """Provides simulated BLE advertisements for discovered Sesame devices."""

    def __init__(self, devices: list[SimulatedFirmwareDevice]) -> None:
        self._devices = {d.ble_address: d for d in devices}

    def get_discovered_devices(self) -> list[VirtualBLEDevice]:
        return [
            VirtualBLEDevice(address=d.ble_address, name=f"SESAME_{d.model_name.upper()}")
            for d in self._devices.values()
        ]

    def get_manufacturer_data(self, address: str) -> dict[int, bytes] | None:
        device = self._devices.get(address)
        if not device:
            return None

        # Build CandyHouse manufacturer advertisement payload (company ID 0x055A)
        # Payload format: [model_id: 1B] + [status_flags: 1B] + [battery: 2B] + [random/angle: 2B] + ...
        model_id = device.product_type
        status = 0x01
        mfg_payload = bytes([model_id, status, 0x00, 0x17, 0x70, 0x00, 0x00]) + os.urandom(12)
        return {0x055A: mfg_payload}
