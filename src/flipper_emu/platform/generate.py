"""Render the Renode platform description for the emulated Flipper Zero.

The emulated bus is *generated* from :mod:`flipper_emu.fw.memmap`, so the address
map and the platform can never drift apart: every region and every peripheral in
the map appears in the generated ``.repl``, either as an existing Renode model or
as a Python stub that returns safe defaults.  The same run writes
``bus_map.json`` for tooling (and for the dispatch tests).

Run it directly to regenerate the platform::

    py -3.9 -m flipper_emu.platform.generate
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional, Tuple

from ..fw import memmap

#: The Flipper Zero runs its Cortex-M4 at 64 MHz (32 MHz HSE through the PLL);
#: SysTick and the timer clocks are derived from it.
SYSTEM_CLOCK_HZ = 64000000

#: Renode memory-region kind -> the declaration used in the platform file.
MEMORY_KINDS: Dict[str, str] = {
    "flash": "Memory.MappedMemory",
    "ram": "Memory.MappedMemory",
    "rom": "Memory.MappedMemory",
    "option-bytes": "Memory.MappedMemory",
}

#: Extra properties injected into a peripheral declaration, keyed by name.
#: Several Renode models take *required* constructor arguments, so these mirror
#: the way Renode's own STM32 platforms instantiate them.
PERIPHERAL_PROPERTIES: Dict[str, List[str]] = {
    "NVIC": ["priorityMask: 0xF0", "systickFrequency: %d" % SYSTEM_CLOCK_HZ],
    "TIM1": ["frequency: %d" % SYSTEM_CLOCK_HZ, "initialLimit: 0xFFFF"],
    "TIM2": ["frequency: %d" % SYSTEM_CLOCK_HZ, "initialLimit: 0xFFFF"],
    "TIM16": ["frequency: %d" % SYSTEM_CLOCK_HZ, "initialLimit: 0xFFFF"],
    "TIM17": ["frequency: %d" % SYSTEM_CLOCK_HZ, "initialLimit: 0xFFFF"],
    "IWDG": ["frequency: 32000", "windowOption: true", "defaultPrescaler: 0x0"],
    "LPTIM1": ["frequency: 0x1000000"],
    "LPTIM2": ["frequency: 0x1000000"],
    "FLASH": ["flash: flash", "eeprom: optionBytes"],
    "SPI1": ["series: STM32Series.F4"],
    "SPI2": ["series: STM32Series.F4"],
    "USART1": ["frequency: %d" % SYSTEM_CLOCK_HZ],
    "LPUART1": ["frequency: %d" % SYSTEM_CLOCK_HZ, "lowPowerMode: true"],
    "DMA1": ["numberOfChannels: 7"],
    "DMA2": ["numberOfChannels: 7"],
    "CRC": ["series: STM32Series.F0", "configurablePoly: true"],
    "EXTI": ["numberOfLines: 44"],
    # The WB55 has 32 hardware semaphores; the firmware uses 0 (RNG), 3 (RCC),
    # 4 (stop mode), 5 (CLK48) and 8 (BLE NVM SRAM).
    #
    # `bootChainLockMask` stands in for the boot loader this emulator does not run:
    # the release package is the application image alone, and the application
    # *verifies* (without taking) that CPU1 holds a semaphore before it uses the
    # clock/random source it guards - the compiled code reads `RLR` and furi_crashes
    # when the value is not `LOCK | COREID4`.
    #
    # Bit 5 (CLK48) is measured: with it the application gets past the
    # `furi_hal_bt_init` check and runs instead of crashing 555 times in 18 s.
    # Bit 0 (RNG) was tried and *changed nothing* - the application still spins in
    # `furi_hal_random_fill_buf` with RLR0 reading 0x80000400 - so it is not set:
    # that spin needs its own investigation (the app may take and release the
    # semaphore around its own RNG access).
    #
    # The mask is applied on every reset, exactly as the boot loader would.
    #
    # `coreIdWriteClaims` selects the semantics of a core-id write *without* the LOCK
    # bit: true = it claims the semaphore for that core, false = it releases it.
    # Measured: false is the right reading - the application waits for the RNG
    # semaphore to be held *before* using it and writes 0x400 afterwards, so with
    # `true` it never even reaches that write (RLR0 reads 0x00000000 at the first
    # wait and it spins there).
    #
    # `arbitratedSemaphores` is the resulting stand-in: bits whose semaphore is held
    # by CPU1 from reset and handed straight back whenever it is freed, because the
    # other side of that arbitration (the FUS on CPU2, which owns the RNG semaphore
    # on real silicon) is not part of this emulator.
    "HSEM": [
        "numberOfSemaphores: 32",
        "bootChainLockMask: 0x20",
        "coreIdWriteClaims: false",
        "arbitratedSemaphores: 0x1",
    ],
    # The RNG is deterministic (xorshift32 from this seed) so runs reproduce; it
    # exists so the firmware gets changing values instead of a constant, and is
    # explicitly not a source of entropy.
    "RNG": ["seed: 0x12345678"],
    # Our GPIO port models take the port letter, so their logs and DumpState line
    # up with the pin names in the firmware (PB11 is button LEFT, and so on).
    "GPIOA": ['portName: "A"'],
    "GPIOB": ['portName: "B"'],
    "GPIOC": ['portName: "C"'],
    "GPIOD": ['portName: "D"'],
    "GPIOE": ['portName: "E"'],
    "GPIOH": ['portName: "H"'],
}

#: EXTI line -> NVIC input, following the STM32 grouping (EXTI0..4 direct,
#: EXTI5..9 and EXTI10..15 through combined inputs).
EXTI_DIRECT: Tuple[Tuple[int, int], ...] = ((0, 6), (1, 7), (2, 8), (3, 9), (4, 10))
EXTI_GROUP_LOW = (5, 9, 23)  # lines 5..9 -> NVIC irq 23
EXTI_GROUP_HIGH = (10, 15, 40)  # lines 10..15 -> NVIC irq 40

#: Inline scripts for stubbed peripherals that need no state.  The access is
#: ignored on writes (assigning None to ``request.Value`` raises, since it is a
#: UInt64 property) and answered with a safe default on reads.
TRIVIAL_SCRIPTS: Dict[str, str] = {
    "lcd_wb55": "if request.IsRead: request.Value = 0",
    "wwdg_stub": "if request.IsRead: request.Value = 0",
    "crs_stub": "if request.IsRead: request.Value = 0",
    "usb_stub": "if request.IsRead: request.Value = 0",
    "vrefbuf_stub": "if request.IsRead: request.Value = 0x00000010",
    "sai_stub": "if request.IsRead: request.Value = 0",
    "dmamux_stub": "if request.IsRead: request.Value = 0",
    "tsc_stub": "if request.IsRead: request.Value = 0",
    "aes_stub": "if request.IsRead: request.Value = 0",
    "pka_stub": "if request.IsRead: request.Value = 0",
    "rng_stub": "if request.IsRead: request.Value = 0x00000001",
    "quadspi_stub": "if request.IsRead: request.Value = 0",
    "dbgmcu_stub": "if request.IsRead: request.Value = 0",
}

#: Used when a stub module file has not been written yet.
DEFAULT_STUB_SCRIPT = "if request.IsRead: request.Value = 0"

#: Devices that hang off a modelled bus instead of the memory map:
#: (declaration name, Renode type, bus, extra properties).
#: `{artifacts}` is expanded to an absolute path at generation time: Renode
#: resolves relative paths against its own root, not the caller's directory.
ATTACHED_DEVICES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "st7567",
        "Antmicro.Renode.Peripherals.FlipperEmu.St7567Display",
        "spi2",
        ('outputPath: "{artifacts}/display-stream.bin"',),
    ),
)


#: Extra signal connections from a GPIO port to an attached model:
#: port name -> ((pin, model, input index), ...).
#: The pins are the firmware's own, read out of its pin table in the 1.4.3 ELF:
#: `gpio_display_di` = PB1 (A0/DC: low = command, high = data) and
#: `gpio_display_rst_n` = PB0 (active low). Wiring them makes the recorder tag
#: every byte with real command/data framing, instead of the host inferring it.
GPIO_MODEL_CONNECTIONS: Dict[str, Tuple[Tuple[int, str, int], ...]] = {
    "GPIOB": ((1, "st7567", 0), (0, "st7567", 1)),
}


#: Peripheral interrupt outputs -> NVIC input, keyed by peripheral name.
#: The IRQ numbers were read out of the *firmware's own vector table* (the handler
#: address at vector slot 16 + n identifies which peripheral owns IRQ n), not from a
#: datasheet: LPTIM1 = 46, LPTIM2 = 47, RTC alarm = 40, TIM2 = 27, TIM16/TIM1_UP =
#: 24, TIM17/TIM1_TRG_COM = 25, USART1 = 35. Without these the firmware's tickless
#: idle arms LPTIM1, executes WFI, and nothing ever wakes it - measured: the CPU
#: frozen at `furi_hal_power_sleep+0xC3` with no instruction retirement for 20 s,
#: so the panel stopped being drawn (docs/BRINGUP_LOG.md §19).
#: RTC wakeup is deliberately absent: the firmware's `RTC_WKUP_IRQHandler` vector is
#: still the default handler, so it does not use that path.
PERIPHERAL_NVIC_IRQS: Dict[str, Tuple[Tuple[str, int], ...]] = {
    "TIM1": (("IRQ", 24),),
    "TIM2": (("IRQ", 27),),
    "TIM16": (("IRQ", 24),),
    "TIM17": (("IRQ", 25),),
    "LPTIM1": (("IRQ", 46),),
    "LPTIM2": (("IRQ", 47),),
    "USART1": (("IRQ", 35),),
    "RTC": (("AlarmIRQ", 40),),
}


def repo_root() -> str:
    """Repository root, derived from this file's location."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def stub_dir() -> str:
    """Directory holding the IronPython peripheral models."""
    return os.path.join(repo_root(), "src", "flipper_emu", "peripherals", "python")


