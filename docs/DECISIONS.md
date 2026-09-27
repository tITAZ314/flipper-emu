# Decisions

Architecture decisions with the evidence behind them, so they can be revisited
rather than re-litigated.

## D1 — Renode as the CPU/bus emulator (over Unicorn and QEMU)

**Decision.** Use Renode 1.17.0 (MIT) as the emulation backend, with a platform
description we author for the STM32WB55RG.

**Why.** Three candidates were evaluated on this machine:

| Backend | Evidence |
|---|---|
| **Renode 1.17.0** | Ships a Windows build, ran here unmodified (`Renode v1.17.0`, .NET 8.0.10). Contains the hard parts already: `CPU.CortexM` (`cortex-m4`), `IRQControllers.NVIC` (+SysTick), `GPIOPort.STM32_GPIOPort`, `SPI.STM32SPI`, `UART.STM32F7_USART`, `DMA.STM32G0DMA`, `Timers.STM32_Timer`, `Timers.STM32L0_LpTimer`, `MTD.STM32L0_FlashController`, `Timers.STM32F4_RTC`, `CRC.STM32_CRC`, `Analog.STM32_ADC`. Its shipped `.repl` files were used as the reference for correct constructor arguments. Custom peripherals can be written in C# (built here with `dotnet` targeting `net8.0` against the shipped DLLs, ~9 s) or in IronPython. |
| **Unicorn 2.1.4** | Installs fine by hand, has `UC_MODE_MCLASS`, `MSP/PSP/XPSR/IPSR` registers — but **no `UC_CPU_ARM_CORTEX_M4` in the Python binding**, no NVIC/interrupt support (upstream issue #825 was closed without an answer), and the "FreeRTOS on Cortex-M4" issue (#1893) is still open with the reporter stuck on exactly the `EXC_RETURN`/`bx lr` path this firmware needs. Choosing it means hand-writing NVIC, SysTick, DWT, SCB *and* every peripheral. |
| **QEMU system mode** | No STM32WB machine exists, so a WB55 SoC would have to be written in C; and there is **no C toolchain on this machine** (`cmake`, `arm-none-eabi-gcc`, MSYS2 all absent). |

The "run real firmware" claim has a demonstration path on Renode, and the
prior art that proved it (`d4rks1d33/Flipper-Zero-Emulator`) is appreciated as
**conceptual reference only**: that repository has no license file, so no code
was taken from it. Everything here was written against Renode's own
documentation, its shipped platforms and its error messages.

**Cost accepted.** The bus is Renode's sysbus, not a Python object graph. This is
compensated with a single source of truth for the address map (D2) and a
generated platform, so peripherals remain swappable and dispatch stays testable.

## D2 — One address map that generates the platform

`fw/memmap.py` holds every region and register block, with base addresses taken
verbatim from ST's CMSIS header (`stm32wb55xx.h`). `platform/generate.py` renders
the `.repl`, `.resc` and `bus_map.json` from it. Consequences:

* the emulated bus and the map cannot drift apart;
* `tests/test_memmap_dispatch.py` can assert real invariants (no overlaps,
  valid registration sizes, everything modelled or explicitly stubbed);
* the peripheral table doubles as the work queue: a block with `model=None` is a
  stub, and the bring-up digest reports when the firmware needs it.

Generating the file also made error-hunting tractable: a prefix of a generated
`.repl` is always loadable, which is what the dependency-aware prefix bisect used
to find the `GPIOPort` registration problem.

## D3 — Python stubs must be IronPython 2.7 and must not log per access

Measured on this build: `PythonPeripheral` scripts are re-executed on **every**
bus access, with `request` (`IsRead`, `IsWrite`, `Offset`, `Absolute`, `Length`,
`Value`) bound, and module-level state persists between accesses while `self`
attributes cannot be set. Two rules follow:

1. register-file stubs guard their initialisation (`try: _state / except
   NameError:`) and keep state in module globals;
2. stubs never log per access — a model that did produced an 18 MB log in 25 s,
   which buries the signal.

## D4 — Flash persistence by dump-on-exit, not by patching Renode

The flash lives in `artifacts/flash.img` (1 MiB). It is loaded with
`LoadBinary` and written back on shutdown by a monitor-Python script that calls
`SystemBus.ReadBytes`. The script validates the vector table first, so a failed
platform load can never overwrite a working image (this happened once during
bring-up and is now guarded, with a test).

## D5 — Stdlib-only host tooling

`pip` fails in this environment (`PermissionError` on connection), so the host
side — DFU loader, platform generator, runner, log analyser, tests — uses only
the Python 3.9 standard library, and the tests run under `unittest`. Nothing in
the project needs an install step beyond `tools/fetch_renode.ps1`.

## D6 — Keep the firmware honest

No firmware patches, no `FLIPPER_EMULATOR` defines, no recompilation: the input
is the same `.dfu` a physical Flipper would receive. When the emulator is not
faithful enough, the fix goes into a peripheral model, and the digest is the
evidence that something is still missing rather than silently wrong.

## D7 — C# peripherals are loaded as a Renode plugin under `Antmicro.Renode.*`

Python peripherals cannot cover everything: `Python.PythonPeripheral` implements
only the bus-width interfaces, so GPIO consumers (`IGPIOReceiver`), GPIO sources
(`INumberedGPIOOutput`) and SPI slaves (`ISpiPeripheral`) have to be C#. Getting
an external C# model in front of the `.repl` parser took four findings, all
measured against Renode 1.17.0:

1. `include @<assembly>` does **not** work — Renode tries to tokenise the DLL as
   a script (`Could not tokenize here: MZ…`).
2. `Assembly.LoadFrom` and `TypeManager.Scan`/`ScanFile` make the type appear in
   the "available peripherals" list but still not *resolvable* (`E04`).
3. The supported route is a **plugin class** carrying `[Plugin(...)]`
   (class-level only), with the DLL placed beside `renode.exe` and enabled via
   `[plugins] enabled-plugins` in the config. The runner generates that config
   and passes `--config`, so the Renode installation stays untouched.
4. Even as an enabled plugin, the type only resolves if its namespace is under
   one of Renode's own prefixes. Hence the models live in
   `Antmicro.Renode.Peripherals.FlipperEmu`.

Consequence: the display (SPI) and SD card models have a working, repeatable
build-and-load path, and small instrumentation peripherals are cheap to add.
`dotnet build` takes ~2 s incrementally against Renode's shipped
`Infrastructure.dll`.

## D8 — Finding firmware misbehaviour: instrument, then symbolise

Two dead ends were retired rather than repeated: `LogPeripheralAccess` produced
nothing useful for the reset hunt, and UART capture attaches successfully but
USART1 stays silent (the early diagnostics evidently go over USB CDC). The
approach that works with what is available: set a watchpoint on the register the
firmware writes when it misbehaves (`SystemBus.AddWatchpointHook` on `AIRCR` at
`0xE000ED0C` for resets), read the current PC inside the hook, and symbolise it
with the downloaded release ELF. The digest reports the *effect*; this reports
the *cause*.

