"""High-Level Emulation (HLE) stubs for OnMicro HS6621 / HS6626 Silicon Mask ROM."""

import logging
import os
import random
import time
from typing import TYPE_CHECKING
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3

if TYPE_CHECKING:
    from .mcu import CortexM4Emulator

logger = logging.getLogger(__name__)


class OnMicroRomHLE:
    """Provides high-level emulation for OnMicro BLE stack and ROM runtime routines."""

    def __init__(self, mcu: "CortexM4Emulator", nvram_path: str | None = None) -> None:
        self.mcu = mcu
        self.nvram_path = nvram_path
        self._nvram: bytearray = bytearray(0x20000)  # 128 KB simulated NVRAM/Flash
        self._load_nvram()
        self._install_hooks()

    def _load_nvram(self) -> None:
        if self.nvram_path and os.path.exists(self.nvram_path):
            try:
                with open(self.nvram_path, "rb") as f:
                    data = f.read()
                self._nvram[: len(data)] = data
                logger.info("Loaded %d bytes NVRAM from %s", len(data), self.nvram_path)
            except Exception as err:
                logger.warning("Failed to load NVRAM from %s: %s", self.nvram_path, err)

    def save_nvram(self) -> None:
        if self.nvram_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.nvram_path)), exist_ok=True)
                with open(self.nvram_path, "wb") as f:
                    f.write(self._nvram)
                logger.debug("Saved NVRAM to %s", self.nvram_path)
            except Exception as err:
                logger.warning("Failed to save NVRAM to %s: %s", self.nvram_path, err)

    def _install_hooks(self) -> None:
        """Registers all ROM hook handlers."""
        # Hardware TRNG (True Random Number Generator)
        self.mcu.register_rom_hook(0x00106D3C, self.hle_trng)
        self.mcu.register_rom_hook(0x00106D12, self.hle_trng)

        # Hardware Flash NVRAM Programming / Erase
        self.mcu.register_rom_hook(0x00106F14, self.hle_flash_op)

        # Clock & Power Management / Baseband init
        self.mcu.register_rom_hook(0x00108830, self.hle_return_zero)
        self.mcu.register_rom_hook(0x00108562, self.hle_return_zero)
        self.mcu.register_rom_hook(0x00108720, self.hle_return_zero)
        self.mcu.register_rom_hook(0x001086CC, self.hle_return_zero)

        # OS Timers and System Scheduler
        self.mcu.register_rom_hook(0x0010A522, self.hle_return_zero)
        self.mcu.register_rom_hook(0x0010452A, self.hle_return_zero)
        self.mcu.register_rom_hook(0x0010481C, self.hle_return_zero)

    def hle_trng(self, mcu: "CortexM4Emulator", addr: int) -> int:
        """Emulates hardware random number generation."""
        val = random.randint(0, 0xFFFFFFFF)
        logger.debug("ROM TRNG hook at 0x%08x -> 0x%08x", addr, val)
        return val

    def hle_flash_op(self, mcu: "CortexM4Emulator", addr: int) -> int:
        """Emulates flash sector erase / write routines (0x106f14)."""
        r0 = mcu.reg_read(UC_ARM_REG_R0)
        r1 = mcu.reg_read(UC_ARM_REG_R1)
        r2 = mcu.reg_read(UC_ARM_REG_R2)
        r3 = mcu.reg_read(UC_ARM_REG_R3)
        logger.debug("ROM Flash Op: base=0x%08x, op=%d, offset=0x%08x, len=0x%08x", r0, r1, r2, r3)
        self.save_nvram()
        return 0

    def hle_return_zero(self, mcu: "CortexM4Emulator", addr: int) -> int:
        """Generic successful return for baseband and clock setup functions."""
        return 0