def _renode_name(entry: memmap.Peripheral) -> str:
    """Registration name inside the platform description."""
    if entry.name.startswith("GPIO") and len(entry.name) == 5:
        return "gpioPort" + entry.name[-1]
    if entry.name == "FLASH":
        return "flashController"
    return entry.name.lower()


def _region_name(region: memmap.Region) -> str:
    return "optionBytes" if region.name == "option-bytes" else region.name


def _stub_script_lines(entry: memmap.Peripheral) -> List[str]:
    """``script``/``filename`` lines for a peripheral served by a Python stub."""
    path = os.path.join(stub_dir(), "%s.py" % entry.stub)
    if os.path.exists(path):
        return ['filename: "%s"' % path.replace("\\", "/")]
    script = TRIVIAL_SCRIPTS.get(entry.stub)
    if script is None:
        script = DEFAULT_STUB_SCRIPT
    return ['script: "%s"' % script]


def _exti_range(first: int, last: int) -> str:
    """Connection line sending pins ``first..last`` to the matching EXTI lines."""
    if first == last:
        return "%d -> exti@%d" % (first, first)
    # Renode's range form: `[a-b] -> peripheral@[i-j]` maps index n to i + (n - a).
    return "[%d-%d] -> exti@[%d-%d]" % (first, last, first, last)


