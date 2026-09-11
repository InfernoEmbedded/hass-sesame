"""Automated tests for ARM Cortex-M4 emulator and simulated Sesame firmware."""

import os
import pytest
from sesame_sim.mcu import CortexM4Emulator
from sesame_sim.device_firmware import (
    SimulatedSesame6Pro,
    SimulatedSesameTouch2Pro,
    RX_CHAR_UUID,
)


def test_cortex_m4_emulator_basic():
    """Verify Unicorn Cortex-M4 emulator initializes memory and executes thumb instructions."""
    mcu = CortexM4Emulator()
    assert mcu.uc is not None

    # Write a simple Thumb function: adds r0, r0, r1; bx lr
    # 0x1840 = adds r0, r0, r1
    # 0x4770 = bx lr
    code_addr = 0x00405000
    mcu.mem_write(code_addr, bytes.fromhex("40187047"))

    res = mcu.call(code_addr, 17, 25)
    assert res == 42


def test_sesame6_pro_boot_and_lock_unlock():
    """Verify Sesame 6 Pro boots authentic firmware and processes motor lock/unlock."""
    lock = SimulatedSesame6Pro()
    assert lock.model_name == "sesame6_pro"
    assert lock.product_type == 21
    assert lock.firmware_version == "3.0-21-956bb2"
    assert lock.is_locked is True
    assert lock.current_angle == 0.0

    # Test manual angle rotation
    lock.set_manual_angle(90.0)
    assert lock.current_angle == 90.0
    assert lock.is_locked is False

    # Test lock command
    lock.lock()
    assert lock.target_angle == lock.locked_angle

    # Test state dictionary
    state = lock.get_state()
    assert state["model"] == "sesame6_pro"
    assert state["product_type"] == 21
    assert "angle" in state


def test_sesame_touch_2_pro_boot_and_keypad():
    """Verify Sesame Touch 2 Pro boots authentic firmware and processes keypad and buzzer events."""
    keypad = SimulatedSesameTouch2Pro()
    assert keypad.model_name == "sesame_touch_2_pro"
    assert keypad.product_type == 26
    assert keypad.firmware_version == "3.0-9-e877d5"
    assert keypad.buzzer_active is False

    # Test keypad button presses
    keypad.press_key("1")
    keypad.press_key("2")
    keypad.press_key("3")
    assert keypad.keypad_input == "123"

    # Test clear button '*'
    keypad.press_key("*")
    assert keypad.keypad_input == ""

    # Test enter button '#' with default PIN '123456'
    for char in "123456":
        keypad.press_key(char)
    assert keypad.keypad_input == "123456"
    keypad.press_key("#")
    assert keypad.keypad_input == ""
    assert keypad.led_green is True

    # Test biometrics & NFC
    keypad.scan_card("E004010203040506")
    assert keypad.led_green is True

    keypad.scan_fingerprint(matched=True)
    assert keypad.led_green is True
