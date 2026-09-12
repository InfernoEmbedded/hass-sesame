"""Sesame hardware simulation and firmware emulation environment."""

from .mcu import CortexM4Emulator
from .device_firmware import (
    SimulatedFirmwareDevice,
    SimulatedSesameLock,
    SimulatedSesame6Pro,
    SimulatedSesame5,
    SimulatedSesame5Pro,
    SimulatedSesameTouch2Pro,
)
from .ble_bridge import VirtualBleakClient, VirtualBleakScanner, VirtualBLEDevice

__all__ = [
    "CortexM4Emulator",
    "SimulatedFirmwareDevice",
    "SimulatedSesameLock",
    "SimulatedSesame6Pro",
    "SimulatedSesame5",
    "SimulatedSesame5Pro",
    "SimulatedSesameTouch2Pro",
    "VirtualBleakClient",
    "VirtualBleakScanner",
    "VirtualBLEDevice",
]

