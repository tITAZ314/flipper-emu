# flipper-emu — a software-only Flipper Zero emulator

Runs **real, unmodified Flipper Zero firmware** — the official `.dfu` packages,
exactly as qFlipper flashes them — on a desktop PC. No hardware, no firmware
patches, no special build defines. VERY EARLY VERSION, MANY THINGS DONT WORK !!!


The emulated board is a STM32WB55RG described to [Renode](https://renode.io)
(MIT), with the peripherals of the real Flipper Zero on top: 128×64 ST7567
display over SPI, button GPIOs wired into EXTI, Sub-GHz (CC1101), NFC
(ST25R3916), RFID comparators, IR timers, microSD over SPI, and the CPU2/IPCC
mailbox.

---

## Status: measured, not aspirational

| Milestone | State |
|---|---|
| `.dfu` loader (DfuSe, including Flipper's variant layout) | **done** — 14 tests, verified byte-for-byte against the official 1.4.3 package |
| Flash image assembly + persistence across runs | **done** — 1 MiB image, dumped on exit, guarded so a failed run cannot destroy it |
| STM32WB55 platform description | **done** — 50 register blocks, 57 instances, loads cleanly in Renode 1.17 |
| Real firmware executes | **yes** — the application boots cold (this `.dfu` is app-only, no bootloader — see Next Steps), GPIO/HSEM models supply what the bootloader would normally provide, and Furi brings up RTC/interrupts/resources/SPI/iButton/speaker/crypto/I2C/power/BT, reaching "Boot mode 0, starting services" with **zero** crashes |
| Custom C# peripheral models | **yes** — `Wb55Exti`, `GpioWb55Port` (GPIO with a real input path), `HsemWb55` (hardware semaphores) and `St7567Display` load as a Renode plugin; recipe in `peripherals/cs/README.md` |
| Boots to the home screen | **reached** — the application runs, the GUI starts and the panel is drawn (Desktop idle animation; see `docs/BRINGUP_LOG.md` §18). Remaining errors are the absent chips: gauge, Sub-GHz, NFC and the CPU2/wireless stack |
| Button input | **done** — injected levels drive polled reads (boot-time board levels from `flipper_zero.resc`) and EXTI-driven presses come from the UI / monitor |
| Live window | **done** — `flipper_emu ui` (tkinter) tails the panel recorder and injects buttons; it shows exactly what the firmware has drawn (blank until the app gets past BT init) |

---

## The live emulator window (one command)

```powershell
$env:PYTHONPATH = "src"
py -3.9 -m flipper_emu ui
```

That single command starts Renode, boots the firmware from `artifacts\flash.img`,
waits for the first frame and opens a **768x384** window showing the emulated
128x64 panel, updated live (it tails the recorder file the panel model writes, so
what you see is the firmware's own drawing).

**Controls** (the window must have focus):

| Key | Flipper button |
|---|---|
| `↑` `↓` `←` `→` | Up / Down / Left / Right |
| `Enter` or `Space` | OK |
| `Esc` or `Backspace` | Back |

Keys are injected as the firmware's real GPIO levels over Renode's monitor (all
buttons are active-low except OK), so the firmware sees genuine button presses
with EXTI edges. Closing the window stops the emulator and saves the flash.

Useful variants:

| Command | What it does |
|---|---|
| `py -3.9 -m flipper_emu ui --scale 8` | larger window |
| `py -3.9 -m flipper_emu ui --attach` | attach to an emulator you started yourself |
| `py -3.9 -m flipper_emu ui --selftest 20` | headless check: tail for 20 s, inject OK, report frames and whether the panel changed |
| `py -3.9 -m flipper_emu ui --no-buttons` | render only (no monitor connection) |

## Quick start

```powershell
# from the repository root, with the sources on PYTHONPATH
$env:PYTHONPATH = "src"

powershell -File tools\fetch_renode.ps1     # pinned Renode 1.17.0, SHA256 verified
dotnet build -c Release -p:RenodeDir=tools\renode `
  src\flipper_emu\peripherals\cs\FlipperEmu.Peripherals.csproj   # C# models (EXTI, display, …)
py -3.9 -m flipper_emu load                 # firmware\*.dfu -> artifacts\flash.img
py -3.9 -m flipper_emu check                # load the generated platform in Renode
py -3.9 -m flipper_emu run --seconds 25     # boot it, then print the bring-up digest
py -3.9 -m flipper_emu bus                  # print the emulated address map
```

`run` starts Renode headless, captures everything it says, stops it, saves the
flash image back to disk and prints a digest of what the firmware touched, e.g.:

```
emulator log digest:
  unmapped accesses: 0 distinct address(es)
  registers the existing models do not implement: 25 distinct
    rcc       offset 0x58 read   25536x
    exti      offset 0x80 read   28728x
    gpioPortA offset 0x04 write  15960x
  platform resets: 3192, cpu starts: 3193  <-- BOOT LOOP
    SYSRESETREQ x3192
    last boot: PC=0x08011B9D SP=0x20030000
```

Point `RENODE_EXE` at a Renode you already have if you prefer not to vendor it.

---

## How it is put together

The project keeps four boundaries, so each layer can be changed or tested alone:

| Module | Responsibility |
|---|---|
| `flipper_emu.fw` | **Firmware loader**: DfuSe parsing, flash image assembly/persistence, the STM32WB55 address map (single source of truth) |
| `flipper_emu.platform` | **Bus/platform**: renders the Renode `.repl`/`.resc` from that address map |
| `flipper_emu.peripherals` | **Peripheral models**: IronPython 2.7 register models (`python/`) and C# bus devices (`cs/`, display + SD card) |
| `flipper_emu.console` | **Debug console**: turns a Renode log into the bring-up digest (unimplemented registers, unmapped accesses, boot loops, faults) |
| `flipper_emu.frontend` | **UI/rendering**: 128×64 framebuffer window with key→button mapping (`ui_tk.py`) |
| `flipper_emu.runner` / `cli.py` | Session orchestration and the command line |

Design rules that keep it honest:

* **One address map.** `fw/memmap.py` holds every region and register block; the
  platform file is *generated* from it, so the emulated bus and the map cannot
  drift apart. `py -3.9 -m flipper_emu check` fails loudly if the platform is
  rejected, and `tests/test_memmap_dispatch.py` asserts no overlaps, valid
  registration sizes, and that every block is either modelled or explicitly
  stubbed.
* **Stubs are explicit.** A register block with no model yet is declared as a
  `Python.PythonPeripheral` returning safe defaults, tagged in the platform, and
  listed in the digest — "not implemented yet" is a first-class state rather
  than a silent hole.
* **The firmware owns the flash.** The image is loaded at start-up and written
  back on exit, so settings and OTA behaviour survive runs, like the real chip.

---


## Supported firmware and files

* `firmware/*.dfu` — official stock, Momentum and RogueMaster packages all use
  the same DfuSe container. The 1.4.3 release is what the tests are pinned to.
* The application element is placed at `0x08000000`; the region above
  `0x080C0000` (256 KiB) is the wireless-stack/FUS reservation and is left alone.
* The optional `-f7-firmware-*.elf` from the same release is useful for
  symbolising traces (`arm-none-eabi-addr2line`) — not required.

## Known limitations

* **Radio is stubbed, not simulated.** CC1101/ST25R3916 register traffic is
  answered so drivers initialise; there is no RF.
* **BLE Core2 is stubbed.** The IPCC mailbox acknowledges channels instantly
  (`peripherals/python/ipcc_wb55.py`), but the payload the firmware reads from the
  shared SRAM area (FUS/stack version) is not synthesised.
* **The animation-timer wakeup path is still being finalised.** The firmware
  boots to the home screen and draws correctly, but its idle-animation timer
  interrupt isn't fully wired yet, so playback can stall after the first few
  frames; see `docs/BRINGUP_LOG.md` for the current state.
* **UART capture is wired up but silent**: the firmware's early diagnostics appear
  to go over USB CDC, which is a stub, so nothing reaches USART1 yet.
* **No Python `pip` dependencies.** Host tooling is standard-library only, and
  peripheral models must stay IronPython-2.7-compatible (Renode embeds
  IronPython 2.7.12).
* **Flash persistence is a 1 MiB dump on exit**, not a live file-backed region.
* Windows is the verified platform (Renode's own Windows build); nothing in the
  host tooling is Windows-specific.

## Next steps

1. Finish wiring the animation-timer interrupt path so idle-animation playback
   no longer stalls after the first few frames.
2. Run the real Flipper bootloader instead of standing in for its one measured
   side effect (the CLK48 hardware-semaphore handover) — this is what turns
   "this firmware build works" into "any Flipper firmware build works".
3. SD card and external SPI flash (W25Q64) emulation.
4. Verify a second, independently-built firmware version boots with no manual
   tuning, as the real test of firmware-agnostic support.
   
---

> **Development note:** built with heavy use of AI coding assistance
> (Deepseek V4.1 / Claude) for implementation, debugging, and documentation,
> under my direction and review throughout. The engineering approach and
> every decision were mine; AI was a tool in the process. Disclosed here
> for transparency.

## License

GPL-3.0 — see `LICENSE` for the full text. Contributions and forks are welcome
under the same terms.
