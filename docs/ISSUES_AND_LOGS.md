# Problems and logs — complete record

Everything that went wrong on this project, why, how it was fixed (or whether it still
isn't), plus the raw measured output for each claim. Written at the request of "every
problem I have hit and every log, like every output" — so it is deliberately redundant
with `BRINGUP_LOG.md`: that file tells the story in order, this file is the **index of
failures + evidence** you can grep.

Related docs: `BRINGUP_LOG.md` (chronological deep dives, §0–§18), `DECISIONS.md`,
`PERIPHERAL_COVERAGE.md`, `../peripherals/cs/README.md` (plugin recipe).

Measurement state at time of writing: **76 unit tests OK**, platform loads
(`load result: ok`, 57 instances), firmware boots to the GUI with 0 crashes / 0 resets —
but **problem P17 (screen freezes on one frame after ~2 s) is still OPEN**: the IRQ
wiring fix is implemented and the platform regenerated, and a fresh verification run
shows the CPU is *still* frozen identically (evidence in P17).

---

## Index of every problem

| # | Phase | Problem | Status |
|---|---|---|---|
| P01 | DFU | DfuSe container is Flipper's *variant* layout (11-byte extra header, 3 pad bytes) | fixed, verified vs official 1.4.3 package |
| P02 | DFU | Suffix CRC matches no standard convention (5 variants tried) | worked around — SHA256 from release manifest |
| P03 | Platform | `.repl` syntax/constructor errors (E00/E25/E21 ×3) during platform bring-up | fixed |
| P04 | Boot | CPU started at `0x0` (flash not aliased) | fixed — dual registration `0x00000000` + `0x08000000` |
| P05 | Boot | **Boot loop**: 3192 `SYSRESETREQ` resets in 25 s | fixed — `rcc_wb55.py` register model (§3/§9) |
| P06 | Boot | C# plugin model "exists but never resolvable" | fixed — `[Plugin]` class + `Antmicro.Renode.*` namespace + `--config` |
| P07 | Boot | Renode 1.17 has no `Access.Execute` — execute probes silently no-op | documented; earlier "never fired" conclusions retracted |
| P08 | Tooling | Probe/recorder **relative paths resolve to Renode's root**, not the repo | fixed — absolute paths from the generator |
| P09 | Tooling | `--console` and `-P <port>` mutually exclusive → button path broken | fixed — runner never passes `--console` when monitoring |
| P10 | Tooling | UART capture script opened `"ab"` then `truncate(0)` → crash on 2nd run | fixed |
| P11 | Boot | `furi_check` crash inside `furi_hal_bt_init` — 1106 resets/22 s, `HSEM RLR5=0` | fixed — `HsemWb55.cs` + `bootChainLockMask: 0x20` |
| P12 | Boot | RNG semaphore spin — 1.1 M spins/10 s in `furi_hal_random_fill_buf` | fixed — `arbitratedSemaphores: 0x1` (+ `RngWb55.cs`) |
| P13 | Renode | Platform values applied only in `Reset()` don't take effect | fixed — apply in property setters |
| P14 | Display | Panel never initialised (blank): 2214 records = 2213 RESET edges, 0 commands | fixed — A0/DC wiring + `St7567Display.cs` |
| P15 | Input | Bootloader/board button levels missing → boot decision chain wrong | fixed — startup levels in `.resc` post-`start` |
| P16 | Display | "Retry loops" in digest (`spi2 CR2 44310x`, `i2c1 TIMINGR 7389x`) | investigated — normal firmware behaviour / absent device; no fix needed |
| P17 | **UI** | **Screen freezes on one frame after ~2 s; frame counter stops advancing** | **OPEN** — IRQ wiring implemented + regenerated, verified *not yet effective* |
| P18 | **UI** | **Bottom-right of screen looks incomplete/cut off** | diagnosed — not missing stream data; 2 latent decoder defects logged |
| P19 | Env | Stale/protected `renode.exe` processes lock `artifacts/display-stream.bin` | open — workaround only (alt capture path); needs elevation to kill |
| P20 | Probes | LPTIM1 register *offsets mislabeled in my probes* (report said `CR=0x03`) | corrected here — raw offsets recorded, `CR` (0x20) still unread |
| P21 | Tooling | Default renode lookup fails (`FileNotFoundError`) → needs `--renode`/`RENODE_EXE` | documented |
| P22 | UI (latent) | Tail skip-resync jumps to newest records without resetting decoder addressing | known, not yet hit |
| P23 | Decoder (latent) | `render()` applies `com_reversed` twice → net no-op | known, not yet fixed |
| P24 | Decoder (by design) | `render()` drops columns ≥128 | correct here (measured 0 writes outside 0..127) |


---

## The problems, in detail

### P01/P02 — DFU container is not textbook DfuSe

Symptom: parsing `flipper-z-f7-full-1.4.3.dfu` (768,441 bytes, SHA256 verified against
Flipper's release manifest) with a textbook DfuSe parser misreads the target/element
records.

```
DfuSe version=1 declared_size=768425 data_end=768425 target_bytes=768414 layout=flipper
target 0 'Flipper Zero F7' declared_size=768140 elements=1
  element 0: 0x08000000..0x080BB884 (768132 bytes)
suffix vid=0x0483 pid=0xDF11 crc=0x7A619838 crc_ok=None
SP=0x20030000 reset=0x08011B9D thumb_bit=1
```

Cause: Flipper adds an 11-byte variant header before the 255-byte target name and 3 pad
bytes before the element records; `dwImageSize` counts up to the DFU suffix. The suffix
CRC (`0x7A619838`) matches **none** of five standard CRC conventions tried.
Fix: variant parser + classic fallback; integrity via manifest SHA256.
Status: 14 tests, byte-for-byte verified against the official package.

### P03 — platform bring-up errors (3 iterations)

| # | Error | Cause | Fix |
|---|---|---|---|
| 1 | `Error E00: unexpected '['; expected attribute list end` | range connections need bracketed indices | `[0] -> nvic@[6]` |
| 2 | `Error E25: no suitable constructor for 'SPI.STM32SPI'` | `bufferCapacity` doesn't exist; `STM32Series.L4` invalid | `series: STM32Series.F4` only |
| 3 | `Error E21: no suitable constructor for this registration point` | GPIO needs a ranged registration | `@ sysbus <0x48000000, +0x400>` |
| 4 | `Error E25` for `MTD.STM32L0_FlashController` | requires an `eeprom` binding | bound to the option-bytes region |

### P04 — CPU started at zero

```
[INFO] cpu: Setting initial values: PC = 0x0, SP = 0x0.
[WARNING] sysbus: [cpu: 0x0] ReadDoubleWord from non existing peripheral at 0x4.
```

STM32 boots with flash aliased at 0. Fix: one `Memory.MappedMemory` registered at both
`0x00000000` and `0x08000000`. Verified: `PC = 0x08011B9D, SP = 0x20030000` from the
real vector table.

### P05 — the boot loop

25 s run, 18.8 MB log, 188,352 lines:

```
unmapped accesses: 0 distinct
registers the existing models do not implement: 25 distinct
  exti      offset 0x80 read   28728x
  rcc       offset 0x58 read   25536x     <- RCC_APB1ENR1 on WB55
  gpioPortA offset 0x04 write  15960x
platform resets: 3192, cpu starts: 3193  <-- BOOT LOOP
  SYSRESETREQ x3192
```

Cause: read-modify-write on WB55 clock-enable registers read back 0 → the firmware's own
consistency check fails → `NVIC_SystemReset()`. Fix: `peripherals/python/rcc_wb55.py`
(instant `HSION→HSI16RDY`-style ready flags, `CSR` reset flags, pulse semantics for the
reset registers). After it:

```
registers the existing models do not implement: 3 distinct
  exti      offset 0x80 read   19130x
  gpioPortA offset 0x04 write  11478x
  nvic      offset 0xDFC write   3826x
platform resets: 3825, cpu starts: 3826  <-- still looping (EXTI/RTC next)
```

Second cause (BRINGUP §9): RTC clock never brought up — fixed too. Loop gone.

### P06 — loading a custom C# peripheral (cost the most time)

Four non-obvious requirements, all still true:
1. the assembly needs a **`[Plugin]`-annotated class** (not just a peripheral type),
2. it must sit **beside `renode.exe`**,
3. it must be enabled via config `[plugins] enabled-plugins` (runner passes `--config`),
4. the type must live under an **`Antmicro.Renode.*` namespace**, otherwise it appears in
   the "available peripherals" list but is never resolvable by a `.repl`.

Build: `dotnet build -c Release -p:RenodeDir=<renode> src\flipper_emu\peripherals\cs\FlipperEmu.Peripherals.csproj`
(~9 s on this machine).

### P07 — `Access.Execute` does not exist in Renode 1.17.0

```python
Enum.GetNames(clr.GetClrType(Access))   # from inside the monitor
# -> Read, Write, ReadAndWrite   (NO Execute)
```

Every probe that armed an *execute* hook silently failed with
`ARM_<label>_FAIL='type' object has no attribute 'Execute'`, so its "never fired" result
described nothing — an earlier conclusion in the log had to be retracted (kept in
`BRINGUP_LOG.md` §14). What does work: read/write watchpoints with
`hook(cpu, address, width, value)` and `SysbusAccessWidth.DoubleWord` (Word width fails
silently for `BasicDoubleWordPeripheral`); instruction fetch is *not* a bus `Read`, so
"did execution reach X" is answered by symbolising `LR` from crash dumps +
`console/callgraph.py` disassembly.

### P08 — relative paths resolve to Renode's root (re-hit once)

Probe logs with `open("artifacts/timer-probe.log", "a")` land **beside `renode.exe`**:

```
%TEMP%\_renode\renode_1.17.0-portable\artifacts\anim-probe.log
%TEMP%\_renode\renode_1.17.0-portable\artifacts\timer-probe.log
%TEMP%\_renode\renode_1.17.0-portable\artifacts\display-stream.bin   (1,193,683 bytes, 10:06)
... boot-probe, bt-probe, halt-probe, mutex-probe, sem0-probe, stall-probe logs
```

Same rule hit the *recorder* first (captures went into the Renode install). Fix: the
generator emits an **absolute** `outputPath`; probes must use absolute paths. Re-hit
09-27: a probe with a relative `STREAM` path silently measured the wrong file (caught
because `os.path.getsize` disagreed with the repo's file).

### P09 — `--console` and `-P <port>` are mutually exclusive

A session that needs the TCP monitor (button injection) must not pass `--console`;
shutdown then goes over the monitor (`pause; dump; quit`) so the flash is still saved.

### P10 — UART capture script bug

`runner.write_uart_capture_script` opened `uart1.log` with `"ab"` and then called
`truncate(0)` → on any run where the file already existed:
`Unable to truncate the file ... opened for writing`. Fixed.

### P11 — the `furi_check` crash in `furi_hal_bt_init` (HSEM)

Symptom: after startup the app died and rebooted — 1106 resets in 22 s:

```
[I][FuriHalRtc] Init OK           [I][FuriHalSpeaker] Init OK
[I][FuriHalInterrupt] Init OK     [I][FuriHalCrypto] Init OK
[I][FuriHalResources] Init OK     [I][FuriHalI2c] Init OK
[I][FuriHalSpiConfig] Init OK     [E][Gauge] ID: Device is not responding
[I][FuriHalIbutton] Init OK       [I][FuriHalPower] Init OK
[I][FuriHalBt] Start BT initialization
[CRASH][InitSrv] furi_check failed      <- then "Rebooting system."
```

Evidence chain: crash dump `lr = 0x08003635` = `furi_hal_bt_init+0x40` → disassembly
places the failing check on `LL_HSEM_1StepLock(HSEM, CFG_HW_CLK48_CONFIG_SEMID)` against
`RLR5`; with HSEM a Python stub returning 0, that check can never pass
(`hsem: HSEM RLR[5] (CLK48): 0x00000000`, **555 crashes in 18 s** at that one site).
Fix: `peripherals/cs/HsemWb55.cs` — register map **derived from the firmware image**
(every `LDR`/`STR` against the HSEM base literal, cross-checked against
`targets/f7/ble_glue/hsem_map.h`) — plus `bootChainLockMask: 0x20`, because on real
hardware the *boot loader* (not present in this DFU) holds CLK48 across every reset.

### P12 — the RNG semaphore spin

```
sem 0 not held (bootChainLockMask 0x20):
  READ_RLR0 #1..8  PC=0x0800AEBC LR=0x08071B19   ; furi_hal_random_fill_buf <- bt_keys_storage_alloc
  t+2.5s counts: {'READ_RLR0': 206712}           ; 1.1M spins in 10 s, no WRITE_R0 at all
```

Both RNG functions were decoded byte-completely: they **wait** for semaphore 0 to read as
held by CPU1, use the RNG, then **release** it (`STR #0x400` into `R0`) — the image never
*takes* it (an earlier windowed scan missed the release because it only looked 32 bytes
past the HSEM literal load; corrected in §17). Measured out: `coreIdWriteClaims: true`
(the wait happens *before* any write, so claim semantics cannot satisfy the first wait —
`ExecutedInstructions` climbed 7 M → 50 M spinning, `RLR0` still 0). What holds:
`arbitratedSemaphores: 0x1` — semaphore 0 held by CPU1 from reset, handed back whenever
freed (this emulator has no CPU2/FUS to arbitrate it).

```
HSEM: no arbitration here - semaphores 0x1 are held by core 4 from reset
hsem: HSEM RLR[0] (RNG): 0x80000400            <- the wait passes
rng: RNG enabled (CR=0x00000004); data is deterministic from seed 0x12345678
```

Side evidence the `RngWb55.cs` model matters — the firmware's random idle-animation pick
changed:

```
before the model: [I][AnimationManager] Select 'L1_NoSd_128x49' animation
after  the model: [I][AnimationManager] Select 'L1_Tv_128x47' animation
```

### P13 — Renode gotcha: values applied only in `Reset()` don't take effect

Measured: with `arbitratedSemaphores`/`bootChainLockMask` set only inside `Reset()`, the
model logged nothing and `RLR0` stayed free. Setting the same value from the **property
setter** works. Both values are applied by their setters; `generate.py` comments record
why.

### P14 — panel never initialised (blank screen)

Measured over 22 s: **2214 records — 2213 `/RESET` edges, one A0 edge, zero ST7567
commands, zero pixel data.** Two causes, both fixed: A0/DC not wired (PB1→A0,
PB0→reset) and no SPI slave yet. After: `panel recorder: 6399 records — 6144 data bytes,
155 commands, 100 pin edges`.

### P15 — boot decision chain / button startup levels

`main()` polls `GPIO->IDR` (LEFT=DFU, UP=recovery); without the board's startup levels
the chain misbehaved. Fixed with post-`start` button/SD levels in `flipper_zero.resc`;
real EXTI presses then come from the UI/monitor.

### P16 — digest "retry loops" that were not bugs

* `spi2 offset 0x04 write 44310x` — `CR2` writes on the **panel** bus: every byte at
  `furi_hal_spi_bus_tx+0x49` from `u8x8_hw_spi_stm32+0x27` (u8g2 backend); `SR` toggles
  `0x02` (TXE) → `0x03` (TXE|RXNE). The model's `DS[8-10]`/`FRXTH` warnings are cosmetic;
  WB55 SPI is the v1 layout (confirmed from ST's CMSIS header), so `STM32Series.F4` is right.
* `i2c1 offset 0x10 write 7389x` — `TIMINGR` unimplemented by Renode's `STM32F7_I2C`;
  it's the fuel-gauge probe retrying (instant under our DWT fast-forward). The LED driver
  does only init/deinit: 260 sampled accesses, **zero** transfers — absent LP5562, not a
  semantics bug.


### P17 — screen freezes on one frame after ~2 s (resolved — see P24)

User-visible symptom: the live window draws the first frame(s), the frame counter/data
numbers stop advancing, and it sits on the same image indefinitely. Reported again after
the IRQ fix (see verification below) — same numbers as before.

#### Evidence 1 — the stream stops growing (pre-fix, `artifacts/anim-probe.log` path caveat: this one is from the Renode root `artifacts/`)

```
ANIM_PROBE_LOADED panel=None
CapturedBytes attribute present: False
t+ 2s captured=err:'NoneType' object has no attribute 'CapturedBytes' file=12295
t+ 4s ... file=12295
t+ 6s ... file=12295
t+ 8s ... file=12295
t+10s ... file=12295
t+12s ... file=12295
t+14s ... file=12295
t+16s ... file=12295
t+18s ... file=12295
t+20s ... file=12295
t+22s ... file=12805          <- +510 bytes only at teardown
```

#### Evidence 2 — the CPU stops retiring instructions (pre-fix, `timer-probe.log`)

```
TIMER_PROBE_LOADED cpu=<...CortexM... [CPU: flipper.cpu[0]]>
t+ 2s PC=0x08009E9C LR=0x08009DDF instr=3398735
      systick ctrl=0x00000006 load=0x0000F9FF val=0x000010EC
      lptim1(ISR,DIER,CFGR,CR,ARR,CNT) 0x0100001B 0x00000001 0x00000000 0x00000003 0x00003EF8 0x00000000
      (labels above are the probe's own and are WRONG - see P20; offsets read were 0x00,0x08,0x0C,0x10,0x18,0x1C)
t+ 4s ... identical (instr=3398735)
t+ 6s ... identical (instr=3398735)     <- same at every sample to +20s
```

Interpretation: `PC = 0x08009E9C` = `furi_hal_power_sleep+0xC3` (the WFI inside the
tickless-idle path), `SysTick CTRL=0x06` = ENABLE|CLKSOURCE with **TICKINT off** (FreeRTOS
tickless idle), instruction count frozen → the core is waiting for an interrupt that never
arrives. With no interrupt and no other scheduled event, Renode's virtual time has nothing
to drive the panel, so drawing stops too.

#### What was implemented (and verified to be *loaded*)

`generate.py` gained `PERIPHERAL_NVIC_IRQS` — peripheral IRQ outputs wired to the NVIC at
numbers **read out of the firmware's own vector table** (handler address at vector slot
`16 + n` identifies the owner of IRQ `n`; no datasheet guessing):

```
LPTIM1 = 46   LPTIM2 = 47   RTC_Alarm = 40   TIM2 = 27
TIM1_UP/TIM16 = 24   TIM1_TRG_COM/TIM17 = 25   USART1 = 35
(HSEM=45, IPCC C1 RX/TX=43/44 have handlers too, but those models expose no IRQ output)
```

Regenerated platform (09-27 12:12) contains exactly these connection lines:

```
IRQ -> nvic@27          (tim2)
AlarmIRQ -> nvic@40     (rtc)
IRQ -> nvic@46          (lptim1)
IRQ -> nvic@47          (lptim2)
IRQ -> nvic@24          (tim1/tim16)
IRQ -> nvic@35          (usart1)
IRQ -> nvic@24          (tim16)
IRQ -> nvic@25          (tim17)
```

and it loads: `py -3.9 -m flipper_emu check` → `load result: ok / peripherals instantiated: 57`.
RTC wakeup (`RTC_WKUP`) was deliberately left unwired: that vector is still the firmware's
default handler.

#### Verification result (09-27 15:27, fresh run): **the fix did NOT change anything**

Fresh headless run with an extended probe (absolute output paths; stream written to a
*separate* file because the normal one was locked — see P19):

`py -3.9 -m flipper_emu run --renode <renode.exe> --renode-include %TEMP%\_renode\timer_probe2.py`
→ `emulator session: 25.2 s`, digest below — note the **new** entry
`lptim1 offset 0x24 read 12x` (the firmware is now reading `LPTIM1`'s low registers in a
way it did not before):

```
registers the existing models do not implement: 10 distinct
  spi2       offset 0x04 write    150x
  i2c1       offset 0x10 write     31x
  lptim1     offset 0x24 read      12x      <- NEW vs the pre-fix run
  spi1       offset 0x04 write      6x
  usart1     offset 0x2C read/write  2x/2x
  flashController offset 0x84 read   2x
  nvic       offset 0xDFC write     1x
  rtc        offset 0x08 write      1x
platform resets: 0, cpu starts: 1
```

Probe output (`artifacts/timer-probe2.log`, verbatim):

```
PROBE2_LOADED cpu=<...CortexM... [CPU: flipper.cpu[0]]>
t+ 2s PC=0x08009E9C LR=0x08009DDF instr=3398735 delta=-
      systick ctrl=0x00000006 val=0x000010EC | stream=12295 bytes
      lptim1(ISR,DIER,CFGR,CR,ARR,CNT) 0x0100001B 0x00000000 0x00000003 0x00003EF8 0x00000000 0x00000000
      (verbatim from the file; offsets read were 0x00,0x0C,0x10,0x18,0x1C,0x24 - labels wrong, see P20)
      ISER0=0x00D807C4 ISER1=0x0040B300 IPSR=0x00000800 PRIMASK=0x410FC240
t+ 4s ... instr=3398735 delta=-  stream=12295 bytes   (all fields identical)
t+ 6s ... instr=3398735 delta=-  stream=12295 bytes
t+ 8s ... instr=3398735 delta=-  stream=12295 bytes
t+10s ... instr=3398735 delta=-  stream=12295 bytes
t+12s ... instr=3398735 delta=-  stream=12295 bytes
```

Conclusions, stated strictly:

1. **Same PC, same instruction count (3,398,735), same SysTick state, same frozen stream
   size (12,295) as the pre-fix run** — the platform loads the new IRQ lines but the
   behaviour is bit-for-bit unchanged. The wiring is *not sufficient*; it may still be
   *necessary*.
2. `ISER1=0x0040B300` decodes to NVIC-enabled IRQs 40, 41, 44, 45, **47** (and 54) —
   i.e. RTC alarm, IPCC TX, HSEM, **LPTIM2**, but **not 46 (LPTIM1)**. So by the time the
   CPU is parked, the firmware has LPTIM2 enabled at the NVIC, not LPTIM1. (Whether the
   read itself is trustworthy is P20-adjacent: `IPSR=0x00000800` and `PRIMASK=0x410FC240`
   are impossible values, so **core-control-space reads via `SystemBus.ReadBytes` are not
   reliable**; the ISER pattern is plausible but should be re-confirmed through the NVIC
   peripheral object rather than the bus.)
3. `LPTIM1` raw register facts — parsed programmatically from both logs, offset→value,
   and **both runs agree on every common offset**:
   `[0x00]=0x0100001B, [0x08](IER)=0x00000001, [0x0C](CFGR)=0, [0x10](CNT)=0x00000003,
   [0x18](CMP)=0x00003EF8, [0x1C](RCR)=0, [0x24]=0`.
   `0x14 (ARR)` and `0x20 (CR)` were **never read** — so the earlier claim
   "`CR=0x03` armed" (§timer notes) came from mislabeled offsets and must not be trusted.
4. The next measurements needed (in order): (a) read `LPTIM1` at **0x20** and `LPTIM2`
   (base `0x40007800`) at 0x00/0x10/0x14/0x20 to see which timer is actually armed;
   (b) query the **NVIC peripheral object** (not the bus) for enabled/pending state of
   40/41/46/47; (c) read the RTC alarm registers (`0x40002800`: CR 0x08, ISR 0x0C,
   ALRMAR 0x30) — IRQ 40/41 are enabled, so RTC may be the real wakeup source and its
   EXTI lines (17/18) are **not** wired in our EXTI model; (d) symbolise the exact
   instruction at `0x08009E9C` to confirm it is the WFI (vs. a fault path).

#### Why the user's re-test may also have been invalidated (see P19)

`artifacts/display-stream.bin` was locked during the re-test window by a `renode.exe`
that cannot be killed without elevation, so the UI reads the *stale* file content
(frozen at 12,805 bytes — the teardown figure every run ends at), and a new emulator
instance cannot open the recorder file at all. Kill check (09-27 15:22):

```
taskkill /F /IM renode.exe /T
  ERROR: The process with PID 198876 (child process of PID 178600) could not be terminated.
renode procs: 20      stream file: STILL LOCKED
Stop-Process ... : Cannot stop process "renode (15208)" because ... Access is denied
```



### P18 — bottom-right of the screen looks incomplete/cut off (diagnosed; data is complete)

Two separate questions were instrumented, exactly as asked: *is anything missing from the
SPI stream*, and *is the decoder misplacing it*.

#### Measurement 1 — frame audit (decoder subclassed so the project's own command handling runs)

`%TEMP%\_renode\frame_audit.py` against `artifacts/display-stream-anim.bin`:

```
pixel-data bytes: 6144, frames captured: 6
  frame  0 hash=0631457264ff cells_written=1024
  frame  1 hash=c1b7c2787dd0 cells_written=1024
  frame  2 hash=c1b7c2787dd0 cells_written=1024
  frame  3 hash=c1b7c2787dd0 cells_written=1024
  frame  4 hash=c1b7c2787dd0 cells_written=1024
  frame  5 hash=4c0933b474bd cells_written=1024
distinct frames: 3

coverage of the LAST frame ('#' pixel on, '.' pixel off, '?' never written in it):
  page 0 #......#..##################################################...#.........................#...#####.#......................#...##
  page 1 .................................................................................................###############################
  page 2 ....#...................#......................................................................##.............##................
  page 3 ........#...........#.##........####.....#................#............########...........#.........................#...........
  page 4 ............#...#.........##................#..............................#........##................................##.......#
  page 5 ########.............############################.###...........................#.....####.......................#.........###.#
  page 6 ####################################.######.#########...####..#.#..#..#..#..#..##............................#........#........#
  page 7 ##....######.#.#.#..##..##.######.#########.##################################################################.................#
```

Reading: every frame writes **all 1024 cells** (8 pages x 128 columns), so nothing is
"never written" in a frame; and the firmware genuinely produced **3 distinct frames**
before going quiet (that quiet is P17, not P18).

#### Measurement 2 — column addressing audit (does any byte land outside the visible window?)

```
data bytes: 6144
  page 0: 768 writes, columns 0..127, outside 0..127: 0
  page 1: 768 writes, columns 0..127, outside 0..127: 0
  page 2: 768 writes, columns 0..127, outside 0..127: 0
  page 3: 768 writes, columns 0..127, outside 0..127: 0
  page 4: 768 writes, columns 0..127, outside 0..127: 0
  page 5: 768 writes, columns 0..127, outside 0..127: 0
  page 6: 768 writes, columns 0..127, outside 0..127: 0
  page 7: 768 writes, columns 0..127, outside 0..127: 0
```

768 = 128 columns x 6 frames exactly, and **zero writes outside 0..127** — so the
`render()` drop-columns->=128 rule (P24) never removes anything in these frames, and the
per-page column commands address the panel correctly.

#### Measurement 3 — what is actually in the bottom-right (decoded pixels, rows 34-63, cols 96-127)

```
41 ####................######.....#
42 ..................##......###..#
43 .................#.........###.#
44 ####............#...........####
45 ##.............#.............###
...
48 ..............#...........######
...
58 ........######..###............#
59 ##############.................#
60 ...........###.................#
61 .............#.................#
63 .............#.................#
```

That is the `L1_Tv_128x47` animation's own artwork: the solid right border at x=127 down
every row, the TV frame's bottom edge, and empty space inside the picture area — i.e.
frame *content*, not truncated data. The window itself cannot clip it: the canvas is sized
`WIDTH*scale x HEIGHT*scale` exactly and the PNG is encoded 1:1.

#### Verdict + latent defects it surfaced

* **Not** missing stream data, **not** partial-page compositing, **not** window clipping.
* P22 (UI tail skip-resync doesn't reset decoder `page`/`column` state) — can composite a
  skipped region at the wrong address once streams get long; not observed here.
* P23 (`render()` applies `com_reversed` twice: in-loop `y = HEIGHT-1-y` **and**
  `rows.reverse()` — the two cancel, so the panel's `C8` COM-reverse setting has **no net
  effect**) — latent: today's image happens to look right because u8g2's page order and
  the panel setting already agree; a stream where it should matter would render unflipped.

### P19 — stale/protected `renode.exe` processes lock the recorder file (env, OPEN)

Symptoms and errors, verbatim:

```
System.IO.IOException: The process cannot access the file
'C:\Users\gtttr\auraesp32\flipper-emu\artifacts\display-stream.bin'
because it is being used by another process.
```

```
PS> Stop-Process -Name renode ...
Stop-Process : Cannot stop process "renode (15208)" because of the following error:
Access is denied

PS> taskkill /F /IM renode.exe /T
ERROR: The process with PID 198876 (child process of PID 178600) could not be terminated.
renode procs: 20      stream file: STILL LOCKED
```

Facts:
* up to 19-20 `renode.exe` processes accumulate from probe runs that never shut down;
* a subset of them (different elevation/session) **cannot be killed without admin**;
* while one holds `display-stream.bin`, a new emulator cannot open the recorder file, and
  the UI still *reads* the old content — which looks exactly like "the fix didn't work"
  (frozen frame counter, same numbers). This is a standing alternative explanation for any
  "still frozen" observation taken without first checking the lock.
* Workarounds used: point `outputPath` at a fresh file for verification runs
  (`display-stream-verify.bin`), regenerate the platform to restore the standard path;
  and `Get-Process renode | Stop-Process -Force` for the killable ones. Real fix: run one
  session at a time and close the UI window (which stops the emulator) before re-running.

### P20 — my own probe mislabeled the LPTIM1 registers (instrumentation bug)

The probes printed label text that did not match the offsets they read:

| offsets actually read | probe's printed label | true STM32 LPTIM registers |
|---|---|---|
| 0x00, 0x08, 0x0C, 0x10, 0x18, 0x1C | `ISR,DIER,CFGR,CR,ARR,CNT` | ISR, **IER**, CFGR, **CNT**, **CMP**, **RCR** |
| 0x00, 0x0C, 0x10, 0x18, 0x1C, 0x24 | `ISR,DIER,CFGR,CR,ARR,CNT` | ISR, CFGR, CNT, CMP, RCR, (beyond map) |

Consequences: the widely-repeated note "**LPTIM1 armed (CR=0x03)**" is wrong — offset
0x18 is `CMP` (it held `0x00003EF8`), and `CR` at `0x20` plus `ARR` at `0x14` were never
read at all. The corrected raw facts are in P17 conclusion 3 (both runs agree). The labels
in the quoted log lines are left as-is (they are what the files say) with a note. Next
probe must read `0x14` and `0x20` and derive the layout from the firmware's own accesses
(the same scan method that produced the HSEM map).

### P21 — default renode lookup fails

```
FileNotFoundError: Renode not found. Run tools/fetch_renode.ps1 (or point RENODE_EXE at renode.exe).
```

On this machine the portable build lives in `%TEMP%\_renode\renode_1.17.0-portable\`, so
headless runs need
`py -3.9 -m flipper_emu run --renode "%TEMP%\_renode\renode_1.17.0-portable\renode.exe" ...`
(`check` likewise). The `ui` path works because it resolves the same way as before — if it
ever errors, pass `--renode`/set `RENODE_EXE`.

### P22 — tail skip-resync doesn't reset decoder addressing (latent)

`DisplayStreamTail` keeps a decoding budget (256 KB/poll) and, past 512 KB backlog,
**jumps to the newest records** and clears `leftover` — but the decoder's `page`/`column`
carry on from before the jump, so the first partial frame after a resync can composite at
the wrong address (it heals on the next full page pass). `--selftest` prints the resync
count so it stays measurable: `selftest: events=2219776 frames=806 resyncs=1`.

### P23 — `render()` applies `com_reversed` twice (latent)

In the page/bit loop `y` is mirrored when `com_reversed`, and then `rows.reverse()`
mirrors again — the two cancel, so the `C8` setting is a net no-op. Harmless for today's
stream (image reads correctly), wrong by construction. Fix: mirror exactly once (pick the
convention that matches u8g2's page order + panel `C8`) with a golden-image test.

### P24 — columns >= 128 dropped by `render()` (by design, measured irrelevant)

`if x >= WIDTH: continue` — the ST7567 visible area is 128 columns even though the RAM
window is 132. Audit (P18) shows **0 writes outside 0..127** in the captured streams, so
nothing is lost today.


---

## Appendix A — key raw outputs (verbatim)

### A1 — firmware UART boot log (a full run, `artifacts/uart1.log`)

```
0 [I][FuriHalRtc] Init OK
0 [I][FuriHalInterrupt] Init OK
0 [I][FuriHalResources] Init OK
0 [I][FuriHalSpiConfig] Init OK
0 [I][FuriHalIbutton] Init OK
0 [I][FuriHalSpeaker] Init OK
0 [I][FuriHalCrypto] Init OK
0 [I][FuriHalI2c] Init OK
0 [E][Gauge] ID: Device is not responding     (x2)
0 [I][FuriHalPower] Init OK
1 [I][FuriHalBt] Start BT initialization
1 [I][FuriHalMemory] SRAM2A: 0x20031378, 0
1 [I][FuriHalMemory] SRAM2B: 0x20038000, 0
1 [E][FuriHalMemory] No SRAM2 available
1 [I][FuriHalUsb] Init OK
1 [I][FuriHalVibro] Init OK
1 [E][FuriHalSubGhz] Init Fail                 (no CC1101 - expected)
1 [E][FuriHalNfc] Wrong chip id                (no ST25R3916 - expected)
2 [I][Flipper] Firmware version: 1.4.3 / Build date: 05-12-2025 / Git: 8622f1a2 (0) / Branch: 1.4.3
2 [I][Flipper] Boot mode 0, starting services
4 [I][Flipper] Startup complete
9 [I][Loader] Executing system start hooks
10 [E][Gauge] bq27220_read_word failed         (x7)
15 [E][Core2] C2 startup failed                (no CPU2 - expected)
15 [E][FuriHalBt] Core2 start failed
15 [I][BleExtraBeacon] Init
15 [E][BtSrv] Radio stack start failed
16 [I][AnimationManager] Select 'L1_Tv_128x47' animation
25 [I][FuriHalUsb] USB Mode change done
```

### A2 — platform check (after IRQ wiring)

```
$ py -3.9 -m flipper_emu check --renode <renode.exe>
platform:    platform\stm32wb55_flipper.repl
load result: ok
peripherals instantiated: 57
```

Head of that load's log (`artifacts/platform-check.log`):

```
[12:12:18.3464] [INFO] HSEM: no arbitration here - semaphores 0x1 are held by core 4 from reset
[12:12:18.3468] [INFO] HSEM: standing in for the boot loader - semaphores 0x20 are already held by core 4
[12:12:18.3539] [INFO] ST7567 panel attached; recording the SPI stream to ...artifacts/display-stream.bin
```

### A3 — generation + tests

```
$ py -3.9 -m flipper_emu platform
bus: 50 peripherals (29 modelled, 21 stubbed)
outputPath: "C:/Users/gtttr/auraesp32/flipper-emu/artifacts/display-stream.bin"

$ py -3.9 -m unittest discover -s tests
............................................................................
----------------------------------------------------------------------
Ran 76 tests in 2.598s
OK
```

### A4 — end-to-end selftest (historical, pre-freeze era, §12)

```
emulator started (monitor port 3504)
selftest: injected OK at 269 frames, 761600 events
selftest: events=2219776 frames=806 resyncs=1
OK: display decoded live and buttons reach the firmware
emulator stopped, flash saved
```

### A5 — GUI-reached snapshot (§18)

```
panel recorder: 6399 records - 6144 data bytes, 155 commands, 100 pin edges
describe: page=7 column=128 on=True invert=False all_on=False adc_rev=False com_rev=True contrast=32
py -3.9 -m flipper_emu run --seconds 22  ->  0 crashes, 0 resets
```

---

## Appendix B — reproduction commands

```powershell
# from the repository root
$env:PYTHONPATH = "src"
$renode = "$env:TEMP\_renode\renode_1.17.0-portable\renode.exe"   # (P21)

py -3.9 -m flipper_emu platform                 # regenerate .repl/.resc from fw/memmap.py
py -3.9 -m flipper_emu check --renode $renode   # platform loads? (~seconds)
py -3.9 -m flipper_emu run --seconds 25 --renode $renode            # boot + digest
py -3.9 -m flipper_emu run --renode $renode --renode-include "$env:TEMP\_renode\timer_probe2.py"   # P17 probe
py -3.9 -m flipper_emu ui                       # live window (buttons, decoded panel)
py -3.9 -m flipper_emu ui --selftest 20         # headless: tail 20s, tap OK, report frames
py -3.9 -m unittest discover -s tests           # 76 tests

# audits (host-side, on a captured stream)
py -3.9 "$env:TEMP\_renode\frame_audit.py"      # frames + per-cell coverage (P18)
# column audit + pixel crops: see the snippets quoted in P18

# C# plugin rebuild (only needed when peripherals/cs/*.cs change - NOT for platform regen)
dotnet build -c Release -p:RenodeDir=tools\renode src\flipper_emu\peripherals\cs\FlipperEmu.Peripherals.csproj
```

Note: probe scripts live in `%TEMP%\_renode\` and must use **absolute** output paths
(P08); they are included via `--renode-include` (monitor `include @...`).

---

## Appendix C — open items, in priority order

1. **Route A - the microSD card model** (`BRINGUP_LOG.md` §20). The first-start slideshow
   only appears when `/int/.slideshow` exists, and in 1.4.3 `/int` is aliased to `/ext/.int`
   **on the card**, so the missing peripheral is the card, not a flash chip: an SPI2 slave
   selected by PC12 (`gpio_sdcard_cs`) answering CMD0/CMD8/CMD55/ACMD41/CMD58 and serving
   512-byte blocks from a FAT image built from `artifacts/first_start-dev/`. `spi2` has exactly
   one connection today, so the panel/card split has to key off the chip select.
2. **P19 (locked stream file).** Check for stale/protected `renode.exe` before trusting any
   "same numbers as before" observation, and cross-check an A/B with
   `py -3.9 src/flipper_emu/platform/sd_stream_audit.py`: two copies that hash the same are a
   stale read (P25), not a result.
3. **P22 / P23** (tail resync state, double `com_reversed`) - small, testable fixes.
4. P17 is closed (§19, and the 45 s run now shows 841 distinct framebuffer states), so what is
   left on the display path is the SPI2 chip-select routing that Route A needs.


*File generated 2026-09-27; source of truth for every number is the log/probe file quoted
beside it. If a claim here disagrees with a fresh measurement, trust the fresh
measurement and update both this file and `BRINGUP_LOG.md`.*



## P23 - NVIC IRQ numbers were off by one (fixed, verified)

The platform IRQ map had every number one too low: `LPTIM1 -> nvic@46`, but 46 is
HSEM's vector - LPTIM1 is IRQ **47**. Re-derived from the image's vector table
(slot `16 + n` -> the handler named there owns IRQ `n`), resolved against the
release ELF:

| peripheral | IRQ | vector-table handler |
|---|---|---|
| TIM1_UP / TIM16 | 25 | `TIM1_UP_TIM16_IRQHandler` |
| TIM1_TRG_COM / TIM17 | 26 | `TIM1_TRG_COM_TIM17_IRQHandler` |
| TIM2 | 28 | `TIM2_IRQHandler` |
| USART1 | 36 | `USART1_IRQHandler` |
| LPUART1 | 37 | `LPUART1_IRQHandler` |
| RTC alarm | 41 | `RTC_Alarm_IRQHandler` |
| HSEM | 46 | `HSEM_IRQHandler` |
| LPTIM1 | **47** | `LPTIM1_IRQHandler` |
| LPTIM2 | 48 | `LPTIM2_IRQHandler` |

Before: the CPU froze in `furi_hal_power_sleep+0xC3` for the whole session with
no instruction retirement; `ISER1 = 0x0040B300` (IRQ 47 enabled),
`ISPR1 = 0x00004000` (IRQ 46 pending, never delivered), panel stream stuck at
12,295 bytes. After: the CPU executes continuously and the stream grows at about
100 KB/s. Pinned by `tests/test_irq_numbers.py`, which re-derives the table from
`artifacts/flash.img` + the ELF.

## P24 - resolved: our own WB55 LPTIM model replaces the stock timer

With the IRQ numbers fixed the firmware boots into the GUI and then resets 96
times in 20 s: `[CRASH][ISR LPTIM1] furi_check failed`, `lr = 0x08007FA3`
(`LPTIM1_IRQHandler+0x12`), `r4 = 0x200006A8` (the `furi_hal_interrupt` struct),
`r11 = 0x20031364` (`__furi_check_message`).

Tag 1.4.3, `targets/f7/furi_hal/furi_hal_interrupt.c`:

```c
FURI_ALWAYS_INLINE static void furi_hal_interrupt_call(FuriHalInterruptId index) {
    const FuriHalInterruptISRPair* isr_descr = &furi_hal_interrupt.isr[index];
    furi_check(isr_descr->isr);        // <-- the crash
```

`furi_hal_idle_timer.h` shows LPTIM1 *is* the tickless-idle timer
(`FURI_HAL_IDLE_TIMER_IRQ = LPTIM1_IRQn`, one-shot, compare-match interrupt,
`SetCompare(count - 3)`, `SetAutoReload(count)`), and `furi_hal_idle_timer_init()`
enables the NVIC line while registering no callback. `furi_hal_os.c`
(`vPortSuppressTicksAndSleep` -> `furi_hal_os_sleep`) runs the sleep with
`__disable_irq()`, then *polls* the CMPM/ARRM flags and clears the pending IRQ in
`furi_hal_idle_timer_reset()` (`NVIC_ClearPendingIRQ`). This interrupt is never
meant to be taken - which is why no callback for `FuriHalInterruptIdLpTim1`
exists anywhere in the firmware (checked across all of `targets/f7/furi_hal` and
`furi/core`).

Renode's `Timers.STM32L0_LpTimer` does not follow that contract: it fires on its
own `LimitTimer` limit instead of the firmware's compare match, and says so -
`lptim1: Compare value (16117) cannot be greater than auto reload limit (1).
Compare value will be ignored`. It therefore keeps raising IRQ 47; as soon as the
CPU runs with interrupts enabled and that pending bit set, the ISR runs, finds a
NULL callback and crashes.

The originally planned model, now implemented (`peripherals/cs/LptimWb55.cs`):
one-shot counting to `CMP`, flag-based IRQ raise/deassert, so that the firmware's
poll-and-clear handshake behaves as designed.

### Resolution: a WB55 LPTIM model in place of the stock timer

`peripherals/cs/LptimWb55.cs` (the same C# plugin pattern as the GPIO/EXTI/HSEM/RNG
models) implements `ISR`/`ICR`/`DIER`/`CFGR`/`CR`/`CMP`/`ARR`/`CNT`: one-shot counting to
the *compare* value and on to the auto-reload, a flag-based level IRQ on `ISR & DIER`,
the counter at the LSE rate the firmware selects (`FURI_HAL_IDLE_TIMER_CLK_HZ` = 32768),
and `CNT` reading 0 once a one-shot has completed - the firmware's tick arithmetic
(`after_cnt = CNT + skew + 3`) only balances if a completed one-shot leaves `CNT` at 0.

The handshake completes because the model watches its own RCC reset line
(`RCC_APB1RSTR1` bit 31 / `APB1RSTR2` bit 5, via a bus hook) and resets itself when
`furi_hal_idle_timer_reset()` asserts it. That is what turns "flag set -> interrupt ->
firmware clears pending" into a sequence that *terminates* instead of a permanently
re-pending IRQ. A bus hook is the only way to model a reset line here: the RCC is still an
IronPython stub, and `PythonPeripheral` exposes only `Size` and `Code`, so it cannot reach
another peripheral.

Measured, same firmware and platform, 14-22 s headless runs:

| | stock `STM32L0_LpTimer` | our `LptimWb55` |
|---|---|---|
| `[CRASH][ISR LPTIM1] furi_check failed` | 96-322 per run | **0** |
| `platform resets` / `cpu starts` | 96-144 / 97-145 | **0 / 1** |
| panel stream | grew only by redrawing on reboot | grows continuously (~100 KB/s) |
| `lptim1: Compare value ... will be ignored` | every sleep round | gone |

`src/flipper_emu/platform/lptim_probe.py` samples the model's own `DumpState()`
(deliberately not a bus read - see P20), verbatim:

```
t+ 2s lptim1 CR=0x00000003 ISR=0x0 DIER=0x00000001 CFGR=0x0 CMP=15429 ARR=15432 CNT=7372  running=True starts=11 cmpm=10 arrm=0 irq=False
t+ 4s lptim1 CR=0x00000003 ISR=0x0 DIER=0x00000001 CFGR=0x0 CMP=15462 ARR=15465 CNT=8247  running=True starts=21 cmpm=20 arrm=0 irq=False
t+10s lptim1 CR=0x00000003 ISR=0x0 DIER=0x00000001 CFGR=0x0 CMP=15462 ARR=15465 CNT=10265 running=True starts=53 cmpm=52 arrm=0 irq=False
```

53 one-shots and 52 compare matches in 10 s, with the interrupt line low at every sample:
the firmware wakes on the compare match, reads a live counter, and clears the pending IRQ.
`lptim2` reads `starts=0` throughout - the firmware never arms it in these runs.

A 45 s run (`artifacts/anim45.log` + `artifacts/display-stream-45s.bin`, 09-27 22:39) gives
`platform resets: 0`, 0 `furi_check` failures, **216** LPTIM1 reset-line assertions, and
**841 distinct framebuffer states** across 1,463 snapshots of the panel stream: the idle
animation plays rather than one frame being redrawn. **That is P17 resolved.** The live
path is verified end to end too - `py -3.9 -m flipper_emu ui --selftest 20`:

```
emulator started (monitor port 3456)
selftest: tailing C:\...\artifacts\display-stream.bin for 20s
selftest: injected OK at 20 frames, 20224 events
selftest: events=47872 frames=48 resyncs=0
OK: display decoded live and buttons reach the firmware
```

Two platform lessons from this step, both now pinned by tests: a connection source needs a
property of the *concrete* `GPIO` type (`IGPIO` fails with `Error E13: Property 'IRQ' does
not exist ... or is not of the GPIO type`), and probe log paths must be absolute - Renode
resolves a probe's relative paths against **its own** installation root, which is why
`reset_probe.py` looked as though it had never loaded (`RESET_PROBE_LOADED` was sitting in
`%TEMP%\_renode\renode_1.17.0-portable\artifacts\reset-probe.log`).

## P25 - resolved: "no storage traffic" was an instrument failure; the card talks on SPI2

Two wrong conclusions in a row, both from the same class of mistake (trusting a probe that
had died or read a stale file), and both now have a sturdier instrument:

1. **The watchpoint probe was wrong.** `storage_probe.py` armed IronPython hooks on the SPI
   data registers; in the run where a card was actually presented it raised
   `Fatal error. System.AccessViolationException ... at
   DynamicScripting...ActionCallInstruction` 14 times, so its `SUMMARY` line never appeared -
   the "no flash/SD traffic on SPI1, SPI2 or QUADSPI" reading came from runs where no card
   was present at all, and was then quoted for the card case too.
2. **Two card-run/baseline comparisons read the same file.** The recorder file was locked by
   a stale `renode.exe` (P19), so the "baseline" run's `St7567Display` failed with
   `IOException: The process cannot access the file 'artifacts\display-stream.bin'` and wrote
   nothing; copying it produced two files with the same MD5
   (`4DB2BE9FDA33624FD16D8372B15A942C`), which briefly looked like "the emulation is
   deterministic". Check the digest for that IOException, or compare hashes, before believing
   an A/B.

The replacement instrument is `src/flipper_emu/platform/sd_stream_audit.py` (host-side, pinned
by `tests/test_sd_stream_audit.py`), parsing the recorder file the C# panel model writes.
Result with a card presented, 16 s run: **128 000 CMD0 frames** (`40 00 00 00 00 95`) on
SPI2, first at payload offset 80, histogram `00=513833 FF=256801 95=128002 40=128001`; with
the card-detect pin high (no card): 31 451 payload bytes and no CMD0 at all. The count is the
firmware's own `10 × 128 × 100` retry budget from `furi_hal_sd.c`.

Root cause underneath both: in 1.4.3 there is no internal flash volume. `/int/...` is an alias
to `/ext/.int/...` **on the SD card** (`STORAGE_INTERNAL_DIR_NAME ".int"` +
`storage_process_alias()`, `furi_assert(type == ST_EXT)`), the image has no `lfs_*` symbols,
and the SD card is a slave on the **panel's** bus (`furi_hal_spi_bus_handle_sd_slow/fast` use
`furi_hal_spi_bus_d` with CS `gpio_sdcard_cs` = PC12). So the missing peripheral for the
first-start slideshow is the microSD card, not a flash chip - see `BRINGUP_LOG.md` §20.

Measurement hygiene this produced, worth keeping: the audit prints the bytes *around* every
hit (pixel data contains `0x40`/`0x77` all the time), and any SPI2 A/B should be checked
against `artifacts/cd-pin-probe.log`, which shows which card-detect level the run actually
had (`IDR 0x…2840` = card, `0x…3C40` = no card).


## P26 - resolved: the microSD card mounts (DWT deltas and the DMA completion interrupt)

Symptom: the card answered every init command (CMD0/CMD8/CMD55/ACMD41/CMD58 = ready), the
firmware read sector 0 repeatedly, and `StorageExt` kept logging "init cycle N, error: not
mounted". `SdCardSpi` reported `514 queued byte(s) discarded - the host did not read them`
(512 data + 2 CRC, i.e. exactly one block payload after the 0xFE token had been taken).

Two independent causes, both measured, both in *our* platform rather than in the firmware:

1. **`dwt_fast.py` stepped CYCCNT by 2^26 on every read.** That was written to collapse
   `furi_hal_cortex_delay_us()` busy-waits, but `furi_hal_cortex_timer_get()` stores
   `start = CYCCNT` and `value = 64 * timeout_us`, and `is_expired()` compares
   `CYCCNT - start > value`. One read is 67,108,864 cycles, which already exceeds the
   64,000,000-cycle budget of a 1000 ms timeout, so **every** cortex timer looked expired
   after a single read. `sd_spi_wait_for_data(0xFE, 1000)` therefore read the data token,
   declared a timeout, deselected the card and purged the block it had just been handed
   (2 ms of emulated time, measured). Evidence: `sysbus LogPeripheralAccess spi2` around
   16:50:34.4267 shows the 0xFE read, then `Control1` rewritten with SPE cleared and the
   discard warning - and **zero writes to dma2** in the whole run, i.e. the SPI DMA path was
   never even reached.

2. **Renode's stock `DMA.STM32G0DMA` never raised the channel-6 completion interrupt.**
   With the counter fixed the firmware reached `furi_hal_spi_bus_trx_dma()`: DMA2 channel 6
   was programmed (`dma2 offset 0x6C write`, ch7 at 0x80 - the only rejected bits were the
   harmless priority bits), the block bytes did move (no discards), and then it logged
   `[E][FuriHalSpi] DMA timeout` and crashed: `[CRASH][StorageSrv] furi_check failed`,
   `lr = sd_device_read+0xCA` (the `furi_check` inside `sd_spi_read_bytes_dma`), twice =>
   `platform resets: 2` boot loop. `spi_dma_isr` only runs on that interrupt, and it is what
   releases the semaphore the caller waits on.

Fixes:

* `peripherals/cs/DwtWb55.cs` replaces the stub: CYCCNT advances 256 cycles (4 us) per read. A
  1 us step was tried first and is correct but slow - each read is a bus access, so the boot to
  first panel byte cost 13.1 s of wall time, which makes any run shorter than ~20 s look blank
  (`commands=0 data_bytes=0 frames=0`); 4 us cuts that to 3.2 s. Deltas stay below every timeout
  this firmware uses (the smallest is the ADC's ~20 us regulator stabilisation delay = 1280
  cycles) and `furi_delay_us(n)` costs n/256 reads. Renode's stock `Miscellaneous.DWT` was tried
  first and is honest but unusable here:
  it needs one clock entry per cycle, which capped the emulator at ~30 ms of emulated time
  per wall second, so the `furi_delay_us(4000000)` in `furi_hal_power_init()` (absent
  gauge) took minutes and boot never reached the storage service.
* `peripherals/cs/DmaWb55.cs` replaces the stock DMA model: channel-based WB55 DMA
  (0x14 stride, ISR/IFCR, CCR/CNDTR/CPAR/CMAR), one level interrupt per channel, transfers
  performed on `CCR.EN`, and a peripheral-to-memory channel advanced *in lockstep* with the
  enabled memory-to-peripheral channel because one SPI clock moves one byte each way. The
  first version drained the RX channel when it was enabled (the TRX path enables RX *before*
  TX), which handed FatFS 512 stale bytes and produced "sd init error: no filesystem".
* `generate.py` gained `DMA_CHANNEL_IRQS` (DMA1 = IRQ 11..17, DMA2 = 55..61 from the
  firmware's own vector table) so `5 -> nvic@60` and `6 -> nvic@61` wire the SPI2 RX/TX
  completions; `tests/test_irq_numbers.py` pins them.
* `platform/card_present.resc` (an `--renode-include` script) puts the card in the slot;
  a command with spaces cannot survive `Start-Process` quoting, which cost one debugging
  round.

Result: `[I][StorageExt] card mounted` at 1245 ms, 759 command frames, 67 sectors read,
0 resets, 0 crashes in a 24 s run, and the firmware then reads `/int/.notification.settings`
off the card (the image only carries `/.int/.slideshow`). Still missing for the slideshow
itself: card *writes* never complete (`0 sectors written`), so settings saves fail with
"internal error" and the slideshow file cannot be deleted; the panel stream for this run had
only 9 framebuffer states, i.e. the slideshow frames had not been drawn yet.

Instrument notes worth keeping:

* `--seconds` is **wall-clock** (`runner.run()` sleeps that long), so a run whose emulated
  boot is slow must be started detached (`Start-Process`) and polled; `Start-Job` dies with
  the shell that created it.
* The DMA channel register layout is a **0x14** stride; probing at 0x10 reads reserved holes,
  so channel 7 looks idle no matter what the firmware does (Renode answers with "Unhandled
  read from offset 0x68").
* `sysbus LogPeripheralAccess <peripheral>` is the only reliable way to see accesses to a
  *native* Renode model from the outside, and it prints register names - which is how the
  0x6C = `Channel6Configuration` mapping was confirmed.
* Renode's `Python.PythonPeripheral` has no `self.Machine` (only monitor scripts do), so a
  Python stub cannot read `ElapsedVirtualTime`; that pushed the DWT into C#.
