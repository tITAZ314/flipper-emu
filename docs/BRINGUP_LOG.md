# Bring-up log

Chronological record of taking real, unmodified firmware from `.dfu` to
execution. Every number here was measured on this machine; commands are given so
each step can be reproduced.

## 0. Firmware facts established up front

Parsing the official `flipper-z-f7-full-1.4.3.dfu` (768,441 bytes, SHA256
matching Flipper's own release manifest):

```
DfuSe version=1 declared_size=768425 data_end=768425 target_bytes=768414 layout=flipper
target 0 'Flipper Zero F7' declared_size=768140 elements=1
  element 0: 0x08000000..0x080BB884 (768132 bytes)
suffix vid=0x0483 pid=0xDF11 crc=0x7A619838 crc_ok=None
SP=0x20030000 reset=0x08011B9D thumb_bit=1
```

Conclusions that shaped the emulator:

* the container is DfuSe **with Flipper's variant header** (11 extra bytes before
  the 255-byte target name, 3 padding bytes before the element records); a
  classic DfuSe layout is also supported as a fallback;
* `dwImageSize` counts everything up to the DFU suffix, header included;
* the application occupies 768 KiB at `0x08000000`, so the top 256 KiB is the
  wireless-stack reservation — the flash image is 1 MiB with the app at offset 0;
* the suffix CRC does **not** match any standard convention (five variants were
  tried), so integrity comes from the release manifest's SHA256 instead.

## 1. Platform bring-up (Renode 1.17.0, Windows)

Iterations, each fixed from the emulator's own error message:

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `Error E00: unexpected '['; expected attribute list end` | range connections need bracketed destination indices | `[0] -> nvic@[6]` |
| 2 | `Error E25: no suitable constructor for 'SPI.STM32SPI'` | `bufferCapacity` does not exist in this build; `STM32Series.L4` is not a valid member | `series: STM32Series.F4` only |
| 3 | `Error E21: no suitable constructor for this registration point` | `GPIOPort.STM32_GPIOPort` cannot be registered with a bare address | `@ sysbus <0x48000000, +0x400>` |
| 4 | `Error E25` for `MTD.STM32L0_FlashController` | the model requires an `eeprom` binding | bound to the option-bytes region (correct for WB55, whose flash register layout does match L0) |

Result: **platform loads cleanly, 56 peripheral instances**.

## 2. First execution — CPU started at zero

```
[INFO] cpu: Setting initial values: PC = 0x0, SP = 0x0.
[WARNING] sysbus: [cpu: 0x0] ReadDoubleWord from non existing peripheral at 0x4.
```

An STM32 boots with flash aliased to address 0, and that is where the Cortex-M
model fetches SP/PC. Fix: register the flash memory at **both** addresses with a
multi-registration, which keeps it one memory with two mappings:

```
flash: Memory.MappedMemory @ {
    sysbus 0x00000000;
    sysbus 0x08000000
    }
    size: 0x100000
```

Verified: a word written at `0x08000000` reads back at `0x00000000`, and the CPU
then starts with `PC = 0x8011B9D, SP = 0x20030000` from the real vector table.

## 3. Firmware executes (and resets itself)

With the boot path fixed, a 25 s session produced an 18.8 MB log / 188,352 lines:
the firmware initialises clocks, probes I2C, configures EXTI/NVIC and then asks
the NVIC for a platform reset.

```
unmapped accesses: 0 distinct
registers the existing models do not implement: 25 distinct
  exti      offset 0x80 read   28728x
  rcc       offset 0x58 read   25536x     <- RCC_APB1ENR1 on WB55
  gpioPortA offset 0x04 write  15960x
platform resets: 3192, cpu starts: 3193  <-- BOOT LOOP
  SYSRESETREQ x3192
```

Diagnosis: Renode's `Miscellaneous.STM32L0_RCC` has the *L0* register map, so the
firmware's read-modify-write on the WB55 clock-enable registers read back as
zero, its own consistency check failed, and it called `NVIC_SystemReset()` —
which the emulator faithfully turned into a reboot.

Fix: `peripherals/python/rcc_wb55.py`, a WB55 RCC register file with instant
clock-ready semantics (`HSION→HSI16RDY`, `HSEON→HSERDY`, `PLLON→PLLRDY`,
`SW→SWS`), reset-flag clearing in `CSR`, and pulse semantics for the reset
registers.

Result — the unimplemented-register list dropped from 25 to 3:

```
registers the existing models do not implement: 3 distinct
  exti      offset 0x80 read   19130x
  gpioPortA offset 0x04 write  11478x
  nvic      offset 0xDFC write   3826x
platform resets: 3825, cpu starts: 3826  <-- BOOT LOOP
```

## 4. Current blocker (next step)

* `nvic offset 0xDFC` (`DEMCR.TRCENA`) — written once per boot before the
  firmware enables the DWT counter; benign.
* `gpioPortA offset 0x04` (`OTYPER`) — open-drain, unmodelled, irrelevant here;
  benign noise.
* **`exti offset 0x80` — the live suspect.** The WB55 EXTI has `IMR1` at `0x80`
  and `RTSR1/FTSR1/SWIER1/PR1` at `0x00/0x04/0x08/0x14`, while Renode's
  `IRQControllers.STM32F4_EXTI` places *pending banks* at `0x80` and its
  interrupt-mask register at `0x00`. The firmware therefore cannot actually
  enable an EXTI line, and its level/wakeup path plausibly times out into the
  reset we see.

Because GPIO→EXTI connections require a real `IGPIOReceiver`, the EXTI
replacement has to be a **C# model** (`peripherals/cs/`). That path is
de-risked: `dotnet` builds a `net8.0` library against Renode 1.17's
`Infrastructure.dll` in ~9 s on this machine, which is how `ST7567` (display),
the WB55 EXTI and the SPI SD card are meant to be written.

## Reproducing any step

```powershell
$env:PYTHONPATH = "src"
py -3.9 -m flipper_emu check                 # platform loads? (seconds)
py -3.9 -m flipper_emu run --seconds 25      # boot + digest
py -3.9 -m flipper_emu report artifacts\renode-console.log
```

The flash image is written back on exit only after the vector table is validated,
so a failed run can never destroy a working image.
## 5. UART capture and the IPCC hypothesis (latest)

Two further experiments, both recorded so they are not repeated:

* **UART capture works, but the firmware is silent on USART1.** The runner
  attaches to the model's `CharReceived` event (`UART_CAPTURE_ATTACHED usart1`
  appears in the log) yet not a single byte arrives before the reset. The early
  diagnostics therefore go out over the USB CDC path, which is still a stub —
  so capturing them needs either a CDC implementation or symbolising the reset
  call site instead.
* **The CPU2 mailbox handshake was implemented** (`peripherals/python/ipcc_wb55.py`:
  every channel CPU1 sets in `C1TOC2SR` is acknowledged in `C2TOC1SR`, with
  clear semantics). It did **not** change the reset count (4,081 in 20 s before,
  4,081 after), which rules the mailbox handshake out as the immediate cause.

Current best suspect, unchanged: the **EXTI register-layout mismatch**
## 6. WB55 EXTI implemented — and what it did (and did not) fix

Step 1 of the agreed plan was to write the WB55 EXTI model to clear the boot loop.

**Loading a custom C# peripheral** turned out to be the hard part, and it is now
solved (documented in `src/flipper_emu/peripherals/cs/README.md`): the assembly
needs a `[Plugin]`-annotated *class*, must sit beside `renode.exe`, must be
enabled through the config's `[plugins] enabled-plugins`, and — the non-obvious
part — the peripheral type must live under an `Antmicro.Renode.*` namespace,
because Renode's type manager only records types under its own namespace
prefixes. Without that last point the type appears in the "available
peripherals" list but is never resolvable by a `.repl`.

**Result of the model itself:**

```
py -3.9 -m flipper_emu check
  load result: ok
  peripherals instantiated: 56          (… ├── exti (Wb55Exti) …)

py -3.9 -m flipper_emu run --seconds 20
  registers the existing models do not implement: 2 distinct
    gpioPortA offset 0x04 write  14118x     (OTYPER, benign)
    nvic      offset 0xDFC write   4706x     (DEMCR, benign)
  platform resets: 4706, cpu starts: 4707  <-- BOOT LOOP
```

So: the unimplemented-register list went **25 → 3 → 2**, all 20k-per-session EXTI
warnings are gone, and the EXTI block is now genuinely faithful (verified by the
register map from `stm32wb55xx.h`). **The reset loop did not go away**, so the
EXTI layout was *not* the cause of it — it was a real fidelity gap (and is still
required for buttons), just not this symptom.

## 7. Instrumentation for finding the reset site

What is known: the firmware, not the emulator, requests the reset — every reset
logs `nvic: Resetting platform with SYSRESETREQ` (a software reset), roughly
every 4–6 ms of host time, after only the earliest init (debug setup, GPIO
configuration, EXTI reads, and no UART bytes on USART1 before resetting).

Tools now in place to pinpoint it:

* the release **ELF** (`firmware/flipper-z-f7-firmware-1.4.3.elf`, 9.5 MB) for
  symbolising any address;
* `SystemBus.AddWatchpointHook(address, width, access, BusHookDelegate)` — a
  watchpoint on `AIRCR` (`0xE000ED0C`) reaches the reset *before* it happens, and
  the hook can read the current CPU's PC, which identifies the caller of
  `NVIC_SystemReset()` in the firmware;
