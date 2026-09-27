using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 true random number generator (RNG), the small register file the
    /// firmware polls when it wants random bytes.
    /// </summary>
    /// <remarks>
    /// Why this replaced a stub: the platform used to serve `0x58001000` with
    /// `script: "if request.IsRead: request.Value = 0x00000001"`, i.e. every read
    /// returned 1 - `RNG_SR` therefore always looked "ready" (DRDY set, bit 0) and
    /// `RNG_DR` always returned the same word. `furi_hal_random_get()` and
    /// `furi_hal_random_fill_buf()` use it for seeds and BLE key material, so every
    /// value the firmware saw was constant.
    ///
    /// Register map (RNG_TypeDef, stm32wb55xx.h):
    ///
    /// <code>
    /// CR 0x00   RNGEN (bit 2, enable), IE (bit 3), CED (bit 5)
    /// SR 0x04   DRDY (bit 0), CECS (bit 1), SECS (bit 2), CEIS (bit 3), SEIS (bit 4)
    /// DR 0x08   data
    /// </code>
    ///
    /// Behaviour, matching what the firmware's reader expects
    /// (`targets/f7/furi_hal/furi_hal_random.c`): while `CR.RNGEN` is set, `SR.DRDY`
    /// reads as set and each `DR` read returns the next word (and re-arms `DRDY`, so
    /// the poll always passes). `SR` is write-1-to-clear for the two error interrupts
    /// the firmware clears (CEIS/SEIS); the clock and noise-source error conditions
    /// themselves are not modelled, so those flags never latch.
    ///
    /// The generator is a xorshift32 seeded from the platform file, i.e.
    /// deterministic across runs and explicitly not a source of entropy - it exists
    /// so the firmware gets changing values instead of a constant, and so runs stay
    /// reproducible. A `seed` of 0 starts from a fixed non-zero default.
    /// </remarks>
    public class RngWb55 : BasicDoubleWordPeripheral, IKnownSize
    {
        // Register offsets (RNG_TypeDef, stm32wb55xx.h).
        private const long ControlRegister = 0x00;
        private const long StatusRegister = 0x04;
        private const long DataRegister = 0x08;

        /// <summary>CR: random number generator enable.</summary>
        public const uint EnableBit = 1u << 2;

        /// <summary>SR: data ready.</summary>
        public const uint DataReadyBit = 1u << 0;

        /// <summary>SR: clock error current status.</summary>
        public const uint ClockErrorBit = 1u << 1;

        /// <summary>SR: seed error current status.</summary>
        public const uint SeedErrorBit = 1u << 2;

        /// <summary>SR: clock error interrupt status (write 1 to clear).</summary>
        public const uint ClockErrorInterruptBit = 1u << 3;

        /// <summary>SR: seed error interrupt status (write 1 to clear).</summary>
        public const uint SeedErrorInterruptBit = 1u << 4;

        private const uint DefaultSeed = 0x12345678;

        private uint control;
        private uint status;
        private uint state;
        private bool enabledLogged;
        private long wordsRead;

        public RngWb55(IMachine machine, uint seed = 0) : base(machine)
        {
            state = seed == 0 ? DefaultSeed : seed;
        }

        public long Size => 0x400;

        /// <summary>Words handed out so far (for the bring-up log and probes).</summary>
        public long WordsRead
        {
            get { return wordsRead; }
        }

        public override void Reset()
        {
            control = 0;
            status = 0;
            enabledLogged = false;
            wordsRead = 0;
            state = state == 0 ? DefaultSeed : state;
        }

        public override uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case ControlRegister:
                    return control;
                case StatusRegister:
                    return status;
                case DataRegister:
                    return ReadData();
                default:
                    this.NoisyLog("RNG: unimplemented read at 0x{0:X}", offset);
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case ControlRegister:
                    control = value;
                    UpdateDataReady();
                    if((value & EnableBit) != 0 && !enabledLogged)
                    {
                        enabledLogged = true;
                        this.InfoLog("RNG enabled (CR=0x{0:X8}); data is deterministic from seed 0x{1:X8}",
                                     value, state);
                    }
                    return;
                case StatusRegister:
                    // CEIS/SEIS are write-1-to-clear; the error conditions themselves
                    // never latch in this model, so only the clear is honoured.
                    status &= ~(value & (ClockErrorInterruptBit | SeedErrorInterruptBit));
                    return;
                case DataRegister:
                    this.WarningLog("RNG: write of 0x{0:X8} to read-only DR ignored", value);
                    return;
                default:
                    this.NoisyLog("RNG: unimplemented write at 0x{0:X} value 0x{1:X8}", offset, value);
                    return;
            }
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            return string.Format(
                "RNG CR=0x{0:X8} SR=0x{1:X8} words_read={2}", control, status, wordsRead);
        }

        private uint ReadData()
        {
            if((control & EnableBit) == 0)
            {
                // Nothing to hand out while the generator is off; real silicon would
                // flag a clock/seed error, which the firmware clears and retries.
                return 0;
            }

            var value = NextWord();
            wordsRead++;
            // Reading DR clears DRDY on silicon; the generator re-arms it right away
            // because it is still enabled, which is what the firmware's poll expects.
            UpdateDataReady();
            return value;
        }

        private void UpdateDataReady()
        {
            if((control & EnableBit) != 0)
            {
                status |= DataReadyBit;
            }
            else
            {
                status &= ~DataReadyBit;
            }
        }

        /// <summary>xorshift32: deterministic, explicitly not cryptographic.</summary>
        private uint NextWord()
        {
            var value = state;
            value ^= value << 13;
            value ^= value >> 17;
            value ^= value << 5;
            state = value == 0 ? DefaultSeed : value;
            return state;
        }
    }
}
