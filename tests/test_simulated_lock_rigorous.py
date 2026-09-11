"""Rigorous test suite exercising all aspects and protocol functions of the simulated Sesame 6 Pro lock."""

import asyncio
import struct
import pytest
from uuid import UUID

import bleak_retry_connector
from sesame_sim.device_firmware import SimulatedSesame6Pro
from sesame_sim.ble_bridge import VirtualBleakClient, VirtualBLEDevice
from custom_components.sesame_ble.sesame_client.device import (
    SesameAdData,
    SesameLock,
)


@pytest.fixture
def mock_bleak_establish(monkeypatch):
    """Mocks establish_connection to return our in-memory VirtualBleakClient."""
    async def mock_establish(client_cls, dev, *args, **kwargs):
        client = VirtualBleakClient(dev, simulated_device=dev._sim_device, **kwargs)
        await client.connect()
        return client

    monkeypatch.setattr(bleak_retry_connector, "establish_connection", mock_establish)


async def create_connected_lock(mock_bleak_establish, secret_key: bytes = b"\x11" * 16) -> tuple[SimulatedSesame6Pro, SesameLock]:
    sim_lock = SimulatedSesame6Pro(secret_key=secret_key)
    ble_device = VirtualBLEDevice(
        address=sim_lock.ble_address,
        name="SESAME_SESAME6_PRO",
        sim_device=sim_lock,
    )
    ad_data = SesameAdData(
        model_id=sim_lock.product_type,
        is_registered=True,
        device_uuid=UUID("11111111-2222-3333-4444-555555555555"),
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
async def test_lock_login_and_initial_telemetry(mock_bleak_establish):
    """Verify session establishment, time synchronization, and initial telemetry parsing."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

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
    assert lock_device.is_logged_in is False


@pytest.mark.asyncio
async def test_lock_invalid_secret_key_fails(mock_bleak_establish):
    """Verify that connecting with a wrong secret key fails during login."""
    sim_lock = SimulatedSesame6Pro(secret_key=b"\x11" * 16)
    ble_device = VirtualBLEDevice(
        address=sim_lock.ble_address,
        name="SESAME_SESAME6_PRO",
        sim_device=sim_lock,
    )
    ad_data = SesameAdData(
        model_id=sim_lock.product_type,
        is_registered=True,
        device_uuid=UUID("11111111-2222-3333-4444-555555555555"),
    )
    # Give client a completely wrong secret key
    lock_device = SesameLock(
        ble_device=ble_device,
        ad_data=ad_data,
        secret_key="22" * 16,
    )

    await lock_device.connect()
    # Login will fail because session key derives differently, failing verification
    with pytest.raises(Exception):
        await lock_device.login()


@pytest.mark.asyncio
async def test_lock_and_unlock_commands_with_tags(mock_bleak_establish):
    """Verify lock and unlock commands drive simulated motor and notify client of state changes."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    # 1. Send unlock command
    await lock_device.unlock(history_name="Alice Phone")
    # Await simulated motor animation
    await asyncio.sleep(0.35)

    assert lock_device.current_angle == 90
    assert lock_device.is_unlocked is True
    assert lock_device.is_locked is False
    assert sim_lock.is_locked is False

    # 2. Send lock command
    await lock_device.lock(history_name="Home Assistant Auto")
    await asyncio.sleep(0.35)

    assert lock_device.current_angle == 0
    assert lock_device.is_locked is True
    assert lock_device.is_unlocked is False
    assert sim_lock.is_locked is True

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_manual_thumbturn_rotation(mock_bleak_establish):
    """Verify physical thumbturn rotation by hand updates client status in real-time."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    callback_events = []
    lock_device._status_cb = lambda dev, status: callback_events.append((dev.current_angle, dev.is_locked))

    # Manually turn thumbturn to 90 degrees
    sim_lock.set_manual_angle(90.0)
    await asyncio.sleep(0.05)

    assert lock_device.current_angle == 90
    assert lock_device.is_locked is False
    assert len(callback_events) >= 1
    assert callback_events[-1] == (90, False)

    # Manually turn thumbturn back to 0 degrees
    sim_lock.set_manual_angle(0.0)
    await asyncio.sleep(0.05)

    assert lock_device.current_angle == 0
    assert lock_device.is_locked is True
    assert callback_events[-1] == (0, True)

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_configure_mechanical_settings(mock_bleak_establish):
    """Verify configure_lock_position updates angle thresholds on device."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    await lock_device.configure_lock_position(lock_position=15, unlock_position=105)
    await asyncio.sleep(0.05)

    assert sim_lock.locked_angle == 15.0
    assert sim_lock.unlocked_angle == 105.0
    assert lock_device.lock_position == 15
    assert lock_device.unlock_position == 105

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_configure_autolock(mock_bleak_establish):
    """Verify configuring auto-lock timer."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    await lock_device.set_auto_lock_second(45)
    await asyncio.sleep(0.05)

    assert sim_lock.auto_lock_second == 45

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_configure_opensensor_timer(mock_bleak_establish):
    """Verify configuring OpenSensor auto-lock timer."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    await lock_device.set_ops_lock_second(20)
    await asyncio.sleep(0.05)

    assert sim_lock.ops_lock_second == 20
    assert lock_device.ops_lock_second == 20

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_magnet_calibration(mock_bleak_establish):
    """Verify magnet calibration command is processed cleanly."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    # Rotate off-axis first
    sim_lock.set_manual_angle(45.0)
    assert lock_device.current_angle == 45

    # Calibrate magnet: device re-aligns to locked position
    await lock_device.calibrate_magnet()
    await asyncio.sleep(0.05)

    assert lock_device.current_angle == 0
    assert lock_device.is_locked is True

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_history_fetch_and_flush(mock_bleak_establish):
    """Verify lock records lock/unlock history and fetch_and_flush_history drains the log."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    # Trigger actions with history tags
    await lock_device.unlock(history_name="Alice")
    await asyncio.sleep(0.05)
    await lock_device.lock(history_name="Bob")
    await asyncio.sleep(0.05)
    sim_lock.set_manual_angle(90.0)
    await asyncio.sleep(0.05)

    assert len(sim_lock.history_records) >= 3

    # Fetch and flush history
    history = await lock_device.fetch_and_flush_history()

    assert len(history) >= 3
    # The history records in the simulated lock must now be flushed
    assert len(sim_lock.history_records) == 0

    # Verify history entry fields
    raw_names = [bytes.fromhex(h["raw_parameter"]).decode("utf-8", "replace") for h in history if h.get("raw_parameter")]
    assert any("Alice" in name for name in raw_names)
    assert any("Bob" in name for name in raw_names)
    assert any("Manual Turn" in name for name in raw_names)

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_firmware_version_query(mock_bleak_establish):
    """Verify requesting authentic firmware version tag over BLE."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    ver = await lock_device.request_firmware_version()
    assert ver == "3.0-21-956bb2"
    assert lock_device.firmware_version == "3.0-21-956bb2"

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_sync_time(mock_bleak_establish):
    """Verify time synchronization command."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    await lock_device.sync_time()
    assert lock_device.is_logged_in is True

    await lock_device.disconnect()


@pytest.mark.asyncio
async def test_lock_reconnect_lifecycle(mock_bleak_establish):
    """Verify disconnecting and reconnecting cleanly recreates session."""
    sim_lock, lock_device = await create_connected_lock(mock_bleak_establish)

    assert lock_device.is_logged_in is True
    await lock_device.disconnect()
    assert lock_device.is_logged_in is False

    # Reconnect and login again
    await lock_device.connect()
    assert lock_device.is_connected is True
    await lock_device.login()
    assert lock_device.is_logged_in is True

    await lock_device.unlock()
    await asyncio.sleep(0.35)
    assert lock_device.is_unlocked is True

    await lock_device.disconnect()
