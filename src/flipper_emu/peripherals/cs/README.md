# C# peripheral models

Models that need a Renode *interface* rather than just a register window live
here. Python peripherals (`../python/`) cover register-file stubs, but they
cannot be bus-attached devices: `Python.PythonPeripheral` implements only
`IByte/Word/DoubleWord/QuadWordPeripheral` — **no** `IGPIOReceiver`,
`INumberedGPIOOutput` or `ISpiPeripheral` (verified against the shipped
assembly). So GPIO consumers/producers and SPI slaves must be C#.

| File | Status | Why C# is required |
|---|---|---|
| `Wb55Exti.cs` | **done** | GPIO ports connect into EXTI (`[0-15] -> exti@[0-15]`); the WB55 layout (`RTSR1 0x00 … PR1 0x0C … IMR1 0x80 … IMR2 0x90`) differs from Renode's `STM32F4_EXTI`, so lines could never be enabled |
| `GpioWb55Port.cs` | **done** | one port feeds EXTI *and* must accept injected board levels (`IGPIOReceiver`); Renode's `GPIOPort.STM32_GPIOPort` reads every input as 0 and ignores injections, which kept the 1.4.3 bootloader in DFU |
| `HsemWb55.cs` | **done** | the hardware semaphores the wireless bring-up depends on: `furi_hal_bt_init` verifies (without taking) that CPU1 holds one, and a zero-returning stub made that a reboot loop |
| `RngWb55.cs` | **done** | `CR`/`SR`/`DR` with `DRDY`; the stub returned 1 for every read, so all random values were constant |
| `St7567Display.cs` | **done** | 128×64 panel is an SPI slave (`ISpiPeripheral`) |
| `FlipperSdCard.cs` | planned | microSD over SPI2 |

## Build

```powershell
dotnet build -c Release -p:RenodeDir=<renode install> src\flipper_emu\peripherals\cs\FlipperEmu.Peripherals.csproj
```

`RenodeDir` defaults to `tools\renode` (where `fetch_renode.ps1` installs
Renode); the reference is Renode's own `Infrastructure.dll` and the target
framework tracks its runtime (`net8.0` for Renode 1.17).

## How a custom C# peripheral gets loaded (worked out the hard way)

Renode does **not** load an assembly just because it exists, and its `include`
command does not load assemblies at all (it tries to parse them as a script). The
recipe that works on Renode 1.17.0:

1. The assembly must contain a class carrying Renode's plugin attribute
   (`AssemblyInfo.cs` does this:
   `[Plugin(Name = "FlipperEmu.Peripherals", …)]`). The attribute is valid on
   classes only, not on assemblies.
2. The built DLL must sit **beside `renode.exe`** — that is where Renode scans
   for plugins (its own `SampleCommandPlugin`, `tracer`, `Wireshark Plugin`
   live there).
3. The plugin must be **enabled at startup**, via the config's
   `[plugins] enabled-plugins = FlipperEmu.Peripherals`. `flipper_emu.runner`
   generates that config (copying the user's own) and passes `--config`, so the
   Renode installation itself is never modified.
4. **The peripheral type must live under an `Antmicro.Renode.*` namespace.**
   Renode's type manager only records types whose namespace matches its own
   prefixes, so `FlipperEmu.Peripherals.Wb55Exti` is visible in the "available
   peripherals" list yet can never be resolved by a platform file, while
   `Antmicro.Renode.Peripherals.FlipperEmu.Wb55Exti` resolves. Our models
   therefore live in `Antmicro.Renode.Peripherals.FlipperEmu`, which keeps them
   clearly separated from Renode's own code.

`flipper_emu.runner` performs steps 1–3 automatically:
`install_plugin()` copies the built DLL next to `renode.exe` and
`write_renode_config()` enables it; if the model has not been built, the platform
falls back to the stub for that block.

## Reading Renode's API before writing a model

`Infrastructure.dll` is the API contract, and its metadata can be read locally
without a decompiler — the repository's throwaway `tools`-style reflection probe
used Renode's own `Mono.Cecil.dll` to print a type's base class, interfaces and
member signatures. That is how the exact constructor, `OnGPIO` signature and
`Connections` property for this model were obtained.

