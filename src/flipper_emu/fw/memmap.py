"""STM32WB55RG memory map: the single source of truth for the emulated bus.

Every base address below is taken verbatim from ST's CMSIS device header
(``stm32wb55xx.h``, ``Drivers/CMSIS/Device/ST/STM32WBxx/Include``), so the map
can be re-derived instead of remembered.  The flash split (768 KiB application /
256 KiB wireless-stack reserve) matches what the official ``.dfu`` packages
actually contain: their single element is 768,132 bytes at ``0x08000000``.

``platform/generate.py`` renders the Renode platform description from these
tables, and ``tests/test_memmap_dispatch.py`` asserts the invariants that make
the bus dispatch sane (no overlaps, power-of-two sizes, aligned bases, and every
region either modelled or explicitly stubbed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Flash and RAM
# ---------------------------------------------------------------------------
FLASH_BASE = 0x08000000
FLASH_SIZE = 0x00100000  # 1 MiB, FLASH_BASE
FLASH_APP_SIZE = 0x000C0000  # 768 KiB available to the application
FLASH_STACK_BASE = FLASH_BASE + FLASH_APP_SIZE  # 0x080C0000: FUS + wireless stack
FLASH_APP_END = FLASH_STACK_BASE  # exclusive

SRAM_BASE = 0x20000000
SRAM1_BASE = 0x20000000  # 192 KiB
SRAM1_SIZE = 0x00030000
SRAM2A_BASE = 0x20030000  # 32 KiB (shared with the CPU2 / IPCC mailboxes)
SRAM2A_SIZE = 0x00008000
SRAM2B_BASE = 0x20038000  # 32 KiB (wireless stack image, loaded by the FUS)
SRAM2B_SIZE = 0x00008000
SRAM_SIZE = 0x00040000  # 256 KiB total

# ---------------------------------------------------------------------------
# System information blocks (read-only data, not peripherals)
# ---------------------------------------------------------------------------
SYSTEM_MEMORY_BASE = 0x1FFF0000  # 28 KiB: system bootloader
SYSTEM_MEMORY_SIZE = 0x00007000  # 0x1FFF0000 - 0x1FFF6FFF
OTP_AREA_BASE = 0x1FFF7000  # 1 KiB, OTP_AREA_BASE
OTP_AREA_SIZE = 0x00000400  # 0x1FFF7000 - 0x1FFF73FF
ENGI_BYTE_BASE = 0x1FFF7400  # 3 KiB, ENGI_BYTE_BASE
ENGI_BYTE_SIZE = 0x00000C00  # 0x1FFF7400 - 0x1FFF7FFF
PACKAGE_BASE = 0x1FFF7500  # package data register
UID64_BASE = 0x1FFF7580  # 64-bit unique id
UID_BASE = 0x1FFF7590  # 96-bit unique id
FLASHSIZE_BASE = 0x1FFF75E0  # flash size register
OPTION_BYTE_BASE = 0x1FFF8000  # 4 KiB, OPTION_BYTE_BASE
OPTION_BYTE_SIZE = 0x00001000  # 0x1FFF8000 - 0x1FFF8FFF

# ---------------------------------------------------------------------------
# Cortex-M4 private peripherals
# ---------------------------------------------------------------------------
SCS_BASE = 0xE000E000  # NVIC + SysTick + SCB (system control space)
SCS_SIZE = 0x00001000
DWT_BASE = 0xE0001000  # data watchpoint and trace: CYCCNT lives at +0x04
DWT_SIZE = 0x00001000
DBGMCU_BASE = 0xE0042000

# ---------------------------------------------------------------------------
# Bus bases (PERIPH_BASE + region offsets)
# ---------------------------------------------------------------------------
PERIPH_BASE = 0x40000000
APB1PERIPH_BASE = 0x40000000
APB2PERIPH_BASE = 0x40010000
AHB1PERIPH_BASE = 0x40020000
AHB2PERIPH_BASE = 0x48000000
AHB4PERIPH_BASE = 0x58000000

@dataclass(frozen=True)
class Region:
    """A mapped memory region (flash, RAM, or an information block)."""

    name: str
    start: int
    size: int
    kind: str  # flash | ram | rom | option-bytes

    @property
    def end(self) -> int:
        return self.start + self.size

    def contains(self, address: int) -> bool:
        return self.start <= address < self.end


@dataclass(frozen=True)
class Peripheral:
    """One peripheral register block on the bus.

    ``model`` is the Renode model class name when an existing model covers the
    peripheral.  When it is ``None`` the block is served by a *stub* (``stub``
    names the stub id) returning safe defaults: those are exactly the entries
    the bring-up log should report as "touched but not implemented yet".
    """

    name: str
    base: int
    size: int
    model: Optional[str]
    stub: Optional[str]
    note: str = ""

    @property
    def end(self) -> int:
        return self.base + self.size

    @property
    def is_modelled(self) -> bool:
        return self.model is not None

    def contains(self, address: int) -> bool:
        return self.base <= address < self.end


#: Mapped memory regions.  Sizes are powers of two with aligned bases so the
#: generated Renode platform description is always structurally valid.
MEMORY_REGIONS: Tuple[Region, ...] = (
    Region("flash", FLASH_BASE, FLASH_SIZE, "flash"),
    Region("sram1", SRAM1_BASE, SRAM1_SIZE, "ram"),
    Region("sram2a", SRAM2A_BASE, SRAM2A_SIZE, "ram"),
    Region("sram2b", SRAM2B_BASE, SRAM2B_SIZE, "ram"),
    Region("sysinfo", SYSTEM_MEMORY_BASE, 0x8000, "rom"),
    Region("option-bytes", OPTION_BYTE_BASE, OPTION_BYTE_SIZE, "option-bytes"),
)

#: The GPIO port model for the whole board.  It is ours rather than Renode's
#: ``GPIOPort.STM32_GPIOPort`` because that one has no input path: an input pin
#: always reads 0, ``OnGPIO`` never reaches ``IDR``, and pull-up/down is ignored.
#: The 1.4.3 bootloader decides between its DFU splash, its recovery splash and a
#: normal boot by *polling* button levels, so with the stock model it always saw
#: LEFT (PB11) held and never started the firmware.  See
#: ``peripherals/cs/GpioWb55Port.cs``.
GPIO_PORT_MODEL = "Antmicro.Renode.Peripherals.FlipperEmu.GpioWb55Port"

#: The hardware-semaphore block, also ours: its register map was read out of the
#: firmware's own accesses (see ``peripherals/cs/HsemWb55.cs``).  The previous
#: zero-returning stub made every semaphore read look free, and `furi_hal_bt_init`
#: furi_checks that the CLK48 semaphore reads back as held by CPU1.
HSEM_MODEL = "Antmicro.Renode.Peripherals.FlipperEmu.HsemWb55"

#: The RNG, modelled so the firmware gets changing values instead of the constant
#: the old stub returned (`if request.IsRead: request.Value = 1`).
RNG_MODEL = "Antmicro.Renode.Peripherals.FlipperEmu.RngWb55"

#: The low-power timers, modelled rather than left to Renode's stock
#: ``Timers.STM32L0_LpTimer``.  That model fires on its own ``LimitTimer`` limit
#: instead of the firmware's *compare* match (it logs "Compare value (16117) cannot
#: be greater than auto reload limit (1). Compare value will be ignored"), so it
#: fired at the wrong time and kept re-raising LPTIM1's IRQ.  The firmware has no
#: ISR registered for that interrupt - it polls the flags with interrupts masked -
#: so delivery ended in ``furi_check(isr_descr->isr)`` and reset the chip 96 times
#: in 20 s.  See docs/ISSUES_AND_LOGS.md (P24) and peripherals/cs/LptimWb55.cs.
LPTIM_MODEL = "Antmicro.Renode.Peripherals.FlipperEmu.LptimWb55"

#: Register blocks: ``(name, base, size, renode model, stub id, note)``.
#: ``model`` names are Renode 1.17 classes; ``None`` means "served by the stub
#: module named in the ``stub`` column until the bring-up log says otherwise".
_PERIPHERAL_TABLE: Tuple[Tuple[str, int, int, Optional[str], Optional[str], str], ...] = (
    # --- APB1 (0x40000000) -------------------------------------------------
    ("TIM2", 0x40000000, 0x400, "Timers.STM32_Timer", None, "IR carrier / RFID timing"),
    # The firmware's RTC view.  Its own accessors all use this base (0x40002000)
    # with BKPxR at +0x50; leaving it unmapped made Renode answer reads with all
    # ones, which sets every RTC flag and sent the boot code down its DFU path.
    ("RTC_BACKUP", 0x40002000, 0x400, None, "rtc_wb55", "firmware's RTC + BKPxR bank"),
    ("LCD", 0x40002400, 0x400, None, "lcd_wb55", "glass LCD driver, unused by Flipper"),
    ("RTC", 0x40002800, 0x400, "Timers.STM32F4_RTC", None, "AlarmIRQ/WakeupIRQ -> EXTI"),
    ("WWDG", 0x40002C00, 0x400, None, "wwdg_stub", ""),
    ("IWDG", 0x40003000, 0x400, "Timers.STM32_IndependentWatchdog", None, "frequency 32000"),
    ("SPI2", 0x40003800, 0x400, "SPI.STM32SPI", None, "microSD in SPI mode"),
    ("I2C1", 0x40005400, 0x400, "I2C.STM32F7_I2C", None, "bq27220 gauge / bq25896 charger (absent -> fast NAK)"),
    ("I2C3", 0x40005C00, 0x400, "I2C.STM32F7_I2C", None, "vibro driver LP5562"),
    ("CRS", 0x40006000, 0x400, None, "crs_stub", ""),
    ("USB1", 0x40006800, 0x400, None, "usb_stub", "USB CDC console; not needed to boot"),
    ("LPTIM1", 0x40007C00, 0x400, LPTIM_MODEL, None,
 "tickless-idle wakeup timer: the stock Renode timer fired on the wrong condition"),
    ("LPUART1", 0x40008000, 0x400, "UART.STM32F7_USART", None, ""),
    ("LPTIM2", 0x40009400, 0x400, LPTIM_MODEL, None, "second LPTIM, same model"),
    # --- APB2 (0x40010000) -------------------------------------------------
    ("SYSCFG", 0x40010000, 0x20, None, "syscfg_wb55", "EXTICR line routing + MEMRMP remap (OTA)"),
    ("VREFBUF", 0x40010030, 0x4, None, "vrefbuf_stub", ""),
    ("COMP1", 0x40010200, 0x4, None, "comp_wb55", "125 kHz RFID demodulator"),
    ("COMP2", 0x40010204, 0x4, None, "comp_wb55", "125 kHz RFID demodulator"),
    ("TIM1", 0x40012C00, 0x400, "Timers.STM32_Timer", None, "IR / RFID carrier"),
    ("SPI1", 0x40013000, 0x400, "SPI.STM32SPI", None, "display, SubGHz (CC1101), NFC (ST25R3916)"),
    ("USART1", 0x40013800, 0x400, "UART.STM32F7_USART", None, "debug console output"),
    ("TIM16", 0x40014400, 0x400, "Timers.STM32_Timer", None, ""),
    ("TIM17", 0x40014800, 0x400, "Timers.STM32_Timer", None, "vibro PWM"),
    ("SAI1", 0x40015400, 0x400, None, "sai_stub", "I2S speaker, unused"),

    # --- AHB1 (0x40020000) -------------------------------------------------
    ("DMA1", 0x40020000, 0x400, "Antmicro.Renode.Peripherals.FlipperEmu.DmaWb55", None,
     "channel-based DMA; the per-channel IRQs (11..17, 55..61) are what the SD block reads wait on"),
    ("DMA2", 0x40020400, 0x400, "Antmicro.Renode.Peripherals.FlipperEmu.DmaWb55", None,
     "SPI2 RX = channel 6 -> IRQ 60, TX = channel 7 -> IRQ 61 (furi_hal_spi.c)"),
    ("DMAMUX1", 0x40020800, 0x400, None, "dmamux_stub", "DMA request routing"),
    ("CRC", 0x40023000, 0x400, "CRC.STM32_CRC", None, "resource/flash checksums"),
    ("TSC", 0x40024000, 0x400, None, "tsc_stub", "unused"),
    # --- AHB2 GPIOs (0x48000000) -------------------------------------------
    # Our C# port model instead of Renode's GPIOPort.STM32_GPIOPort: that model
    # cannot make an input pin read high (IDR stays 0 for inputs, OnGPIO never
    # lands, pull-ups are ignored), and the 1.4.3 bootloader decides between the
    # DFU splash, recovery and a normal boot by *polling* those levels - LEFT
    # (PB11) and UP (PB10) read "pressed", so it always showed the DFU splash.
    ("GPIOA", 0x48000000, 0x400, GPIO_PORT_MODEL, None, "display + radio control lines"),
    ("GPIOB", 0x48000400, 0x400, GPIO_PORT_MODEL, None, "buttons, IR, RFID"),
    ("GPIOC", 0x48000800, 0x400, GPIO_PORT_MODEL, None, "SubGHz/NFC chip selects, SD detect"),
    ("GPIOD", 0x48000C00, 0x400, GPIO_PORT_MODEL, None, ""),
    ("GPIOE", 0x48001000, 0x400, GPIO_PORT_MODEL, None, "power rails"),
    ("GPIOH", 0x48001C00, 0x400, GPIO_PORT_MODEL, None, "32.768 kHz oscillator"),
    # --- the 0x50000000 block ---------------------------------------------
    ("ADC1", 0x50040000, 0x400, "Analog.STM32_ADC", None, "battery / RFID level sensing"),
    ("AES1", 0x50060000, 0x400, None, "aes_stub", ""),
    # --- AHB4 (0x58000000) -------------------------------------------------
    ("RCC", 0x58000000, 0x400, None, "rcc_wb55",
     "WB55 register map; the L0 model has different offsets and breaks clock-enable read-back"),
    ("PWR", 0x58000400, 0x400, None, "pwr_wb55", "CPU2 boot (C2BOOT) + low power"),
    ("EXTI", 0x58000800, 0x400, "Antmicro.Renode.Peripherals.FlipperEmu.Wb55Exti", None,
     "our C# model: the WB55 has IMR1 at 0x80 and RTSR1 at 0x00, which is not Renode's F4 layout"),
    ("IPCC", 0x58000C00, 0x400, None, "ipcc_wb55",
     "CPU2 mailbox: handshake auto-answered; shared-SRAM payload not yet synthesised"),
    ("RNG", 0x58001000, 0x400, RNG_MODEL, None,
     "random values for seeds and BLE key material; was a stub returning 1 for every read"),
    ("HSEM", 0x58001400, 0x400, HSEM_MODEL, None,
     "hardware semaphores: furi_hal_bt_init checks the CLK48 semaphore here, so a "
     "zero-returning stub fails its furi_check"),
    ("AES2", 0x58001800, 0x400, None, "aes_stub", ""),
    ("PKA", 0x58002000, 0x400, None, "pka_stub", ""),
    ("FLASH", 0x58004000, 0x400, "MTD.STM32L0_FlashController", None,
     "WB55 shares the L0 register layout (KEYR 0x08/SR 0x10/CR 0x14/OPTR 0x20); "
     "the model requires an 'eeprom' binding, which we point at the option bytes"),
    ("QUADSPI", 0xA0001000, 0x400, None, "quadspi_stub", "not used by Flipper Zero"),
    # --- Cortex-M4 private -------------------------------------------------
    ("NVIC", SCS_BASE, SCS_SIZE, "IRQControllers.NVIC", None, "SysTick frequency drives firmware timing"),
    ("DWT", DWT_BASE, DWT_SIZE, "Antmicro.Renode.Peripherals.FlipperEmu.DwtWb55", None,
     "CYCCNT steps one microsecond per read: honest deltas for furi_delay_us and the cortex timers"),
    ("DBGMCU", DBGMCU_BASE, 0x400, None, "dbgmcu_stub", ""),
)

PERIPHERALS: Tuple[Peripheral, ...] = tuple(
    Peripheral(name, base, size, model, stub, note)
    for name, base, size, model, stub, note in _PERIPHERAL_TABLE
)

def _start(entry: object) -> int:
    """Start address of a :class:`Region` (``start``) or :class:`Peripheral` (``base``)."""
    value = getattr(entry, "start", None)
    return value if value is not None else getattr(entry, "base")


def region_for(address: int) -> Optional[Region]:
    """Return the memory region containing ``address``, if any."""
    for region in MEMORY_REGIONS:
        if region.contains(address):
            return region
    return None


def peripheral_for(address: int) -> Optional[Peripheral]:
    """Return the peripheral register block containing ``address``, if any."""
    for peripheral in PERIPHERALS:
        if peripheral.contains(address):
            return peripheral
    return None


def find_overlaps(entries: Sequence[object]) -> List[Tuple[str, str]]:
    """Pairs of entries whose ranges intersect (drives the dispatch tests)."""
    problems: List[Tuple[str, str]] = []
    ordered = sorted(entries, key=_start)
    for first, second in zip(ordered, ordered[1:]):
        if _start(second) < _start(first) + first.size:
            problems.append((first.name, second.name))
    return problems


def alignment_issues(entries: Sequence[object]) -> List[str]:
    """Peripheral register blocks must have power-of-two, base-aligned sizes.

    Memory regions are exempt: real STM32 SRAM blocks are not powers of two
    (SRAM1 is 192 KiB = 0x30000) and Renode accepts that, so callers pass the
    set they care about — peripherals for this check, regions for the overlap
    check.
    """
    issues: List[str] = []
    for entry in entries:
        address, size = _start(entry), entry.size
        if size <= 0 or (size & (size - 1)) != 0:
            issues.append("%s: size 0x%X is not a power of two" % (entry.name, size))
        elif address % size != 0:
            issues.append("%s: base 0x%08X is not aligned to size 0x%X" % (entry.name, address, size))
    return issues


def overlap_issues() -> List[str]:
    """Overlaps among regions, among peripherals, and between the two sets."""
    issues: List[str] = []
    for first, second in find_overlaps(PERIPHERALS):
        issues.append("peripherals overlap: %s / %s" % (first, second))
    for first, second in find_overlaps(MEMORY_REGIONS):
        issues.append("regions overlap: %s / %s" % (first, second))
    for region in MEMORY_REGIONS:
        for peripheral in PERIPHERALS:
            if peripheral.base < region.end and region.start < peripheral.end:
                issues.append("region/peripheral overlap: %s / %s" % (region.name, peripheral.name))
    return issues


def modelled_peripherals() -> Tuple[Peripheral, ...]:
    """Peripherals backed by an existing Renode model."""
    return tuple(entry for entry in PERIPHERALS if entry.is_modelled)


def stubbed_peripherals() -> Tuple[Peripheral, ...]:
    """Peripherals served by safe-default stubs instead of a real model."""
    return tuple(entry for entry in PERIPHERALS if not entry.is_modelled)


def peripheral_records() -> List[dict]:
    """JSON-friendly view of the bus map (consumed by ``platform/generate.py``)."""
    return [
        {
            "name": entry.name,
            "base": entry.base,
            "size": entry.size,
            "end": entry.end,
            "model": entry.model,
            "stub": entry.stub,
            "note": entry.note,
            "status": "modelled" if entry.is_modelled else "stubbed",
        }
        for entry in PERIPHERALS
    ]


def region_records() -> List[dict]:
    """JSON-friendly view of the memory regions."""
    return [
        {"name": entry.name, "start": entry.start, "size": entry.size, "kind": entry.kind}
        for entry in MEMORY_REGIONS
    ]


def describe() -> str:
    """Human-readable map, used by the CLI and the bring-up log."""
    lines = ["memory regions:"]
    for region in MEMORY_REGIONS:
        lines.append("  0x%08X..0x%08X %-13s %s" % (region.start, region.end, region.name, region.kind))
    lines.append(
        "peripherals: %d total, %d modelled, %d stubbed"
        % (len(PERIPHERALS), len(modelled_peripherals()), len(stubbed_peripherals()))
    )
    for entry in PERIPHERALS:
        status = entry.model if entry.is_modelled else "STUB:%s" % entry.stub
        lines.append(
            "  0x%08X..0x%08X %-8s %-38s %s"
            % (entry.base, entry.end, entry.name, status, entry.note)
        )
    return "\n".join(lines)




