# Peripheral coverage

Generated view of the emulated bus. Regenerate with
`py -3.9 -m flipper_emu bus`; the machine-readable form is
`platform/bus_map.json`.

Status legend:

* **modelled** — an existing Renode model drives the block.
* **stub** — a model from `src/flipper_emu/peripherals/python/` (or an inline
  safe-default script) answers the block so the firmware can make progress.
  Stubs are listed here on purpose: they are the work queue.

Current totals: **49 register blocks — 27 modelled, 22 stubbed**
(plus 6 memory regions and the CPU/NVIC pair).

## Memory

| Region | Address | Size | Notes |
|---|---|---|---|
| `flash` | `0x08000000` | 1 MiB | also **aliased at `0x00000000`**, which is where the Cortex-M model fetches its initial SP/PC: an STM32 boots with flash mapped low |
| `sram1` | `0x20000000` | 192 KiB | |
| `sram2a` | `0x20030000` | 32 KiB | IPCC/FUS shared area |
| `sram2b` | `0x20038000` | 32 KiB | wireless-stack image |
| `sysinfo` | `0x1FFF0000` | 32 KiB | system memory + OTP + UID/flash-size registers |
| `optionBytes` | `0x1FFF8000` | 4 KiB | also serves as the flash controller's "eeprom" binding |

## Modelled by Renode

CPU (`cortex-m4`), `NVIC` (+SysTick at 64 MHz), `GPIOA…GPIOE/GPIOH`,
`SPI1/SPI2`, `I2C1/I2C3`, `USART1`, `LPUART1`, `TIM1/TIM2/TIM16/TIM17`,
`LPTIM1/LPTIM2`, `RTC`, `IWDG`, `DMA1/DMA2`, `CRC`, `ADC1`, `EXTI`,
and the flash controller (`MTD.STM32L0_FlashController`, whose WB55 register
layout matches).

## Stubbed (the work queue)

| Block | Address | Stub | What it needs next |
|---|---|---|---|
| `GPIOA…GPIOE/GPIOH` | `0x48000000` | **ours** | **implemented as a C# model** (`peripherals/cs/GpioWb55Port.cs`): `MODER/OTYPER/OSPEEDR/PUPDR/IDR/ODR/BSRR/LCKR/AFRL/AFRH/BRR`, pull-up/down applied to undriven inputs, `IDR` fed by injected levels, and one numbered output per pin for EXTI. Needed because Renode's `GPIOPort.STM32_GPIOPort` cannot make an input read high (that is what kept the firmware in its DFU splash — see `docs/BRINGUP_LOG.md` §14–15) |
| `EXTI` | `0x58000800` | **ours** | **implemented as a C# model** (`peripherals/cs/Wb55Exti.cs`): WB55 register map from `stm32wb55xx.h` (`RTSR1 0x00 … PR1 0x0C … IMR1 0x80 … IMR2 0x90`), GPIO inputs wired to the NVIC outputs, write-1-to-clear pending, software interrupts, and masked lines deliberately not latching (documented deviation). Needed because a Python peripheral cannot be an `IGPIOReceiver`, and Renode's `STM32F4_EXTI` has an incompatible layout — with it, no EXTI line could ever be enabled |
| `LCD` | `0x40002400` | inline | unused by stock firmware |
| `WWDG` | `0x40002C00` | inline | |
| `CRS` | `0x40006000` | inline | unused |
| `USB1` | `0x40006800` | inline | USB CDC console (not needed to boot) |
| `SYSCFG` | `0x40010000` | `syscfg_wb55` | EXTICR routing + `MEMRMP` flash remap (needed by the OTA updater) |
| `VREFBUF` | `0x40010030` | inline | |
| `COMP1`/`COMP2` | `0x40010200`, `0x40010204` | `comp_wb55` | 125 kHz RFID demodulator |
| `SAI1` | `0x40015400` | inline | unused |
| `DMAMUX1` | `0x40020800` | inline | DMA request routing |
| `TSC` | `0x40024000` | inline | unused |
| `AES1`/`AES2` | `0x50060000`, `0x58001800` | inline | |
| `PWR` | `0x58000400` | `pwr_wb55` | CPU2 boot (`C2BOOT`), low-power modes |
| `IPCC` | `0x58000C00` | `ipcc_wb55` | fake the FUS/"stack installed" handshake so `furi_hal_bt` stops failing |
| `RNG` | `0x58001000` | **ours** | **implemented as a C# model** (`peripherals/cs/RngWb55.cs`): `CR 0x00`/`SR 0x04`/`DR 0x08`, `RNGEN` gating `DRDY`, xorshift32 data from a platform-file seed (deterministic on purpose, explicitly not entropy). It replaced a stub that returned 1 for every read, so every seed and BLE key byte was constant |
| `RNG` | `0x58001000` | inline | |
| `HSEM` | `0x58001400` | **ours** | **implemented as a C# model** (`peripherals/cs/HsemWb55.cs`): `R_i = 4*i` (take/release) and `RLR_i = 0x80 + 4*i` (owner/lock view), `LOCK` bit 31 and `COREID` bits 11:8, both read out of the firmware's own accesses. It also carries `bootChainLockMask`, a labelled stand-in for the boot loader this emulator does not run - the application *verifies* (without taking) that CPU1 holds the CLK48 semaphore in `furi_hal_bt_init`, and with a zero-returning stub it crashed 555 times per 18 s instead of running (see `docs/BRINGUP_LOG.md` §16) |
| `PKA` | `0x58002000` | inline | |
| `QUADSPI` | `0xA0001000` | inline | not used by Flipper Zero |
| `DWT` | `0xE0001000` | `dwt_fast` | **implemented**: `CYCCNT` advances fast so the firmware's `furi_delay_us` busy-waits (up to 4 s for the battery-gauge probe) return immediately |
| `DBGMCU` | `0xE0042000` | inline | |
| `RCC` | `0x58000000` | `rcc_wb55` | **implemented**: WB55 register map, instant clock-ready bits, SW→SWS coherence, reset-flag clearing. Renode's L0 RCC broke clock-enable read-back and caused 3192 reboots in 25 s |

