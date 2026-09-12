"""Sesame hardware simulation and firmware emulation environment."""

from .mcu import CortexM4Emulator
from .device_firmware import (
    SimulatedFirmwareDevice,
    SimulatedSesameLock,
    SimulatedSesame6Pro,
    SimulatedSesame6,
    SimulatedSesame6ProSlidingDoor,
    SimulatedSesame5,
    SimulatedSesame5Pro,
    SimulatedSesame5USA,
    SimulatedSesameBike2,
    SimulatedSesameTouch2Pro,
)
from .ble_bridge import VirtualBleakClient, VirtualBleakScanner, VirtualBLEDevice

__all__ = [
    "CortexM4Emulator",
    "SimulatedFirmwareDevice",
    "SimulatedSesameLock",
    "SimulatedSesame6Pro",
    "SimulatedSesame6",
    "SimulatedSesame6ProSlidingDoor",
    "SimulatedSesame5",
    "SimulatedSesame5Pro",
    "SimulatedSesame5USA",
    "SimulatedSesameBike2",
    "SimulatedSesameTouch2Pro",
    "VirtualBleakClient",
    "VirtualBleakScanner",
    "VirtualBLEDevice",
]