* the C# plugin route means small instrumentation peripherals are now cheap.

Next candidates, in order of likelihood: (a) the reset call site's symbol from
the watchpoint PC, (b) the option-bytes region, which is currently plain zeroed
RAM at `0x1FFF8000` (a real device reports `FLASH_OPTR = 0x3FFFF1AA`, exactly the
value Flipper's own tooling writes in `option_bytes_recover`), and (c) the
FUS/wireless-stack presence checks around the IPCC mailbox.

(`IMR1` at `0x80` on the WB55 versus pending banks at `0x80` in Renode's
`STM32F4_EXTI`). Solving it means a C# `IGPIOReceiver` EXTI model in
`peripherals/cs/`, which is the next milestone.

*Resolved afterwards*: `peripherals/cs/Wb55Exti.cs` was written (recorded in §6),
two separate root causes turned up in §8 (RCC reset registers) and §9 (RTC LSE/LSI
ready bits), and the reset loop is gone.

## 8. Root cause of the reset loop — found (evidence chain)

Instrumentation that worked: a watchpoint on `AIRCR` (`0xE000ED0C`) via
`SystemBus.AddWatchpointHook` from monitor Python. The hook receives the CPU, so
the crash site can be read directly, and the firmware's own saved registers and
stack give a backtrace. Addresses were resolved with
`flipper_emu.console.symbols` (a dependency-free ELF symbol reader) against
`firmware/flipper-z-f7-firmware-1.4.3.elf`.

**What the hardware saw**

```
AIRCR_HIT value=0x05FA0004        <- VECTKEY + SYSRESETREQ: NVIC_SystemReset()
AIRCR_PC=0x08009FEC LR=0x08012963 SP=0x2002FF88
STACK[09] 0x0800323B  STACK[21] 0x08009A7D  STACK[25] 0x08002CB7
STACK[27] 0x0801195F  STACK[29] 0x08011BD1
```

**Symbolised backtrace**

| Address | Symbol |
|---|---|
| `0x08009FEC` | `furi_hal_power_reset+0xF` — the `AIRCR` store |
| `0x08012962` | `__furi_crash_implementation+0x171` — the *caller* |
| `0x0800323A` | **`furi_hal_bus_enable+0x11`** — the call site that failed a check |
| `0x08009A7C` | `furi_hal_os_init+0x7` |
| `0x08002CB6` | `furi_hal_init_early+0x19` |
| `0x0801195E` | `main+0x9` |
| `0x08011BD0` | `Reset_Handler+0x33` |

So the chain is: `Reset_Handler → main → furi_hal_init_early → furi_hal_os_init
→ furi_hal_bus_enable → furi_check failed → __furi_crash_implementation →
furi_hal_power_reset → NVIC_SystemReset` — a **deliberate crash on a failed
assert**, not a CPU fault and not a hang. `__furi_check_message` confirmed it:
the message pointer reads **"furi_check failed"**.

**Why the check fails** (from the firmware source at tag 1.4.3,
`targets/f7/furi_hal/furi_hal_bus.c`): `furi_hal_bus_enable()` validates the RCC
register state with asserts, e.g.

```c
} else if(bus < FuriHalBusAPB1_GRP2) {
    furi_check(FURI_HAL_BUS_IS_PERIPH_DISABLED(APB1, value, 1));
    FURI_HAL_BUS_PERIPH_ENABLE(APB1, value, 1);
...
} else {
    furi_check(FURI_HAL_BUS_IS_RESET_ASSERTED(APB3, value));
    FURI_HAL_BUS_RESET_DEASSERT(APB3, FURI_HAL_BUS_APB3_GRP1, 1);
}
```

i.e. the firmware asserts invariants about the *clock-enable* and *reset*
registers it is about to touch. Our `rcc_wb55.py` cannot satisfy at least one of
them:

* its reset registers (`AHB1RSTR 0x28`, `AHB2RSTR 0x2C`, `AHB3RSTR 0x30`,
  `APB1RSTR1 0x38`, `APB1RSTR2 0x3C`) are implemented as *pulses* that read back
  as zero, whereas on silicon they are read/write — the bit stays set until
  software clears it — which breaks
  `furi_check(FURI_HAL_BUS_IS_RESET_ASSERTED(...))`;
* `APB3RSTR` (`0x44`) is not listed at all;
* the register file is never cleared when the platform resets (the Python stub
  ignores the `IsInit` request), so state leaks from one boot attempt into the
  next.

**Measured at the first reset** (`RESET_1 CR=0x00000001 APB1ENR1=0x00000000
APB1RSTR1=0x00000000`): the enable register was still clean, so the first failure
is a check that expects a register to be *set* (the reset-asserted invariant
fits), while the later thousands of resets are consistent with state leaking
across resets making the `..._IS_PERIPH_DISABLED` checks fail as well.

**Ruled out, with evidence**

* **`FLASH_OPTR` option bytes**: the failing path is the bus/clock-enable
  assert, not flash option validation. The option-bytes region being zeroed RAM
  is still wrong for other reasons, but it is not this loop.
* **EXTI layout**: fixed by `Wb55Exti.cs` with no change in reset count.
* **Clock-ready semantics** (`HSION→HSI16RDY`, …): implemented, and the digest no
  longer reports any RCC register access as unimplemented.

**Measurement caveat**: the `AIRCR` watchpoint only fired once per session (the
NVIC is re-created on platform reset, which drops the hook), so the single sample
above is from reset #1; subsequent resets were counted from the emulator log.
Reinstalling the hook from a machine-state hook would give per-reset samples.


## 9. RTC clock bring-up — root cause #2, and the loop is gone

With the RCC reset-register fix in place the resets fell from 4706 in 16 s to
**7 in 16 s**, and each attempt now runs ~2 s of host time instead of 4 ms. The
re-armed `AIRCR` watchpoint (re-armed from `Machine.StateChanged`, because the
NVIC is re-created on reset) gave 8 identical samples:

```
RESET_1 PC=0x0800C352 LR=0x0800C343 SP=0x2002FFD8
  APB1RSTR1=0x00000001  APB3RSTR=0x00000001
  RCC_BDCR=0x00000019   RCC_CSR=0x00000000  PWR_CR1=0x00000000
  STACK[05] 0x08002CC7  STACK[07] 0x0801195F  STACK[09] 0x08011BD1
```

Symbolised: `Reset_Handler → main → furi_hal_init_early+0x29 →
furi_hal_rtc_init_early+0x89/+0x99`.

The source at tag 1.4.3 (`targets/f7/furi_hal/furi_hal_rtc.c`) explains the
values exactly. `furi_hal_rtc_init_early()` calls
`furi_hal_rtc_start_clock_and_switch()`, which enables LSE and LSI and then
**waits for them to become ready**; if they never do it calls
`furi_hal_rtc_recover()`, whose "Plan C" is literally:

```c
if(!furi_hal_rtc_start_clock_and_switch()) {
    furi_hal_light_sequence("rgb R.r.R.r.R.r");
    furi_hal_rtc_reset();
    NVIC_SystemReset();
}
```

`RCC_BDCR = 0x00000019` shows `LSEON` set with **`LSERDY` clear** — our model
never reported the oscillator ready, so the firmware reset the chip by its own
logic (not even an assert this time).

Fixes (all in `peripherals/python/`):

* `rcc_wb55.py`: `BDCR.LSEON|LSEBYP → LSERDY`, `CSR.LSION → LSIRDY` and
  `CRRCR.HSI48ON → HSI48RDY` are now reflected on read, so the oscillator-ready
  polls complete;
* `rcc_wb55.py`: `CSR` write semantics corrected — it is not a pure
  write-1-to-clear register: `LSION` (bit 0) is a control bit that must stick,
  while only the reset flags (bits 24-31) clear;
* `pwr_wb55.py` (new model): the register file round-trips writes, so
  `LL_PWR_EnableBkUpAccess()` (PWR_CR1.DBP) and the `PWR_CR4.C2BOOT` request are
  visible instead of being answered with zeros. (The previous PWR "model" was an
  inline stub that always returned 0.)

**Result**

```
platform resets: 0, cpu starts: 1
RCC_BDCR = 0x0000001B   (LSEON | LSERDY | LSEDRV)
RCC_CSR  = 0x00000003   (LSION | LSIRDY)
```

The firmware now runs continuously and has moved on to SPI2 and I2C activity.
Remaining findings from the same run (next work, in order):

1. `emulator model errors: 10` — these turned out **not** to be our peripheral
   stubs: the probe's own teardown called `RemoveAllWatchpointHooks` after the
   machine was disposed, which walks into native tlib code and takes a
   `System.AccessViolationException`. The probe no longer removes hooks, and
   `reset_probe.py` carries a comment so this trap is not re-entered. (Our
   Python stubs were clean in this run.)
2. `spi2 offset 0x04 write 44310x` — Renode's `STM32SPI` reports an unimplemented
   `CR2` write, and the volume suggests a retry loop (SD card / radio handshake).
3. `i2c1 offset 0x10 write 7389x` — `TIMINGR` is not implemented by Renode's
   `STM32F7_I2C`, and the count reflects the battery-gauge probe retrying; our
   DWT fast-forward makes those retry delays instantaneous, so the loop is
   thousands of times faster than on hardware.


## 10. SPI2 and I2C1: instrumented findings (no fixes needed)

Both "retry loops" in the digest turned out to be **normal firmware behaviour**,
identified by hooking the peripherals and symbolising the callers (same method as
the reset loop).

### SPI2 is the LCD, and it is drawing

* Every byte goes out at `PC=0x0800E97C` = `furi_hal_spi_bus_tx+0x49`, called from
  `LR=0x080653F5` = **`u8x8_hw_spi_stm32+0x27`** — u8g2's STM32 SPI backend. The
  SPI is configured by `LL_SPI_Init` (`CR1=0x31C` → `0x35C`, `CR2=0x700`
  (DS=8-bit) `| 0x1000` (FRXTH)).
* The byte stream is an **ST7567 init sequence followed by frame data**:
  `E2 A2 A0 C8` (reset, bias 1/9, ADC normal, COM reverse), `40`, `25`,
  `81 20` (contrast), `2F`, `A4`, `AF`, then `10 00`/`B0` (column/page) with 132
  bytes of pixel data per page. Later samples carry varied data and page/column
  commands (`0xB2`, `0x40`, `0x78`, `0x48`, `0x08`), i.e. **the GUI redrawing**.
* Status behaves correctly: `SR` reads back `0x02` (TXE) → `0x03` (TXE|RXNE).
* The register layout question is settled by ST's own CMSIS header: **WB55 SPI is
  the v1 layout** (`CR1 0x00, CR2 0x04, SR 0x08, DR 0x0C`), so the platform's
  `series: STM32Series.F4` choice is correct. Renode's model only warns about
  `CR2` bits `DS[8-10]` and `FRXTH` (cosmetic here: data size is 8-bit anyway).
* What is actually missing is a **slave**: the model warns *"SPI transmission
  while no SPI peripheral is connected"* because we have no ST7567 attached. The
  firmware does not care (write-only path) and keeps running.

### I2C1 is the light/power bus being acquired without a transaction

* Callers: `LR=0x08005FA9` = `furi_hal_i2c_bus_handle_power_event+0x50`, and the
  stack shows `furi_hal_light_init+0x9` and `furi_hal_i2c_handle_external+0x7`
  below it — i.e. the **LED driver (LP5562) acquiring the external I2C bus**.
* The full ordered trace of the bus shows **only** init/deinit: `CR1=0`,
  `TIMINGR=0x10707DBC`, `CR1=PE`, `OAR1=OAR2=0`, `CR2=0x02000000` (AUTOEND),
  then `CR1=0` again. Across 260 sampled accesses there is **zero** activity on
  `ISR` (0x18), `TXDR` (0x28) or `RXDR` (0x24) and no `START`/address in `CR2`:
  the driver never performs a transfer, because the LP5562 is not there.
* So this is an *absent device*, not a register-semantics problem, and it is not
  blocking: the same run shows the GUI drawing.

### What this means for the next step

The display path is the one that matters for the home-screen goal, and it is now
**specified by measured traffic** rather than guesswork: implementing
`St7567Display.cs` (SPI slave, decodes the command/data stream, exposes the
128×64 framebuffer) turns "the firmware is drawing" into "we can show the frame".
The I2C side only needs an absent-device answer (LP5562 / bq27220) so the driver
short-circuits instead of polling — lower priority, since it no longer blocks.

Note on run-to-run behaviour: because the emulated flash persists firmware writes
(by design), successive runs are not identical — an early run showed the display
re-initialising thousands of times per second, while a later one showed steady
GUI drawing with modest polling. That is what a real device does too (first boot
vs subsequent boots), and it is worth remembering when comparing digests.


## 11. The display: ST7567 modelled, and the first decoded frame

With the boot loop gone, SPI2 turned out to be the panel bus (§10), so the model
was written against the captured stream rather than guessed:

* `peripherals/cs/St7567Display.cs` - an `ISPIPeripheral` (the same interface set
  as Renode's `ENC28J60`) attached to `spi2` in the generated platform. It captures
  every byte with the A0 state (the A0/RESET lines can be wired later; without them
  bytes are tagged "A0 unknown") and appends it to a record file:
  `[tag][payload]` pairs after a `FZDPI1\n` header.
* `frontend/st7567.py` - the decoder: all interpretation lives on the host side so
  it is unit-testable, and it handles both exact A0 tags and inference from the
  ST7567 command set (parameter bytes after `0x81`, page-select commands opening
  u8g2's 132-byte data runs).
* `tests/test_st7567_decoder.py` - 15 tests, including the real captured init
  sequence, framing/inference edge cases (a data byte that looks like a command
  inside a run), display state (on/off, invert, all-points-on) and PNG output.

**First real capture** (14 s boot, 1,193,683 bytes = 596,838 events): the decoder
reported 119 distinct frames, with the richest at 335,000 events (`ink=1198` of
8192 pixels). Frames are partial updates, which is what the firmware sends - the
panel is drawn page by page, and the first pages carry the slideshow/boot art.

A trap worth remembering: `Renode resolves relative paths against its own root`,
not the caller's directory, so the recorder initially wrote into the Renode
installation. Attached devices now get an absolute output path from the generator
(`{artifacts}` is expanded at generation time).

Verification of the whole chain in one run:

```
spi2 (STM32SPI)
└── st7567 (St7567Display)      <- attached, no more "no SPI peripheral" warnings
ST7567 byte #0 = 0xE2 ... #6 = 0x81 (the panel init sequence, from the model)
decoded frames: 119 -> artifacts/frame-*.png (512x256 at scale 4)
```


## 12. Live window: `flipper_emu ui`

The UI is deliberately host-side and dumb about the emulator:

* `frontend/st7567.py` decodes the recorder stream incrementally;
* `frontend/ui_tk.py` tails that file (`DisplayStreamTail`), re-encodes the
  framebuffer as a 1:1 PNG whenever it changes, and lets Tk scale it 6x - so a
  frame costs a few milliseconds and the window keeps up;
* `frontend/keymap.py` maps keys to the firmware's button GPIO lines and
  `console/monitor.py` sends them over Renode's TCP monitor;
* `runner.start_session()` starts Renode without `--console` (the two are mutually
  exclusive, which is what the button path needs) and `Session.stop()` drives
  pause/dump/quit over the monitor, so the flash is still saved on exit.

End-to-end self-test, run headless through the real stack (no window):

```
emulator started (monitor port 3504)
selftest: injected OK at 269 frames, 761600 events
selftest: events=2219776 frames=806 resyncs=1
OK: display decoded live and buttons reach the firmware
emulator stopped, flash saved
```

That is the whole chain proven in one run: 2.2 million display events decoded
live, 806 distinct frames rendered, and an injected OK press reaching the firmware
and changing the panel (it opened the menu). The same run also confirms the window
itself can be built in this environment, and decoding the head of that capture
renders the firmware's own splash text (`artifacts/live-frame.png`, 610 lit pixels
of 8192):

```
events=1048572 commands=640 data=751
display_on=True inverted=False contrast=32
lit pixels=610 of 8192
00 .......#..........##.......##............###.......#####...........
02 .......#.#.##...####..###.####..###.....##.##......##..##..###...###
07 #######..####...####..##.##.##..###......###.##....##..##..###...###
16 ..##..##.####..##..##.....####..##..#..##.###.##..##.##.##.##......
20 ..####...##.....####......####...##..##.####...##..###...####......
```

Notes from making it live:

* the firmware redraws much faster than wall-clock time because the DWT stub makes
  its delays instant, so the stream can outrun a host decoder; the tail therefore
  keeps a decoding budget per poll (256 KB) and, when it falls far behind
  (> 512 KB), jumps to the newest records - page/column commands arrive
  constantly, so the image heals within a frame. `--selftest` prints the resync
  count so this stays measurable (one resync in the run above, then 537 further
  frames decoded).
* a resync has to be aligned to the *record grid*, and the recorder header is
  seven bytes - aligning to an even file offset (the obvious `& ~1`) misparses
  every following record. `tests/test_display_tail.py` pins this down.
* Renode resolves relative paths against its own root, and it can only have one
  *writing* recorder per stream file - so run one session at a time (stale
  emulator processes from earlier experiments will keep writing to the same
  `artifacts/display-stream.bin`).
* `--console` and `-P <port>` are mutually exclusive in Renode: a session that
  needs the TCP monitor (buttons) must not pass `--console`, and its shutdown
  commands therefore go over the monitor instead of stdin.



## 13. What the firmware draws today, and the next visible step

Decoding the live stream shows the firmware has booted into its own update path:
the panel reads

```
Update & Recovery Mode
      DFU Started
```

(`artifacts/live-frame.png`, 610 lit pixels of 8192, decoded from a 2 MB slice of
a live capture). The reason is a property of the *hardware we present*, not of the
display path:

* the 1.4.3 package is a DFU with **one** element - `0x08000000..0x080BB884`, the
  application - so the release expects an existing bootloader to have written the
  bootloader-version struct the firmware reads back at boot (`0x0000B00B`: present
  in the ELF's debug data, absent from our flashed image);
* `spi1` (the external SPI flash) has no device attached, and there is no SD card.

So the firmware concludes it is in update mode and shows its recovery screen -
which is correct behaviour for this hardware. Next visible steps, cheapest first:

> **Update:** section 14 measured all of these. The screen is *not* caused by the
> missing bootloader struct, the option bytes, the missing external flash/SD, or the
> RTC boot-mode register - mapping the RTC window and driving the SD card-detect pin
> changed nothing. It is the bootloader's **polled button levels**: it reads LEFT and
> UP from `GPIO IDR`, and this Renode build returns 0 for input pins no matter what
> is injected, so the bootloader always takes its DFU path. Read section 14 before
> working through the list below.

1. write the bootloader-version struct into `artifacts/flash.img` (the address the
   firmware reads is reachable from the `0xB00B0000` code site in the ELF, near
   file offset `0x089D8E`), and/or
2. attach a W25Q64 model to `spi1` so the storage checks pass, and/or
3. wire `A0`/`RESET` to the panel model (`gpioConnections` in the generated
   platform, panel inputs 0/1 as documented in `St7567Display.cs`). Today every
   captured byte arrives tagged `0x05` ("A0 unknown") - 100% of records in a
   5.5 MB / 30 s capture - which is exactly the case the inference in
   `frontend/st7567.py` exists for; it reproduces the firmware's screens reliably,
   and wiring the line would make the framing exact instead of inferred.

## 14. Why the firmware showed the DFU splash, and what is left

The 1.4.3 release DFU is the **full** image (`flipper-z-f7-full-1.4.3.dfu`: one element,
`0x08000000..0x080BB884`), i.e. the bootloader and the firmware in one binary with the
vector table at `0x08000000`. So the code that runs first is the *bootloader*, and it
decides between four paths. Its `main` (0x08011954) decodes, by hand, to:

```
r0 = furi_hal_rtc_get_boot_mode();          // BKP1R bits 19:16, base 0x40002000 + 0x54
if (r0 == 1) goto show_dfu_splash;          // CMP #1 / BEQ
if (!(GPIOC->IDR & 0x0800)) goto show_dfu_splash;   // that mask is gpio_button_left (PB11)
if (r0 != 3) goto recovery_check;           // CMP #3 / BNE
... update path (flipper_boot_update_exec) ...
goto power_shutdown;

show_dfu_splash:
    blink(); furi_hal_rtc_...; flipper_boot_dfu_show_splash();   // <- what we see
    furi_hal_power_shutdown();

recovery_check:
    if (!(GPIOB->IDR & 0x0400)) { draw recovery splash; power_shutdown(); }  // gpio_button_up (PB10)
    ...
    furi_init();                             // the firmware itself
    BLX 0x08011941                           // jumped into the application
```

The pin masks are not guesses: they are the literals the code loads (`0x080A2610` =
`gpio_button_left`, `0x080A2628` = `gpio_button_up`, read out of the ELF's own `GpioPin`
structs, exactly like the display and SD pins in §13). In other words: **hold LEFT at
power-on and the Flipper boots into DFU; hold UP and it boots into recovery** - and both
read low on a bare emulated board, so the bootloader took the DFU path every time.

Measured on the live session (`artifacts/gpioprobe3.out`, `artifacts/idrprobe.out`):

| Probe | Result |
|---|---|
| `GPIOB IDR` | `0x00000003` - only the two *output* pins (display A0/RESET) show; every input bit reads 0 |
| `GPIOB PUPDR` for PB10..PB12 | `0b01` (pull-up) configured by the firmware, and still IDR = 0: pull-ups are not modelled |
| `gpioPortB OnGPIO 11 true`, then read IDR | **unchanged (0)** - the monitor's GPIO input method does not feed IDR in this Renode build |
| write `GPIOB IDR = 0x1C00`, read back | `0x00000003` - IDR is read-only for input pins |
| write `GPIOB ODR = 0x1C00`, read back | unchanged - ODR only appears in IDR for pins in output mode |
| GPIOC IDR bit 11 (display CS, an output) | set - confirming IDR mirrors ODR for outputs only |

So the buttons are *not* the problem in the small: the UI's button presses still work,
because `OnGPIO` raises the numbered output and the firmware has EXTI interrupt handlers
for its buttons - that is the path the interactive `ui` self-test exercises. What does not
work is a *polled level*: the bootloader reads `IDR`, and `IDR` cannot currently be driven.

Hypotheses that this work measured and **disproved** (all are recorded here so they are not
re-tried): the bootloader-version struct (`0x0000B00B`) missing from the image, the erased
option bytes, the absent external SPI flash / SD card, and the RTC boot-mode-plus-flags
register. Mapping the firmware's real RTC window (`0x40002000`, §13) and driving the SD
card-detect pin (PC10, active low) each changed nothing, and an RTC read watchpoint never
fired because Renode's watchpoint hooks do not see peripheral register reads.

*Corrected in §15*: that probe never armed at all — `Access.Execute` does not exist on
Renode 1.17.0, so the hook failed to register and "never fired" described nothing. Read
and write watchpoints do work, and the working instrumentation is recorded in §15.

**What is left, and it is one piece of work:** the platform needs GPIO ports whose input
path is real - a `GPIOPort` model (C# plugin, following the display/EXTI pattern) that
implements `MODER/OTYPER/OSPEEDR/PUPDR/IDR/ODR/BSRR/LCKR/AFRL/AFRH`, applies pull-up/down
to idle inputs, feeds `IDR` from injected levels (so `OnGPIO` and the monitor work), and
still raises the numbered outputs the EXTI model consumes. The startup levels the
bootloader needs are already generated into `platform/flipper_zero.resc` (all buttons
released, SD detect high) - they simply have nowhere to land today. With that model in
place the boot code reads "no button held", `r0 != 1`, the update path finds no card, and
control reaches `furi_init()` and the `BLX` into the application.

## Verification snapshot

```
py -3.9 -m unittest discover -s tests   ->  Ran 36 tests ... OK
py -3.9 -m flipper_emu check            ->  load result: ok, 56 peripherals instantiated
py -3.9 -m flipper_emu run --seconds 20 ->  flash dump on exit: yes
                                            platform resets: 4081  <-- BOOT LOOP
```

## 15. The GPIO input path: the bootloader now starts the application

§14 ended with one piece of work: a GPIO port whose *input* path is real. That is now
`peripherals/cs/GpioWb55Port.cs` (a C# plugin, same recipe as the EXTI and panel models),
referenced by `fw/memmap.py` (`GPIO_PORT_MODEL`) for all six ports, with `portName:
"A".."H"` properties so its log lines read like the pin names in the firmware. Register map
from `stm32wb55xx.h`: `MODER 0x00 OTYPER 0x04 OSPEEDR 0x08 PUPDR 0x0C IDR 0x10 ODR 0x14
BSRR 0x18 LCKR 0x1C AFRL 0x20 AFRH 0x24 BRR 0x28`.

What an input pin reads, in priority order: an output/alternate-function pin reads back
`ODR`; an input pin reads the injected board level (`OnGPIO` - the monitor command, the
UI's buttons, the `.resc` levels) when the board drives it; otherwise it reads the idle
level `PUPDR` selects (pull-up high, pull-down low, floating 0 - the floating case being a
documented deviation). Injected levels deliberately survive `Reset()`: they are board
wiring, not peripheral state, which keeps the boot code's polled reads consistent across
the firmware's own reboots. Each pin still drives a numbered output, written only when its
level changes, so the existing `[0-15] -> exti@[0-15]` wiring and the EXTI edge detector
keep working unchanged.

Measured, from the model's own first-read log line:

```
gpioPortB: GPIOB first IDR read: 0x1C00 (MODER=0x800000F5 PUPDR=0x01500000 injected=0x1C00)
gpioPortC: GPIOC first IDR read: 0x0440 ...   (PC6/PC10/PC13 high: DOWN/BACK released, SD in)
gpioPortH: GPIOH first IDR read: 0x0000 ...   (PH3 low: OK released, it is active high)
```

`0x1C00` sets bits 10, 11 and 12 - **UP, LEFT and RIGHT all high = released**, with the
firmware's own pull-up configuration (`PUPDR` 0x01 per pin) behind them. The bootloader
reads "no button held", so neither the DFU branch nor the recovery branch is taken and the
application is started.

### What runs now, and which screen it reaches

The UART log shows the application's own bring-up, which never appeared before:

```
[I][FuriHalRtc] Init OK           [I][FuriHalSpeaker] Init OK
[I][FuriHalInterrupt] Init OK     [I][FuriHalCrypto] Init OK
[I][FuriHalResources] Init OK     [I][FuriHalI2c] Init OK
[I][FuriHalSpiConfig] Init OK     [E][Gauge] ID: Device is not responding
[I][FuriHalIbutton] Init OK       [I][FuriHalPower] Init OK
[I][FuriHalBt] Start BT initialization
[CRASH][InitSrv] furi_check failed      <- then "Rebooting system."
```

**The screen is blank** - not the home screen, and no longer the DFU splash. Measured on
the panel recorder over 22 s: 2214 records, of which 2213 are `/RESET` edges and one is an
A0 edge, and **zero** are ST7567 commands or pixel data. The panel is never even
initialised, because the GUI is never reached: the app dies inside `furi_hal_bt_init()` and
reboots - 1106 times in 22 s (`platform resets: 1106 ... SYSRESETREQ`). A blank panel is
exactly what a panel that received no `AF` (display on) and no frame should show.

The failing check is inside `furi_hal_bt_init`: the crash dump's `lr` is `0x08003635` =
`furi_hal_bt_init+0x40` (the function is `0x080035F5..0x0800365D`), and disassembling that
range with `console/callgraph.py` places one `furi_check` between the two statements 1.4.3
has there:

```c
furi_hal_bus_enable(FuriHalBusHSEM); ... furi_hal_bus_enable(FuriHalBusCRC);
if(!furi_hal_bt.core2_mtx) {
    furi_hal_bt.core2_mtx = furi_mutex_alloc(FuriMutexTypeNormal);
    furi_check(furi_hal_bt.core2_mtx);
}
furi_check(LL_HSEM_1StepLock(HSEM, CFG_HW_CLK48_CONFIG_SEMID) == 0);
ble_glue_init();
```

Two candidates live in that window, and the next step is to separate them by measurement.
Already measured: a write watchpoint on `0x200005AC` (`furi_hal_bt.core2_mtx`) fires only on
the **first** boot of a run (before an include-time probe can arm), so every later boot
already sees a non-NULL stale pointer, skips the whole `if` block and cannot be crashing on
the mutex - which points at `LL_HSEM_1StepLock`. Its answer comes entirely from what the
register returns after its read-modify-write, and `HSEM` is still a Python stub that
returns 0 for reads and drops writes, so a real HSEM model (hardware-semaphore core-id
semantics) is the next piece of work.

### Instrumentation: what actually works on this Renode build

Recorded because it cost time, and because two entries invalidate earlier notes:

| Fact | Evidence |
|---|---|
| `Antmicro.Renode.Peripherals.Bus.Access` has **no `Execute`** member in 1.17.0 - only `Read`, `Write`, `ReadAndWrite` | `Enum.GetNames(clr.GetClrType(Access))` from inside the monitor |
| So every probe that armed *execute* hooks silently failed, and its "never fired" result described nothing: `ARM_<label>_FAIL='type' object has no attribute 'Execute'`. §14's retraction is above | `%TEMP%\_renode\...\artifacts\boot-probe.log` |
| Read and write watchpoints **do** work, and the hook signature is `hook(cpu, address, width, value)` | a positive control on `0x48000418` (GPIOB BSRR) fired with `argc=4` and decoded PC/LR correctly |
| Instruction fetch is **not** a `Read` on the bus, so "did execution reach X?" cannot be answered with these hooks | a `Read` hook on `0x080035F4` stayed silent while the function certainly executed |
| What does work: read `lr` out of the UART crash dump, resolve it with `console/symbols.py`, then disassemble with `console/callgraph.py --from A B`. That is how the check above was localised | this section |

Probes that write logs resolve relative paths against **Renode's own directory**, not the
repository (the same rule as `.repl` paths), so `artifacts/*-probe.log` lands beside
`renode.exe`.

### Along the way: a real bug in the UART capture script

`runner.write_uart_capture_script` opened `uart1.log` with mode `"ab"` and then called
`truncate(0)`. On any run where the log already existed that raised *"Unable to truncate
data that previously existed in a file opened in Append mode"*, which Renode reported as a
failed `include` - and since the start-up commands execute as one line, that failure
aborted every include chained after it. The script now opens the log `"wb"`, which empties
it without the invalid truncate, and chained includes survive.

Updated snapshot:

```
py -3.9 -m unittest discover -s tests   ->  Ran 75 tests ... OK
py -3.9 -m flipper_emu check            ->  load result: ok, 57 peripherals instantiated
py -3.9 -m flipper_emu run --seconds 22 ->  0 crashes, 0 resets: the application runs
                                            panel still blank (spins in bt_keys_storage_alloc)
```

## 16. The HSEM block: the boot loop is gone, and where the application stops now

§15 ended with the application dying in a `furi_check` inside `furi_hal_bt_init`, next to a
hardware-semaphore read, with the HSEM block served by a Python stub that returned 0 for
every read.

### The model

`peripherals/cs/HsemWb55.cs` is that block, and its register map was **read out of the
firmware** rather than taken from a datasheet guess: every `LDR`/`STR` the image performs
against the HSEM base literal was enumerated, and each one lines up with the semaphore id
the firmware documents for it (`targets/f7/ble_glue/hsem_map.h`):

| access in the image | register | semaphore (documented id) | site |
|---|---|---|---|
| read `[HSEM+0x80]` | `RLR0` | 0 RNG | `furi_hal_random_get`, `furi_hal_random_fill_buf` |
| read `[HSEM+0x8C]` | `RLR3` | 3 RCC | `furi_hal_power_sleep`, `furi_hal_power_shutdown` |
| read `[HSEM+0x90]` | `RLR4` | 4 ENTRY_STOP_MODE | `furi_hal_power_sleep`, `furi_hal_power_shutdown` |
| read `[HSEM+0x94]` | `RLR5` | 5 CLK48 | `furi_hal_bt_init`, `furi_hal_bt_start_radio_stack` |
| read `[HSEM+0xA0]` | `RLR8` | 8 BLE_NVM_SRAM | `furi_hal_bt_nvm_sram_sem_acquire` |
| write `[HSEM+0x20] = 0x400` | `R8` | 8 BLE_NVM_SRAM | `furi_hal_bt_nvm_sram_sem_release` |

So `R_i = 4*i`, `RLR_i = 0x80 + 4*i`, `LOCK` is bit 31 and `COREID` is bits 11:8 (CPU1 = 4).
The model implements take/release on the `R` bank, an owner/lock view on `RLR`, the
interrupt/clear registers (their offsets are the only part not derived from firmware
evidence, and the image never touches them), logs the first access to each semaphore, and
exposes `DumpState()`.

### What the failing check actually is

The crash dump's `lr` is `0x08003635` = `furi_hal_bt_init+0x40`. Disassembling the function
and reading its literal pool gives:

```
ldr   r3, =0x58001400        ; HSEM
ldr.w r2, [r3, #0x94]        ; RLR5 - the CLK48 semaphore
ldr   r3, =0x80000400        ; LOCK | COREID 4
cmp   r2, r3
bne   furi_crash             ; if it is not CPU1 holding CLK48 ...
```

There is no store to HSEM anywhere in that function (every byte of it was decoded), so
1.4.3's `furi_check(LL_HSEM_1StepLock(HSEM, CFG_HW_CLK48_CONFIG_SEMID) == 0)` compiles to a
**pure verification** that CPU1 already owns the CLK48 semaphore.

Measured with the model in place and nothing pre-held: over 18 s the *only* HSEM access in
the running firmware was

```
hsem: HSEM RLR[5] (CLK48): 0x00000000
```

and the run produced **555 crashes** in 18 s at exactly that site - so that is the failing
check, and nothing in the image takes the lock.

### Where the lock comes from, and what the emulator stands in for

The image is the application alone: the 1.4.3 `-full` package has one target with one
element, 768,132 bytes at `0x08000000`; the flash image's vector table has
`SP=0x20030000, PC=0x08011B9D`, which equals the ELF's entry point, and there is no second
vector table anywhere in the image. So the code that runs before the application on real
hardware - the boot loader - is not part of what we boot, and `HsemWb55.BootChainLockMask`
(`bootChainLockMask: 0x20` in the generated platform) supplies that handover. It is applied
on every reset because on hardware the boot loader runs on every reset, it logs what it
did, and it is one reversible line rather than a hidden behaviour. The fully faithful
alternative is to run a real boot loader image, which this package does not contain.

### Result: the reboot loop is gone

Before (HSEM as a zero-returning stub): 555-635 `furi_check` crashes per 18 s, and
`platform resets: 1106` per 22 s. After (model + the labelled CLK48 handover):

```
hsem: HSEM RLR[5] (CLK48): 0x80000400          <- the check now passes
[I][FuriHalBt] Start BT initialization
[I][FuriHalMemory] SRAM2A: 0x20031378, 0      [E][FuriHalMemory] No SRAM2 available
[I][FuriHalUsb] Init OK     [I][FuriHalVibro] Init OK
[E][FuriHalSubGhz] Init Fail  [E][FuriHalNfc] Wrong chip id
[I][Flipper] Firmware version: 1.4.3 ... Boot mode 0, starting services
platform resets: 0, cpu starts: 1
```

**Crash reports in the same 18 s: 0.** The reboot loop is gone: RTC/interrupts/resources/
SPI/iButton/speaker/crypto/I2C/power/BT init all complete, and Furi reaches "starting
services". The `Gauge`, `FuriHalSubGhz` and `FuriHalNfc` errors are the absent-chip paths
(no bq27220, no CC1101, no ST25R3916), not regressions.

### The next blocker, instrumented and symbolised

The panel is still untouched (2 recorder records, 0 ST7567 commands, 0 data bytes), and the
application is not idle-crashing but **spinning**. Measured, not guessed:

```
sample 1: PC=0x0800AEBC after 250ms PC=0x0800AEC0 SPINNING | ExecutedInstructions=7310000
sample 3: PC=0x0800AEBC after 250ms PC=0x0800AEBC FROZEN   | ExecutedInstructions=31108626
   (cpu.IsHalted = False, SysTick counting: a live busy-wait, not a halted WFI)

0x0800AEBC -> furi_hal_random_fill_buf+0x17     LR 0x08071B19 -> bt_keys_storage_alloc+0x28
ldr   r1, =0x58001400        ; HSEM
ldr   r3, =0x80000400        ; LOCK | COREID 4
0x0800AEBC  ldr.w r2, [r1, #0x80]     ; RLR0 - the RNG semaphore
0x0800AEC0  cmp   r2, r3
0x0800AEC2  bne   0x0800AEBC          ; spin until CPU1 holds it
```

So BLE key-storage allocation asks for random bytes, and `furi_hal_random_fill_buf` waits
for the RNG semaphore to read `LOCK | COREID4`. `furi_hal_random_init` (0x0800AE51) enables
the RNG bus clock but - per the complete HSEM scan - touches no semaphore at all.

The cheap experiment already ruled out the simplest explanation: pre-holding semaphore 0 as
well (`bootChainLockMask: 0x21`) was applied and verified in the log
(`hsem: HSEM RLR[0] (RNG): 0x80000400`) and **the spin did not clear**. §17 measures why:
the application *consumes* that handover and releases the semaphore again, so a one-shot
handover only ever satisfies one RNG call. The mask therefore stays at `0x20` (only the part
that is measured to matter).

### Tooling facts from this step

| Fact | Evidence |
|---|---|
| Renode 1.17 resolves platform-file keys as **constructor arguments**; a settable property alone fails the load | `Error E25: Could not find suitable constructor for type '...HsemWb55'` with `bootChainLockMask: 0x20` and no matching parameter |
| `cpu.ExecutedInstructions` plus the SysTick registers tell "halted" from "spinning" without guessing | the table above (6M -> 41M instructions while the PC stayed in one 4-byte loop) |
| Static scanning of the image for a peripheral's base literal, then decoding the `LDR`/`STR` offsets that follow it, recovers a peripheral's register map from the firmware itself | the HSEM map above (`python` one-off over `console/callgraph.py`'s `ThumbImage`) |

Updated snapshot:

```
py -3.9 -m unittest discover -s tests   ->  Ran 75 tests ... OK
py -3.9 -m flipper_emu check            ->  load result: ok, 57 peripherals instantiated
py -3.9 -m flipper_emu run --seconds 18 ->  0 crashes, 0 resets (was 555-635 crashes)
                                            application runs to "starting services"
                                            panel blank: spins in furi_hal_random_fill_buf
```

## 17. The RNG semaphore: the application releases it and never takes it

Question under investigation: `furi_hal_random_fill_buf` spins on
`*(HSEM+0x80) != LOCK|COREID4` (semaphore 0, the RNG). Is a *functional RNG peripheral*
missing, does anything in the image ever write/release the semaphore, and if nothing does,
is an RNG model the answer?

### The peripheral: not modelled, and not what blocks

`RNG` is still a stub - `memmap.py` has `("RNG", 0x58001000, 0x400, None, "rng_stub", "")`,
which the generator turns into `script: "if request.IsRead: request.Value = 0x00000001"`
(every read returns 1, writes are dropped). The 1.4.3 source of the reader
(`targets/f7/furi_hal/furi_hal_random.c`) is:

```c
static uint32_t furi_hal_random_read_rng(void) {
    while(LL_RNG_IsActiveFlag_CECS(RNG) || LL_RNG_IsActiveFlag_SECS(RNG) ||
          !LL_RNG_IsActiveFlag_DRDY(RNG)) { ...clear flags, discard 12 words... }
    return LL_RNG_ReadRandData32(RNG);
}
```

so the reader polls `RNG_SR` (CECS/SECS = bits 1/2, DRDY = bit 0) and then reads `RNG_DR`.
With the stub, `SR` reads `0x1` = DRDY set, so the poll passes immediately and `DR` returns
a constant - i.e. **the RNG peripheral does not block this path** (it does make every
`furi_hal_random_get()` return 1, so key material and seeds are constant). A functional RNG
would be needed for correct *values*, not for progress.

### The semaphore: the image writes it (my earlier windowed scan was wrong)

Both RNG functions were decoded byte-completely (they are 56 and 108 bytes long):

```
furi_hal_random_get  (0x0800AE6D)          furi_hal_random_fill_buf (0x0800AEA5)
  PUSH {r3,r4,r5,lr}                         PUSH {r0,r1,r4,r5,r6,lr}
  LDR r5, =0x58001400  ; HSEM                MOV r4,r1 / MOV r6,r0 ; len, buf
  LDR r3, =0x80000400  ; LOCK|COREID4        CBNZ/CMP  ; furi_check(buf), furi_check(len)
  loop: LDR.W r2,[r5,#0x80]  ; RLR0           LDR r1, =0x58001400 ; HSEM
        CMP r2,r3                            LDR r3, =0x80000400
        BNE loop            ; wait for us    loop: LDR.W r2,[r1,#0x80] ; RLR0
  LDR r4, =0x58001000  ; RNG                        CMP r2,r3
  RNG_CR |= 4 (enable)                             BNE loop
  BL furi_hal_random_read_rng                      LDR r2, =RNG ; enable ; loop{read, store}
  RNG_CR &= ~4 (disable)                           RNG_CR &= ~4 (disable)
  0x0800AE90  MOV.W r3, #0x400                     0x0800AEFA  MOV.W r2, #0x400
  0x0800AE94  STR r3, [r5]   ; write R0            0x0800AEFE  STR r2, [r3, #0] ; write R0
```

So each function **waits (read-only) for the semaphore to read as held by CPU1, uses the
RNG, and then writes `0x400` (COREID 4, LOCK clear) into `R0`** - the release pattern. There
is no write with `LOCK` set anywhere in either function: the image never *takes* semaphore 0.

My earlier scan reported "read only" because it only looked 32 bytes past the HSEM literal
load, and both release writes sit in the function tails. That is corrected here.

### Runtime confirmation (and the explanation of the mask-0x21 result)

Write/read watchpoints on `R0` (`0x58001400`) and `RLR0` (`0x58001480`), double-word width:

```
sem 0 not held (bootChainLockMask 0x20):
  READ_RLR0 #1..8  PC=0x0800AEBC LR=0x08071B19   ; furi_hal_random_fill_buf <- bt_keys_storage_alloc
  t+2.5s counts: {'READ_RLR0': 206712}           ; 1.1M spins in 10 s, no WRITE_R0 at all

sem 0 handed over (bootChainLockMask 0x21):
  READ_RLR0 #1  PC=0x0800AEBC LR=0x08071B19     ; first call passes the wait
  WRITE_R0  #1  value=0x00000400 PC=0x0800AEFE LR=0x0800AEEF   ; <-- the app RELEASES it
  READ_RLR0 #2  PC=0x0800AEBC LR=0x08071B23     ; second call waits, forever
  t+2.5s counts: {'READ_RLR0': 191887, 'WRITE_R0': 1}
```

That is exactly why mask `0x21` "did not hold up": the handover is *consumed* by the first
RNG call and released, and the second call (a different site inside `bt_keys_storage_alloc`,
LR `0x08071B23` vs `0x08071B19`) then waits on a free semaphore.

### What this means for the fix (not implemented)

- A functional RNG model alone would **not** clear the spin: the wait is on the semaphore,
  not on RNG data.
- The image releases semaphore 0 but never takes it, so the holder has to come from outside
  the application - and because the application releases it after every use, a *one-shot*
  handover (or a static `bootChainLockMask` bit) can only ever satisfy one call.
- Two readings of the `STR 0x400` write remain, and they are distinguishable:
  (A) it is a *release* (as `LL_HSEM_ReleaseLock(HSEM, id, 0)` in the tag's source implies) -
  then the environment has to keep granting semaphore 0 to CPU1 whenever it is free
  (modelling the FUS/CPU2 side of the RNG arbitration, which this emulator does not have);
  (B) the write itself *claims* the semaphore (hardware sets LOCK), which would make the
  application self-sufficient after a single handover.
  (B) is disfavoured by the measurement above - under the current model the 0x21 handover
  was consumed and the next call still spun - but it is the one experiment that decides,
  and it is a one-line change to the release handling.
- Caveat on source references: the running binary reports commit `8622f1a2` while the
  sources quoted here are tag `1.4.3`; the tag's `furi_hal_random.c` passes `0` as the
  release CoreID, the compiled code writes `0x400`, so the two are close but not identical.
  Everything above that matters is taken from the binary, not from the source.

### Instrumentation fact worth keeping

Watchpoints on a modelled peripheral must match the width the model is registered with:
`HsemWb55` derives from `BasicDoubleWordPeripheral`, so accesses appear as *double words* -
a `SysbusAccessWidth.Word` watchpoint on it never fires (measured: it silently recorded
nothing, while the same hook at double-word width fired immediately).

## 18. The RNG semaphore: (A) held, (B) measured out, and the GUI now draws

§17 left two readings of the application's core-id write (`STR 0x400`, `LOCK` clear) to
semaphore 0: (B) it *claims* the semaphore, or (A) it *releases* it and something outside
the image has to keep granting it. Both were implemented behind the labelled switch
(`coreIdWriteClaims`, `arbitratedSemaphores`) and measured in that order.

### (B) `coreIdWriteClaims: true` - measured out

With the claim semantics the block accepted the write as a claim, but the application never
reached it: `RLR0` still read `0x00000000` at the *first* wait, no `R write[0]` event was
ever recorded, and the CPU kept spinning at `furi_hal_random_fill_buf+0x17`
(`ExecutedInstructions` climbing 7M -> 50M, PC oscillating 0x0800AEBC/0x0800AEC0). That is
consistent with §17: the wait comes *before* the write, so the claim semantics can only
matter for later calls - it cannot satisfy the first one. Kept at `false`.

### (A) `arbitratedSemaphores: 0x1` - holds

The RNG semaphore is held by CPU1 from reset and handed straight back to it whenever it is
freed (this emulator has no CPU2/FUS to arbitrate it), with `coreIdWriteClaims: false`.
Measured:

## 22. The microSD card mounts in the emulator (09-28)

The storage service now mounts the card: `[I][StorageExt] card mounted` at 1245 ms, 759
command frames, 67 sectors read, 0 resets and 0 crashes in a 24 s run - and the firmware then
reads `/int/.notification.settings` off the card image (which only carries
`/.int/.slideshow`, so the other reads fail with "file/dir not exist" as expected).

Two platform bugs stood in the way, both measured before being fixed
(`docs/ISSUES_AND_LOGS.md`, P26):

* the DWT stub's 2^26-cycle step per read made every `furi_hal_cortex_timer` expire
  immediately, so `sd_spi_wait_for_data()` read the 0xFE data token, declared a timeout and
  purged the 512-byte block payload the card had just been handed (514 bytes discarded, 2 ms
  of emulated time, zero writes to DMA2 in the whole run);
* Renode's stock DMA model moved the bytes but never delivered the DMA2 channel-6 completion
  interrupt that `furi_hal_spi_bus_trx_dma()` waits on, so every block read ended in
  `[E][FuriHalSpi] DMA timeout` -> `furi_check` -> `[CRASH][StorageSrv]` at
  `sd_device_read+0xCA` and a reboot loop.

`peripherals/cs/DwtWb55.cs` (CYCCNT advances one microsecond per read) and
`peripherals/cs/DmaWb55.cs` (WB55 channel DMA, one level interrupt per channel, RX advanced
in lockstep with the TX channel that clocks it) replace the stub and the stock model.
`generate.py` wires DMA2 channel 6 to IRQ 60 and channel 7 to IRQ 61 - numbers taken from the
firmware's own vector table and pinned by `tests/test_irq_numbers.py`.

Reproduction:

```
# Card present: SD_CD (PC10) is active low.  Passed as an include script because a
# --renode-command containing spaces cannot survive Start-Process quoting.
py -3.9 src/flipper_emu/platform/sdcard_build.py        # rebuild: the firmware deletes /.int/.slideshow
py -3.9 -m flipper_emu run --seconds 40 --renode-include src/flipper_emu/platform/card_present.resc
py -3.9 -m flipper_emu.frontend.frame_stats artifacts/display-stream.bin
```

Two timing facts about this platform, both consequences of the honest cycle counter, both of which
make a short run look blank even though the firmware is drawing:

* the **first panel byte lands ~13 s of wall time into a run** (`furi_hal_power_init`'s
  gauge/charger retry delays are real emulated time now), so a run under ~20 s shows
  `commands=0 data_bytes=0 frames=0` with nothing drawn at all - measure with 40 s;
* **never add `--renode-include src/flipper_emu/platform/dwt_trace.resc` to a run you want to
  watch.** `sysbus LogPeripheralAccess dwt` logs every CYCCNT read, and the delay loops read it
  millions of times: measured 1,803,772 log lines in 20 s with the panel stream still at 0 bytes,
  i.e. the boot never reaches its first draw. That include exists for short, targeted looks at the
  counter, not for rendering runs.

Snapshot, 09-28 - updated counts:

```
py -3.9 -m unittest discover -s tests     ->  Ran 95 tests ... OK   (94 before, +1 for the DMA wiring)
```

Still open for the first-start slideshow: the card model never completes a write
(`0 sectors written`, so settings saves fail with "internal error" and the slideshow file
cannot be deleted), and the slideshow frames have not been observed on the panel yet - the
stream from the mounting run holds only 9 framebuffer states.

A follow-up that started as a suspected regression is worth recording: a 20 s run recorded only
7 framebuffer states, and a 40 s run records **739** (78 848 data bytes, 1857 commands, 0 resets)
- the low figure was simply the run ending moments after the first draw, because the honest
counter put the first panel byte ~13 s of *wall* time into the run. The animation itself is paced
by `furi_timer_start()` (the RTOS timer, `bubble_animation_view.c`), not by CYCCNT, so the DWT
handshake never affected its cadence. What *did* come out of it is the step size now used:
CYCCNT advances 4 us per read instead of 1 us, which moved the first draw from 13.1 s to 3.2 s of
wall time while staying five times below the tightest timeout in the firmware.

Also worth knowing before watching a run: **do not add
`--renode-include src/flipper_emu/platform/dwt_trace.resc`**. That logs every CYCCNT read, and
the delay loops read it millions of times - measured 1 803 772 log lines in 20 s with the panel
stream still at 0 bytes, i.e. the boot never reaches its first draw. It exists for short,
targeted looks at the counter.




```
HSEM: no arbitration here - semaphores 0x1 are held by core 4 from reset
hsem: HSEM RLR[0] (RNG): 0x80000400            <- the wait passes
rng: RNG enabled (CR=0x00000004); data is deterministic from seed 0x12345678
[E][Core2] C2 startup failed                   [I][BleExtraBeacon] Init
[E][FuriHalBt] Core2 start failed              [E][BtSrv] Radio stack start failed
[I][AnimationManager] Select 'L1_Tv_128x47' animation
[I][FuriHalUsb] USB Mode change done
platform resets: 0, cpu starts: 1
```

### Renode gotcha this exposed

Applying a platform-file value only inside `Reset()` did **not** take effect for this model
(measured: no arbitration log line, `RLR0` still free). Applying it from the property setter
does - the same pattern the boot-chain mask needed. Both values are therefore applied by
their setters, and the comments in `generate.py` record why.

### The RNG model, and evidence it matters

`peripherals/cs/RngWb55.cs`: `CR 0x00` (`RNGEN` bit 2), `SR 0x04` (`DRDY` bit 0, plus
write-1-to-clear `CEIS`/`SEIS`), `DR 0x08`; while enabled, `DRDY` reads set and every `DR`
read returns the next xorshift32 word, seeded from `seed: 0x12345678` in the platform file
(deterministic on purpose - it is not a source of entropy, only a replacement for the
constant the stub returned).

The firmware's own log shows it consuming varying values: the Desktop picks a random idle
animation, and that pick **changed** once the model was in place -

```
before the model: [I][AnimationManager] Select 'L1_NoSd_128x49' animation
after  the model: [I][AnimationManager] Select 'L1_Tv_128x47' animation
```

### The screen

The GUI is initialised and the panel is drawn:

```
panel recorder: 6399 records - 6144 data bytes, 155 commands, 100 pin edges
describe:       page=7 column=128 on=True com_rev=True contrast=32
```

`py -3.9 -m flipper_emu.frontend.st7567 artifacts/display-stream.bin artifacts/gui-screen.png
--ascii` renders UI chrome across the first pages and the Desktop's idle animation below it
(the dolphin outline is visible in the lower pages). That closes the display path end to
end: buttons were already injected and decoded (§15), and now the firmware's own drawing
arrives as real panel traffic.

### What is still missing (all visible as errors, none fatal)

| Message | Cause |
|---|---|
| `[E][Gauge] bq27220_read_word failed` | the fuel gauge is still a stub (I2C NAKs) |
| `[E][FuriHalSubGhz] Init Fail` | no CC1101 |
| `[E][FuriHalNfc] Wrong chip id` | no ST25R3916 |
| `[E][Core2] C2 startup failed`, `[E][BtSrv] Radio stack start failed` | no CPU2/wireless stack, as expected |

Updated snapshot:

```
py -3.9 -m unittest discover -s tests   ->  Ran 76 tests ... OK
py -3.9 -m flipper_emu check            ->  load result: ok, 57 peripherals (29 modelled)
py -3.9 -m flipper_emu run --seconds 22 ->  0 crashes, 0 resets
                                            GUI init: AnimationManager selects an idle animation
                                            panel: 6144 data bytes + 155 commands
```

## 19. The tickless idle wakes up: our own LPTIM model, and the animation plays

§18 ended with the panel drawn but the screen frozen: the GUI was initialised, one frame
was rendered, and then the core parked in `furi_hal_power_sleep` waiting for a wakeup that
never came (P17). The wakeup is LPTIM1, and the reason it never arrived was the model.

The firmware's tickless idle (`furi_hal_idle_timer.h`, `furi_hal_os.c`:
`vPortSuppressTicksAndSleep`) arms LPTIM1 as a **one-shot compare-match** timer with
interrupts masked, sleeps in WFI, then *polls* the CMPM/ARRM flags and clears the pending
IRQ in `furi_hal_idle_timer_reset()` (an RCC reset of the timer plus
`NVIC_ClearPendingIRQ`). It registers **no ISR** for IRQ 47 - so any delivery of that
interrupt ends in `furi_check(isr_descr->isr)` and a reset. Renode's
`Timers.STM32L0_LpTimer` fires on its own `LimitTimer` limit rather than the compare match
it was given, and says so (`Compare value (16117) cannot be greater than auto reload limit
(1). Compare value will be ignored`), which produced 96-322 crashes and chip resets per
run.

So `peripherals/cs/LptimWb55.cs` replaces it: a one-shot counter to `CMP` and on to `ARR`,
a level IRQ on `ISR & DIER`, `CNT` reading 0 after a completed one-shot, and - the piece
that makes the handshake terminate - a bus hook on the timer's own RCC reset line
(`RCC_APB1RSTR1` bit 31 / `APB1RSTR2` bit 5) so `furi_hal_idle_timer_reset()` really does
stop it. `src/flipper_emu/platform/lptim_probe.py` samples the model's own `DumpState()`
rather than the bus (P20 showed core-space bus reads are unreliable):

```
t+ 2s lptim1 CR=0x00000003 ISR=0x0 DIER=0x00000001 CMP=15429 ARR=15432 CNT=7372  starts=11 cmpm=10 irq=False
t+10s lptim1 CR=0x00000003 ISR=0x0 DIER=0x00000001 CMP=15462 ARR=15465 CNT=10265 starts=53 cmpm=52 irq=False
```

53 one-shots and 52 compare matches in 10 s with the interrupt line low at every sample:
the firmware wakes, reads a live counter, and clears the pending IRQ.

The measurement that closes P17 - a 45 s run (`artifacts/anim45.log`), `platform resets: 0`,
0 `furi_check` failures, 216 LPTIM1 reset-line assertions, and **841 distinct framebuffer
states** across 1,463 snapshots:

```
py -3.9 -m flipper_emu.frontend.frame_stats artifacts/display-stream-45s.bin
  stream: artifacts\display-stream-45s.bin (187301 bytes, 1463 snapshots of 64 records)
  distinct framebuffer states: 841   (the Desktop idle animation keeps playing)
```

and the live path verified without a human watching, through the same tail + injector the
window uses:

```
py -3.9 -m flipper_emu ui --selftest 20
  selftest: injected OK at 20 frames, 20224 events
  selftest: events=47872 frames=48 resyncs=0
  OK: display decoded live and buttons reach the firmware
```

The platform generator is the source of truth for this: `fw/memmap.py` holds `LPTIM_MODEL`
and both timers are rendered from it, so `py -3.9 -m flipper_emu platform` reproduces the
`platform/` files **byte for byte** (checked by hash before/after) and no platform file
needs hand-editing.

Remaining errors are still only the absent chips (gauge, Sub-GHz, NFC, CPU2/wireless), all
non-fatal and visible in the log: see the table in §18.

Snapshot, 09-27 22:45 - every number above is reproducible from a clean checkout:

```
py -3.9 -m unittest discover -s tests     ->  Ran 86 tests ... OK
py -3.9 -m flipper_emu check              ->  load result: ok, 57 peripherals instantiated
py -3.9 -m flipper_emu platform           ->  platform/ files byte-identical (idempotent)
py -3.9 -m flipper_emu run --seconds 45   ->  platform resets: 0, cpu starts: 1, 0 furi_check
py -3.9 -m flipper_emu ui --selftest 20   ->  events=47872 frames=48 resyncs=0, OK
```

Open items left are peripheral coverage, not the boot path: no gauge (I2C stub), no Sub-GHz
(CC1101), no NFC (ST25R3916), no CPU2/wireless stack, and the microSD card over SPI2.

## 20. What storage actually is in 1.4.3, and the SD conversation on SPI2

The first-start slideshow is gated by one call:

```c
if(storage_file_exists(desktop->storage, SLIDESHOW_FS_PATH))   // "/int/.slideshow"
    scene_manager_next_scene(desktop->scene_manager, DesktopSceneSlideshow);
```

so "why is the slideshow missing" is "why does `/int/.slideshow` not exist". Section 19's
snapshot assumed that means a missing **internal-flash** volume (LittleFS on an SPI flash
chip). Instrumenting the firmware says otherwise, and this section records the correction.

**There is no internal flash volume in 1.4.3.** `storage_internal_dirname_i.h` defines
`STORAGE_INTERNAL_DIR_NAME ".int"`, and every `/int/...` path is rewritten before it reaches
a backend - `storage_process_alias()` in `storage_processing.c`, followed by

```c
furi_assert(type == ST_EXT);
```

i.e. internal storage **is a hidden directory on the SD card**: `/int/x` → `/ext/.int/x`.
Consistent with that: the image has **zero `lfs_*` symbols**, links FatFS, and the only
`f_mount` callers are the SD API and the bootloader's update path. A `check` run also shows
QUADSPI untouched (0 accesses, 0 `SR` reads) - there is no flash device driver in this build
to find.

**The SD card is a slave on the panel's bus.** `fh_furi_hal_spi_config.c`:

```c
const FuriHalSpiBusHandle furi_hal_spi_bus_handle_sd_slow = {
    .bus  = &furi_hal_spi_bus_d,          // the display bus - SPI2
    .miso = &gpio_spi_d_miso, .mosi = &gpio_spi_d_mosi, .sck = &gpio_spi_d_sck,
    .cs   = &gpio_sdcard_cs,              // PC12
};
```

with `sd_fast` the same pins at the 16 MHz preset and `sd_slow` at 2 MHz. From
`fh_furi_hal_resources.h`: `SD_CS = PC12`, `SD_CD = PC10` (active low), `SPI_D_SCK = PD1`,
`SPI_D_MOSI = PB15`, `SPI_D_MISO = PC2`. Presence is one line -
`furi_hal_sd_is_present() { return !furi_hal_gpio_read(&gpio_sdcard_cd); }` - and
`sd_notify`/`StorageExt` mount only while it is true.

**What the driver sends to a card** (`fh_furi_hal_sd.c`): 80 dummy clocks with CS high, then
`sd_spi_send_cmd()` frames of `(cmd | 0x40)`, 4 argument bytes, `crc | 0x01` - CMD0
`40 00 00 00 00 95`, CMD8 `48 00 00 01 AA 87`, CMD55 `77`, ACMD41 `69` (v2 sets bit 30),
CMD58 `7A`, then CMD16/CMD13/CMD17/CMD18/CMD24/CMD25 with `0xFE` data tokens and 512-byte
blocks (blocks go through DMA, everything else through byte-wise `furi_hal_spi_bus_trx`,
`SD_TIMEOUT_MS = 1000`). Failure budgets live in the constants: `SD_IDLE_RETRY_COUNT = 100`
(CMD0), 128 outer init retries, `furi_hal_sd_max_mount_retry_count() = 10`, plus a power reset
path that disables external 3.3 V, grounds the bus, pulls CD low for 250 ms, then re-arms the
pins and waits 100 ms.

**The instrument that measured it.** The panel is the only slave attached to `spi2`
(`st7567: ... @ spi2`), so the recorder file it writes is the *union* of everything shifted
out on that bus - panel bytes and card bytes alike. `src/flipper_emu/platform/sd_stream_audit.py`
parses those `[tag][payload]` records, looks for the driver's exact frames, and prints the
bytes around every hit, so single-byte coincidences inside pixel data cannot pass as traffic.
It replaced an IronPython watchpoint probe on the SPI data registers, which reported "no
storage traffic on any bus" and was wrong twice over: it died with
`AccessViolationException` under the card's byte volume, so its summary never printed, and two
of its runs read a **stale** recorder file - the lock documented as P19 - which the copies
gave away by being MD5-identical (`4DB2BE9FDA33624FD16D8372B15A942C`). A file written by the
C# model cannot fail either way.

Card-detect pin, measured with `src/flipper_emu/platform/cd_pin_probe.py` (GPIOC
`IDR/MODER/OTYPER/PUPDR/ODR` over the first seconds):

```
board injects PC10 high  ->  IDR 0x00003C40   PC10/CD=1   (no card, as the board file intends)
inject PC10 low          ->  IDR 0x00002840   PC10/CD=0   (card), OTYPER=0x400 (open-drain on
                             pin 10, set by the power-reset path), PUPDR=0x05501010 (pull-up
                             added by furi_hal_sd_presence_init)
```

A/B of the same 16 s run, differing only in that pin (audit output, trimmed):

| | payload bytes on SPI2 | CMD0 `40 00 00 00 00 95` | firmware UART |
|---|---|---|---|
| no card | 31 451 | none | no `StorageExt` line at all |
| card | 1 026 907 | **128 000** (first at offset 80) | `card detected`, then `init cycle 10 … 1, error: internal` every ~1.4 s |

128 000 is exactly `10 mount retries × 128 init retries × 100 CMD0 attempts` from the
constants above, and the histogram is what that implies - `00=513833 FF=256801 95=128002
40=128001` - i.e. the firmware is shouting GO_IDLE_STATE into a bus where nothing answers (the
panel model returns 0x00 while the driver waits for 0xFF, then 0x01). The 31 451-byte baseline
matches the earlier watchpoint probe's number exactly, which is the cross-check that both
instruments now agree.

**Consequence for the slideshow.** The missing peripheral is the **microSD card**, not a flash
chip: an SPI2 slave selected by PC12 that answers CMD0/CMD8/CMD55/ACMD41/CMD58 and serves
512-byte blocks from a FAT image containing `.int/.slideshow` (built from
`artifacts/first_start-dev/`). Two facts shape that work: `spi2` currently has exactly one
connection, so Renode routes *all* transfers to the panel and card bytes have to be split by
chip select; and the file the firmware looks for is deleted when the slideshow exits
(`storage_common_remove(SLIDESHOW_FS_PATH)`), so the FAT image has to be disposable per run.

Reproduction:

```
py -3.9 -m flipper_emu run --seconds 16                                    # no card: 31 451 bytes, no CMD0
py -3.9 -m flipper_emu run --seconds 16 --renode-command "gpioPortC OnGPIO 10 false"   # card: 128 000 CMD0
py -3.9 src/flipper_emu/platform/sd_stream_audit.py artifacts/display-stream.bin
py -3.9 -m flipper_emu run --seconds 14 --renode-include src/flipper_emu/platform/cd_pin_probe.py
```

Snapshot, 09-28 07:40 - updated counts:

```
py -3.9 -m unittest discover -s tests     ->  Ran 94 tests ... OK   (86 before, +8 for the audit tool)
```