def _gpio_lines(port: str) -> List[str]:
    """Connections for one GPIO port.

    Every port can drive EXTI line n through the EXTICR-selected pin n, but the
    platform language allows an output to feed exactly one destination (connecting
    twice fails with "already been used as a source"). So the pins that feed an
    attached model are spelled out, and the remaining pins go to EXTI as ranges.
    """
    specials = {
        pin: (model, index) for pin, model, index in GPIO_MODEL_CONNECTIONS.get(port, ())
    }
    lines: List[str] = []
    run_start = None
    for pin in range(16):
        if pin in specials:
            if run_start is not None:
                lines.append(_exti_range(run_start, pin - 1))
                run_start = None
        elif run_start is None:
            run_start = pin
    if run_start is not None:
        lines.append(_exti_range(run_start, 15))
    for pin in sorted(specials):
        model, index = specials[pin]
        lines.append("%d -> %s@%d" % (pin, model, index))
    return lines


def connection_lines(entry: memmap.Peripheral) -> List[str]:
    """Signal connections declared under a peripheral (Renode ``->`` syntax)."""
    if entry.name.startswith("GPIO"):
        return _gpio_lines(entry.name)
    if entry.name == "EXTI":
        # Renode uses bottom-index lists for range connections: `[a-b] -> x@[i-j]`.
        lines = ["[%d] -> nvic@[%d]" % (line, irq) for line, irq in EXTI_DIRECT]
        low_start, low_end, _ = EXTI_GROUP_LOW
        high_start, high_end, _ = EXTI_GROUP_HIGH
        lines.append("[%d-%d] -> extiLowGroup@[0-%d]" % (low_start, low_end, low_end - low_start))
        lines.append(
            "[%d-%d] -> extiHighGroup@[0-%d]" % (high_start, high_end, high_end - high_start)
        )
        return lines
    if entry.name in PERIPHERAL_NVIC_IRQS:
        return [
            "%s -> nvic@%d" % (signal, irq)
            for signal, irq in PERIPHERAL_NVIC_IRQS[entry.name]
        ]
    return []


