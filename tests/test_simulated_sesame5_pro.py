"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame 5 Pro lock."""

import asyncio
import struct
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import SimulatedSesame5Pro
from sesame_sim.ble_bridge import VirtualBleakClient, VirtualBLEDevice
from custom_components.sesame_ble.sesame_client.device import (
    SesameAdData,
    SesameLock,
    ProductModels,
)


@pytest.fixture
def mock_bleak_establish(monkeypatch):
    """Mocks establish_connection to return our in-memory VirtualBleakClient."""
    async def mock_establish(client_cls, dev, *args, **kwargs):
        client = VirtualBleakClient(dev, simulated_device=dev._sim_device, **kwargs)
        await client.connect()
        return client

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", mock_establish)


async def create_connected_sesame5_pro(mock_bleak_establish, secret_key: bytes = b"\x23" * 16) -> tuple[SimulatedSesame5Pro, SesameLock]:
    sim_lock = SimulatedSesame5Pro(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_lock.ble_address,
        name="SESAME_SESAME5_PRO",
        sim_device=sim_lock,
    )
    ad_data = SesameAdData(
        model_id=ProductModels.SESAME5_PRO.value,
        is_registered=True,
        device_uuid=UUID("77777777-7777-7777-7777-777777777777"),
    )
    lock_device = SesameLock(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key=secret_key.hex(),
    )
    await lock_device.connect()
    await lock_device.login()
    return sim_lock, lock_device


@pytest.mark.asyncio
async def test_sesame5_pro_login_and_initial_telemetry(mock_bleak_establish):
    """Verify Sesame 5 Pro session establishment, initial telemetry, and model attributes."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    assert sim_lock.product_type == 7
    assert sim_lock.model_name == "sesame5_pro"
    assert lock_device.is_connected is True
    assert lock_device.is_logged_in is True
    assert lock_device.battery_voltage == 6.0
    assert lock_device.battery_percentage == 100
    assert lock_device.current_angle == 0
    assert lock_device.target_angle == 0
    assert lock_device.is_locked is True
    assert lock_device.is_unlocked is False
    assert lock_device.is_moving is False
    assert lock_device.is_battery_critical is False

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that an incorrect secret key is rejected by the Simulated Sesame 5 Pro firmware."""
    sim_lock = SimulatedSesame5Pro(secret_key=b"\x11" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_lock.ble_address,
        name="SESAME_SESAME5_PRO",
        sim_device=sim_lock,
    )
    ad_data = SesameAdData(
        model_id=7,
        is_registered=True,
        device_uuid=UUID("77777777-7777-7777-7777-777777777777"),
    )
    wrong_lock = SesameLock(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="00" * 16,
    )
    await wrong_lock.connect()

    with pytest.raises(Exception):
        await wrong_lock.login()

    await wrong_lock.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_lock_and_unlock_commands(mock_bleak_establish):
    """Verify motor unlock and lock commands drive the Sesame 5 Pro motor animation."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    # 1. Send Unlock command
    await lock_device.unlock(history_name="TestUser")
    await asyncio.sleep(0.35)

    assert sim_lock.current_angle == 90.0
    assert sim_lock.is_locked is False
    assert lock_device.current_angle == 90
    assert lock_device.is_unlocked is True
    assert lock_device.is_locked is False

    # 2. Send Lock command
    await lock_device.lock(history_name="TestUser")
    await asyncio.sleep(0.35)

    assert sim_lock.current_angle == 0.0
    assert sim_lock.is_locked is True
    assert lock_device.current_angle == 0
    assert lock_device.is_locked is True
    assert lock_device.is_unlocked is False

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_manual_thumbturn_rotation(mock_bleak_establish):
    """Verify physical thumbturn rotation by hand updates client status in real-time."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    sim_lock.set_manual_angle(90.0)
    await asyncio.sleep(0.05)

    assert lock_device.current_angle == 90
    assert lock_device.is_unlocked is True
    assert lock_device.is_locked is False

    sim_lock.set_manual_angle(0.0)
    await asyncio.sleep(0.05)

    assert lock_device.current_angle == 0
    assert lock_device.is_locked is True
    assert lock_device.is_unlocked is False

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_configure_mechanical_settings(mock_bleak_establish):
    """Verify setting custom locked and unlocked angles on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    await lock_device.configure_lock_position(lock_position=15, unlock_position=105)
    await asyncio.sleep(0.05)

    assert sim_lock.locked_angle == 15.0
    assert sim_lock.unlocked_angle == 105.0
    assert lock_device.lock_position == 15
    assert lock_device.unlock_position == 105

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_configure_autolock(mock_bleak_establish):
    """Verify auto-lock timer configuration on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    await lock_device.set_auto_lock_second(15)
    await asyncio.sleep(0.05)

    assert sim_lock.auto_lock_second == 15
    assert lock_device.auto_lock_second == 15

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_configure_opensensor_timer(mock_bleak_establish):
    """Verify OpenSensor auto-lock timer configuration on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    await lock_device.set_ops_lock_second(8)
    await asyncio.sleep(0.05)

    assert sim_lock.ops_lock_second == 8
    assert lock_device.ops_lock_second == 8

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_magnet_calibration(mock_bleak_establish):
    """Verify magnet calibration command on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    sim_lock.current_angle = 45.0
    sim_lock.is_locked = False

    await lock_device.calibrate_magnet()
    await asyncio.sleep(0.05)

    assert sim_lock.current_angle == sim_lock.locked_angle
    assert sim_lock.is_locked is True
    assert lock_device.is_locked is True

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_history_fetch_and_flush(mock_bleak_establish):
    """Verify history record generation and queue drain on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    await lock_device.unlock(history_name="UserA")
    await asyncio.sleep(0.05)
    await lock_device.lock(history_name="UserB")
    await asyncio.sleep(0.05)

    assert len(sim_lock.history_records) >= 2

    records = await lock_device.fetch_and_flush_history()
    assert len(records) >= 2
    assert len(sim_lock.history_records) == 0

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_firmware_version_query(mock_bleak_establish):
    """Verify that Sesame 5 Pro returns authentic firmware version 3.0-7-3bfc1c."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    version = await lock_device.request_firmware_version()
    assert version == "3.0-7-3bfc1c"
    assert sim_lock.firmware_version == "3.0-7-3bfc1c"

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_sync_time(mock_bleak_establish):
    """Verify time synchronization command execution on Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    await lock_device.sync_time()
    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_sesame5_pro_reconnect_lifecycle(mock_bleak_establish):
    """Verify disconnecting and reconnecting to the Simulated Sesame 5 Pro."""
    sim_lock, lock_device = await create_connected_sesame5_pro(mock_bleak_establish)

    assert lock_device.is_connected is True
    await lock_device.disconnect()
    assert lock_device.is_connected is False

    await lock_device.connect()
    await lock_device.login()
    assert lock_device.is_connected is True
    assert lock_device.is_logged_in is True

    await lock_device.disconnect()
