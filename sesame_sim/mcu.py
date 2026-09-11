"""ARM Cortex-M4 CPU and memory emulator wrapper using Unicorn."""

import logging
import struct
from typing import Callable
import unicorn
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB
from unicorn.arm_const import (
    UC_ARM_REG_PC,
    UC_ARM_REG_SP,
    UC_ARM_REG_LR,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
)

logger = logging.getLogger(__name__)

ROM_BASE = 0x00100000
ROM_SIZE = 0x00040000  # 256 KB

FLASH_BASE = 0x00400000
FLASH_SIZE = 0x00080000  # 512 KB

SRAM_BASE = 0x20000000
SRAM_SIZE = 0x00080000  # 512 KB

STOP_ADDR = 0x30000000
STOP_SIZE = 0x00010000  # 64 KB


class CortexM4Emulator:
    """Manages an ARM Cortex-M4 Thumb-2 execution environment for Sesame firmware."""

    def __init__(self) -> None:
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self._mmio_read_hooks: dict[int, Callable[[int, int], int]] = {}
        self._mmio_write_hooks: dict[int, Callable[[int, int, int], None]] = {}
        self._rom_hooks: dict[int, Callable[["CortexM4Emulator", int], int | None]] = {}
        self._init_memory()

    def _init_memory(self) -> None:
        """Map initial memory ranges: ROM, Flash, SRAM, and Stop Trampoline."""
        # 1. ROM: fill with Thumb 'bx lr' (0x4770) so unhooked ROM calls return safely
        self.uc.mem_map(ROM_BASE, ROM_SIZE, unicorn.UC_PROT_ALL)
        bx_lr = struct.pack("<H", 0x4770) * (ROM_SIZE // 2)
        self.uc.mem_write(ROM_BASE, bx_lr)

        # 2. Flash
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE, unicorn.UC_PROT_ALL)

        # 3. SRAM
        self.uc.mem_map(SRAM_BASE, SRAM_SIZE, unicorn.UC_PROT_ALL)

        # 4. Stop address trampoline
        self.uc.mem_map(STOP_ADDR, STOP_SIZE, unicorn.UC_PROT_ALL)
        stop_bx_lr = struct.pack("<H", 0x4770) * (STOP_SIZE // 2)
        self.uc.mem_write(STOP_ADDR, stop_bx_lr)

        # 5. Core MMIO blocks
        for page in (0x40000000, 0x40010000, 0x40020000, 0x400E0000, 0x50000000, 0x51000000, 0xE0000000):
            try:
                self.uc.mem_map(page, 0x10000, unicorn.UC_PROT_ALL)
            except unicorn.UcError:
                pass

        # Hook unmapped memory to prevent hard-crashes on vendor peripheral pages
        self.uc.hook_add(unicorn.UC_HOOK_MEM_UNMAPPED, self._hook_unmapped)

        # Hook ROM executions to run Python HLE callbacks
        self.uc.hook_add(
            unicorn.UC_HOOK_CODE,
            self._hook_code_execution,
            begin=ROM_BASE,
            end=ROM_BASE + ROM_SIZE,
        )

        # Hook MMIO memory accesses
        self.uc.hook_add(unicorn.UC_HOOK_MEM_READ, self._hook_mem_read)
        self.uc.hook_add(unicorn.UC_HOOK_MEM_WRITE, self._hook_mem_write)

    def _hook_unmapped(self, uc: Uc, access: int, address: int, size: int, value: int, user_data: None) -> bool:
        """Dynamically maps any 64KB page accessed by firmware that was not pre-mapped."""
        page = address & ~0xFFFF
        try:
            uc.mem_map(page, 0x10000, unicorn.UC_PROT_ALL)
            logger.debug("Auto-mapped memory page 0x%08x on access %d to 0x%08x", page, access, address)
            return True
        except Exception as err:
            logger.warning("Failed to auto-map 0x%08x: %s", address, err)
            return False

    def _hook_code_execution(self, uc: Uc, address: int, size: int, user_data: None) -> None:
        """Intercepts execution inside the Mask ROM to execute HLE handlers."""
        # Check if address has a registered hook (masking thumb bit)
        norm_addr = address & ~1
        hook = self._rom_hooks.get(norm_addr)
        if hook:
            res = hook(self, norm_addr)
            if res is not None:
                self.reg_write(UC_ARM_REG_R0, res)

    def _hook_mem_read(self, uc: Uc, access: int, address: int, size: int, value: int, user_data: None) -> None:
        """Dispatches MMIO read hooks."""
        hook = self._mmio_read_hooks.get(address)
        if hook:
            val = hook(address, size)
            if val is not None:
                try:
                    data = val.to_bytes(size, "little")
                    uc.mem_write(address, data)
                except Exception as err:
                    logger.warning("Error in MMIO read writeback for 0x%08x: %s", address, err)

    def _hook_mem_write(self, uc: Uc, access: int, address: int, size: int, value: int, user_data: None) -> None:
        """Dispatches MMIO write hooks."""
        hook = self._mmio_write_hooks.get(address)
        if hook:
            hook(address, size, value)

    def register_rom_hook(self, address: int, callback: Callable[["CortexM4Emulator", int], int | None]) -> None:
        """Registers a Python function to intercept execution at a ROM address."""
        self._rom_hooks[address & ~1] = callback

    def register_mmio_read(self, address: int, callback: Callable[[int, int], int]) -> None:
        """Registers a handler for MMIO reads at a specific address."""
        self._mmio_read_hooks[address] = callback

    def register_mmio_write(self, address: int, callback: Callable[[int, int, int], None]) -> None:
        """Registers a handler for MMIO writes at a specific address."""
        self._mmio_write_hooks[address] = callback

    def load_firmware(self, bin_data: bytes, base_address: int) -> None:
        """Writes the firmware binary into emulated flash memory."""
        self.uc.mem_write(base_address, bin_data)
        logger.info("Loaded %d firmware bytes at 0x%08x", len(bin_data), base_address)

    def reg_read(self, reg_id: int) -> int:
        return self.uc.reg_read(reg_id)

    def reg_write(self, reg_id: int, value: int) -> None:
        self.uc.reg_write(reg_id, value)

    def mem_read(self, address: int, size: int) -> bytes:
        return bytes(self.uc.mem_read(address, size))

    def mem_write(self, address: int, data: bytes) -> None:
        self.uc.mem_write(address, data)

    def read_u32(self, address: int) -> int:
        return struct.unpack("<I", self.mem_read(address, 4))[0]

    def write_u32(self, address: int, value: int) -> None:
        self.mem_write(address, struct.pack("<I", value))

    def boot(self, sp: int, reset_addr: int, max_steps: int = 1000000) -> None:
        """Boots the firmware starting from Reset vector until initial setup yields."""
        self.reg_write(UC_ARM_REG_SP, sp)
        self.reg_write(UC_ARM_REG_LR, STOP_ADDR | 1)
        try:
            self.uc.emu_start(reset_addr | 1, STOP_ADDR, count=max_steps)
            logger.info("Firmware boot completed initial sequence successfully")
        except unicorn.UcError as err:
            pc = self.reg_read(UC_ARM_REG_PC)
            if pc == STOP_ADDR:
                logger.info("Firmware boot completed and reached STOP trampoline")
            else:
                logger.debug("Boot stopped at PC=0x%08x: %s", pc, err)

    def call(self, address: int, *args: int, max_steps: int = 1000000) -> int:
        """Invokes a Thumb function inside the firmware and waits for return."""
        reg_args = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
        for i, val in enumerate(args[:4]):
            self.reg_write(reg_args[i], val)

        # Extra arguments are pushed onto the stack if > 4
        if len(args) > 4:
            sp = self.reg_read(UC_ARM_REG_SP)
            extra = args[4:]
            extra_bytes = b"".join(struct.pack("<I", a) for a in extra)
            new_sp = sp - len(extra_bytes)
            self.mem_write(new_sp, extra_bytes)
            self.reg_write(UC_ARM_REG_SP, new_sp)

        # Set LR to the STOP trampoline address
        self.reg_write(UC_ARM_REG_LR, STOP_ADDR | 1)

        try:
            self.uc.emu_start(address | 1, STOP_ADDR, count=max_steps)
        except unicorn.UcError as err:
            pc = self.reg_read(UC_ARM_REG_PC)
            if pc != STOP_ADDR:
                logger.debug("Execution finished at PC=0x%08x: %s", pc, err)

        return self.reg_read(UC_ARM_REG_R0)