def peripheral_lines(entry: memmap.Peripheral) -> List[str]:
    """Render one peripheral declaration, its properties and its connections."""
    name = _renode_name(entry)
    if entry.is_modelled:
        # `<base, +size>` is the registration form that works for models without
        # a default size (e.g. GPIOPort.STM32_GPIOPort rejects a bare address).
        lines = [
            "%s: %s @ sysbus <0x%08X, +0x%X>" % (name, entry.model, entry.base, entry.size)
        ]
    else:
        lines = ["%s: Python.PythonPeripheral @ sysbus 0x%08X" % (name, entry.base)]
        lines.append("    size: 0x%X" % entry.size)
        lines.extend("    " + line for line in _stub_script_lines(entry))
    lines.extend("    " + line for line in PERIPHERAL_PROPERTIES.get(entry.name, []))
    if entry.name == "NVIC":
        lines.append("    IRQ -> cpu@0")
    lines.extend("    " + line for line in connection_lines(entry))
    return lines


def render_repl() -> str:
    """Render the complete platform description."""
    lines = [
        "// Generated by flipper_emu.platform.generate -- do not edit by hand.",
        "// Emulated board: Flipper Zero (STM32WB55RG, Cortex-M4F @ 64 MHz).",
        "",
        "cpu: CPU.CortexM @ sysbus",
        '    cpuType: "cortex-m4"',
        "    nvic: nvic",
        "",
    ]

    # Memory first, then peripherals, so the file reads top-down and any
    # truncation of it stays loadable (useful when bisecting platform errors).
    for region in memmap.MEMORY_REGIONS:
        declaration = MEMORY_KINDS[region.kind]
        if region.name == "flash":
            # An STM32 boots with the flash aliased to address 0, and that is
            # where the Cortex-M model fetches its initial SP and PC from, so the
            # flash has to answer at both addresses (one memory, two mappings).
            lines.append("flash: %s @ {" % declaration)
            lines.append("    sysbus 0x00000000;")
            lines.append("    sysbus 0x%08X" % region.start)
            lines.append("    }")
        else:
            lines.append(
                "%s: %s @ sysbus 0x%08X" % (_region_name(region), declaration, region.start)
            )
        lines.append("    size: 0x%X" % region.size)
        lines.append("")

    for entry in memmap.PERIPHERALS:
        lines.extend(peripheral_lines(entry))
        lines.append("")

    artifacts_dir = os.path.join(repo_root(), "artifacts")
    for name, model, bus, properties in ATTACHED_DEVICES:
        lines.append("%s: %s @ %s" % (name, model, bus))
        for prop in properties:
            lines.append("    " + prop.replace("{artifacts}", artifacts_dir.replace("\\", "/")))
        lines.append("")

    # Grouped EXTI lines reach the NVIC through combined inputs.
    for name, count, irq in (
        ("extiLowGroup", EXTI_GROUP_LOW[1] - EXTI_GROUP_LOW[0] + 1, EXTI_GROUP_LOW[2]),
        ("extiHighGroup", EXTI_GROUP_HIGH[1] - EXTI_GROUP_HIGH[0] + 1, EXTI_GROUP_HIGH[2]),
    ):
        lines.append("%s: Miscellaneous.CombinedInput" % name)
        lines.append("    numberOfInputs: %d" % count)
        lines.append("    -> nvic@%d" % irq)
        lines.append("")

    for entry in memmap.PERIPHERALS:
        if entry.is_modelled:
            continue
        lines.append('sysbus:')
        break
    if any(not entry.is_modelled for entry in memmap.PERIPHERALS):
        lines.append("    init:")
        for entry in memmap.PERIPHERALS:
            if not entry.is_modelled:
                lines.append(
                    '        Tag <0x%08X, 0x%08X> "%s"'
                    % (entry.base, entry.end - 1, "%s (stub)" % entry.name)
                )
        lines.append("")
    return "\n".join(lines)

