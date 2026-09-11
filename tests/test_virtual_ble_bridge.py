"""Automated tests for VirtualBleakClient and BLE communication with simulated firmware."""

import asyncio
import pytest
from uuid import UUID
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameTouch2Pro,
)
from sesame_sim.ble_bridge import (
    VirtualBleakClient,
    VirtualBleakScanner,
    VirtualBLEDevice,
)
from custom_components.sesame_ble.sesame_client.device import (
    SesameAdData,
    SesameLock,
    SesameKeypad,
)


@pytest.mark.asyncio
async def test_virtual_bleak_client_sesame6_pro(monkeypatch):
    """Verify SesameLock connects, logs in, and executes unlock via VirtualBleakClient."""
    import bleak_retry_connector

    async def mock_establish(client_cls, dev, *a, **kw):
        client = VirtualBleakClient(dev, simulated_device=dev._sim_device, **kw)
        await client.connect()
        return client

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", mock_establish)

    sim_lock = SimulatedSesame6Pro(secret_key=b"\x01" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_lock.ble_address,
        name="SESAME_SESAME6_PRO",
        sim_device=sim_lock,
    )
    ad_data = SesameAdData(
        model_id=sim_lock.product_type,
        is_registered=True,
        device_uuid=UUID("00000000-0000-0000-0000-000000000000"),
    )

    lock_device = SesameLock(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=sim_lock.secret_key.hex(),
    )

    # Test connect and login
    await lock_device.connect()
    assert lock_device.is_connected is True

    device_time = await lock_device.login()
    assert lock_device.is_logged_in is True
    assert device_time > 0

    # Test unlock
    await lock_device.unlock()
    assert sim_lock.motor_running is True or sim_lock.current_angle == sim_lock.unlocked_angle

    await lock_device.disconnect()
    assert lock_device.is_connected is False


@pytest.mark.asyncio
async def test_virtual_bleak_client_touch_2_pro(monkeypatch):
    """Verify SesameKeypad connects, logs in, and syncs status via VirtualBleakClient."""
    import bleak_retry_connector

    async def mock_establish(client_cls, dev, *a, **kw):
        client = VirtualBleakClient(dev, simulated_device=dev._sim_device, **kw)
        await client.connect()
        return client

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", mock_establish)

    sim_keypad = SimulatedSesameTouch2Pro(secret_key=b"\x02" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_keypad.ble_address,
        name="SESAME_SESAME_TOUCH_2_PRO",
        sim_device=sim_keypad,
    )
    ad_data = SesameAdData(
        model_id=sim_keypad.product_type,
        is_registered=True,
        device_uuid=UUID("00000000-0000-0000-0000-000000000000"),
    )

    keypad_device = SesameKeypad(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=sim_keypad.secret_key.hex(),
    )

    # Test connect and login
    await keypad_device.connect()
    assert keypad_device.is_connected is True

    device_time = await keypad_device.login()
    assert keypad_device.is_logged_in is True
    assert device_time > 0

    await keypad_device.disconnect()
    assert keypad_device.is_connected is False


def test_virtual_bleak_scanner():
    """Verify scanner generates valid advertising and manufacturer data."""
    sim_lock = SimulatedSesame6Pro()
    sim_keypad = SimulatedSesameTouch2Pro()
    scanner = VirtualBleakScanner([sim_lock, sim_keypad])

    devices = scanner.get_discovered_devices()
    assert len(devices) == 2

    mfg = scanner.get_manufacturer_data(sim_lock.ble_address)
    assert 0x055A in mfg
    assert mfg[0x055A][0] == sim_lock.product_type