## Known-benign noise in the digest

These warnings appear in every run but do not block progress; they are listed so
they are not mistaken for real blockers:

* `gpioPortA offset 0x04 write` — `OTYPER` (open-drain) is not modelled by
  `STM32_GPIOPort`. Nothing in this emulation depends on open-drain.
* `nvic offset 0xDFC write` — `DEMCR.TRCENA`, written once per boot before the
  firmware enables the `DWT` cycle counter (our stub handles the counter itself).
* `spi2 offset 0x04 write` — Renode's F4-series `STM32SPI` does not implement
  `CR2` bits `DS[8-10]` and `FRXTH`; the firmware writes them during
  `LL_SPI_Init`. Data size is 8 bits either way, so this is cosmetic.
* `i2c1 offset 0x10 write` — similarly, `TIMINGR` is not implemented by
  `STM32F7_I2C`. The firmware re-initialises the bus per transaction, so this
  fires often; it does not affect whether a transfer happens.
* `exti offset 0x80` — **fixed**: the WB55 `IMR1`/`EMR1` layout mismatch is gone
  now that `Antmicro.Renode.Peripherals.FlipperEmu.Wb55Exti` (our C# model) drives
  the block; see `docs/BRINGUP_LOG.md` §6.

## Measured bus map (from instrumented runs)

Two "retry loops" in the digest were chased down and turned out to be normal
firmware behaviour, not blockers:

| Bus | Owner (symbolised) | Traffic observed | Conclusion |
|---|---|---|---|
| `SPI2` | `u8x8_hw_spi_stm32` → `furi_hal_spi_bus_tx` | ST7567 init sequence (`E2 A2 A0 C8 40 25 81 20 2F A4 AF`) then page/column addressing with pixel data; `SR` returns TXE→TXE\|RXNE | **This is the LCD**, and the GUI is drawing. Missing piece: the panel model (`St7567Display.cs`), which is what makes frames visible |
| `I2C1` | `furi_hal_light_init` → `furi_hal_i2c_handle_power_event` (external bus handle) | init/deinit only: `CR1`, `TIMINGR=0x10707DBC`, `OAR1/OAR2`, `CR2=0x02000000`; **no** `START`, address, `TXDR`/`RXDR` or `ISR` | **LED driver (LP5562) with no device present**; the driver short-circuits before transferring. Needs an absent-device answer, not register work |

Note that the emulated flash persists firmware writes, so first-boot and
later-boot runs legitimately differ (a fresh run re-initialises the panel
thousands of times per second; a later run draws frames steadily with light
polling). Compare digests accordingly.