def render_resc(repl_path: str, flash_path: str) -> str:
    """Startup script: create the machine, load the platform and the firmware."""
    repl = repl_path.replace("\\", "/")
    flash = flash_path.replace("\\", "/")
    return "\n".join(
        [
            "# Generated by flipper_emu.platform.generate",
            "$platform = @%s" % repl,
            "$flash = @%s" % flash,
            'mach create "flipper"',
            "machine LoadPlatformDescription $platform",
            "sysbus LoadBinary $flash 0x%08X" % memmap.FLASH_BASE,
            "",
            "# Identity/capacity registers the firmware reads from the system block.",
            "sysbus WriteDoubleWord 0x%08X 0x400" % memmap.FLASHSIZE_BASE,  # 1 MiB in KiB
            "sysbus WriteDoubleWord 0x%08X 0x00000001" % memmap.UID_BASE,
            "sysbus WriteDoubleWord 0x%08X 0x00000002" % (memmap.UID_BASE + 4),
            "sysbus WriteDoubleWord 0x%08X 0x00000003" % (memmap.UID_BASE + 8),
            "",
            "start",
            "",
            "# Board inputs, injected once the machine is running.  The start-up",
            "# reset clears peripheral state, so injection before `start` is wasted",
            "# (levels themselves survive later resets: the port model treats them as",
            "# board wiring, not peripheral state).  The port model must honour these",
            "# for the boot code to see released buttons: a Renode GPIO port without an",
            "# input path reads every input pin low, and low means \"pressed\" for all of",
            "# these except OK - which made the 1.4.3 bootloader see LEFT (its DFU",
            "# entry) and UP (its recovery entry) held and show the 'Update & Recovery",
            "# Mode / DFU Started' splash instead of starting the firmware.",
            "# Pin table from the release ELF's own GpioPin structs:",
            "#   gpio_button_left PB11, gpio_button_right PB12, gpio_button_up PB10,",
            "#   gpio_button_down PC6, gpio_button_back PC13 (all active low),",
            "#   gpio_button_ok PH3 (active high), gpio_sdcard_cd PC10 (active low).",
            "gpioPortB OnGPIO 10 true",
            "gpioPortB OnGPIO 11 true",
            "gpioPortB OnGPIO 12 true",
            "gpioPortC OnGPIO 6 true",
            "gpioPortC OnGPIO 13 true",
            "gpioPortH OnGPIO 3 false",
            "gpioPortC OnGPIO 10 true",
            "",
        ]
    )


def write_platform(out_dir: Optional[str] = None, flash_path: Optional[str] = None) -> Dict[str, str]:
    """Write the platform files and return their paths."""
    out_dir = out_dir or os.path.join(repo_root(), "platform")
    os.makedirs(out_dir, exist_ok=True)
    repl_path = os.path.join(out_dir, "stm32wb55_flipper.repl")
    resc_path = os.path.join(out_dir, "flipper_zero.resc")
    map_path = os.path.join(out_dir, "bus_map.json")
    flash_path = flash_path or os.path.join(repo_root(), "artifacts", "flash.img")

    with open(repl_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_repl())
    with open(resc_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_resc(repl_path, flash_path))
    with open(map_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "regions": memmap.region_records(),
                "peripherals": memmap.peripheral_records(),
                "system_clock_hz": SYSTEM_CLOCK_HZ,
            },
            handle,
            indent=2,
        )
        handle.write("\n")
    return {"repl": repl_path, "resc": resc_path, "bus_map": map_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Renode platform description")
    parser.add_argument("--out-dir", default=None, help="where to write the platform files")
    parser.add_argument("--flash", default=None, help="path to the flash image for the .resc")
    parser.add_argument("--print", dest="print_repl", action="store_true", help="dump the .repl")
    args = parser.parse_args()

    paths = write_platform(args.out_dir, args.flash)
    for key, value in sorted(paths.items()):
        print("%-9s %s" % (key, value))
    print(
        "bus: %d peripherals (%d modelled, %d stubbed), %d memory regions"
        % (
            len(memmap.PERIPHERALS),
            len(memmap.modelled_peripherals()),
            len(memmap.stubbed_peripherals()),
            len(memmap.MEMORY_REGIONS),
        )
    )
    if args.print_repl:
        with open(paths["repl"], "r", encoding="utf-8") as handle:
            print(handle.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


