using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 EXTI (extended interrupt/event controller).
    /// </summary>
    /// <remarks>
    /// Why this model exists instead of Renode's <c>IRQControllers.STM32F4_EXTI</c>:
    /// the WB55 register map is different, and the difference is not cosmetic.
    ///
    /// <code>
    /// WB55:  RTSR1 0x00  FTSR1 0x04  SWIER1 0x08  PR1 0x0C  IMR1 0x80  EMR1 0x84
    ///        RTSR2 0x20  FTSR2 0x24  SWIER2 0x28  PR2 0x2C  IMR2 0x90  EMR2 0x94
    /// F4:    IMR   0x00  EMR   0x04  RTSR   0x08  FTSR  0x0C  SWIER 0x10  PR 0x14
    /// </code>
    ///
    /// The firmware configures triggers through <c>RTSR1/FTSR1</c> (which the F4
    /// model reads as its interrupt mask) and enables interrupts through
    /// <c>IMR1</c> at 0x80 (which the F4 model treats as a pending bank of lines
    /// 96 and up). No line ever becomes enabled, so the firmware's level/wakeup
    /// path times out and resets the chip - measured as thousands of
    /// <c>SYSRESETREQ</c>s per session.
    ///
    /// The offsets above come from ST's CMSIS device header (<c>EXTI_TypeDef</c>
    /// in <c>stm32wb55xx.h</c>).
    ///
    /// Latching rule: an edge only becomes pending when the line is enabled in
    /// <c>IMR</c> or <c>EMR</c>. Real silicon latches regardless of the mask, but
    /// a masked pending bit can never be cleared by the firmware (it clears
    /// <c>PR</c> while handling the interrupt, and a masked line has no handler),
    /// which leaves the line permanently asserted and live-locks the input
    /// service. This is a deliberate, documented deviation.
    /// </remarks>
    public class Wb55Exti : BasicDoubleWordPeripheral, IGPIOReceiver, INumberedGPIOOutput, IKnownSize
    {
        // Register offsets (EXTI_TypeDef, stm32wb55xx.h).
        private const long Rtsr1 = 0x00;
        private const long Ftsr1 = 0x04;
        private const long Swier1 = 0x08;
        private const long Pr1 = 0x0C;
        private const long Rtsr2 = 0x20;
        private const long Ftsr2 = 0x24;
        private const long Swier2 = 0x28;
        private const long Pr2 = 0x2C;
        private const long Imr1 = 0x80;
        private const long Emr1 = 0x84;
        private const long Imr2 = 0x90;
        private const long Emr2 = 0x94;
        private const long C2Imr1 = 0xC0;
        private const long C2Emr1 = 0xC4;
        private const long C2Imr2 = 0xD0;
        private const long C2Emr2 = 0xD4;

        /// <summary>EXTI lines the WB55 exposes (0-31 in bank 1, 32-43 in bank 2).</summary>
        public const int LineCount = 44;

        /// <summary>First line of bank 2; bank 1 holds lines 0..31.</summary>
        private const int BankBoundary = 32;

        private readonly uint[] rising = new uint[2];
        private readonly uint[] falling = new uint[2];
        private readonly uint[] pending = new uint[2];
        private readonly uint[] interruptMask = new uint[2];
        private readonly uint[] eventMask = new uint[2];
        private readonly uint[] cpu2InterruptMask = new uint[2];
        private readonly uint[] cpu2EventMask = new uint[2];
        private readonly bool[] level = new bool[LineCount];
        private readonly Dictionary<int, IGPIO> connections = new Dictionary<int, IGPIO>();

        public Wb55Exti(IMachine machine, int numberOfLines = LineCount) : base(machine)
        {
            if(numberOfLines < 1 || numberOfLines > LineCount)
            {
                throw new ArgumentOutOfRangeException(nameof(numberOfLines));
            }

            NumberOfLines = numberOfLines;
            for(var line = 0; line < LineCount; line++)
            {
                // Every line gets an output GPIO; the platform file connects the
                // ones the firmware uses to the NVIC (directly or via the groups).
                connections[line] = new GPIO();
            }
        }

        public long Size => 0x400;

        public long NumberOfLines { get; }

        public IReadOnlyDictionary<int, IGPIO> Connections => connections;

        public override void Reset()
        {
            Array.Clear(rising, 0, rising.Length);
            Array.Clear(falling, 0, falling.Length);
            Array.Clear(pending, 0, pending.Length);
            Array.Clear(interruptMask, 0, interruptMask.Length);
            Array.Clear(eventMask, 0, eventMask.Length);
            Array.Clear(cpu2InterruptMask, 0, cpu2InterruptMask.Length);
            Array.Clear(cpu2EventMask, 0, cpu2EventMask.Length);
            Array.Clear(level, 0, level.Length);
            for(var line = 0; line < LineCount; line++)
            {
                connections[line].Set(false);
            }
        }

        /// <summary>Receives a pin level from one of the GPIO ports.</summary>
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= NumberOfLines || level[number] == value)
            {
                return;
            }

            var wasHigh = level[number];
            level[number] = value;

            var bank = Bank(number);
            var bit = Bit(number);
            var triggered = (value && !wasHigh && (rising[bank] & bit) != 0)
                            || (!value && wasHigh && (falling[bank] & bit) != 0);
            if(!triggered)
            {
                return;
            }

            // See the class remarks: masked lines deliberately do not latch.
            if(((interruptMask[bank] | eventMask[bank]) & bit) == 0)
            {
                return;
            }

            pending[bank] |= bit;
            UpdateOutput(number);
        }

        public override uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case Rtsr1: return rising[0];
                case Ftsr1: return falling[0];
                case Pr1: return pending[0];
                case Rtsr2: return rising[1];
                case Ftsr2: return falling[1];
                case Pr2: return pending[1];
                case Imr1: return interruptMask[0];
                case Emr1: return eventMask[0];
                case Imr2: return interruptMask[1];
                case Emr2: return eventMask[1];
                case C2Imr1: return cpu2InterruptMask[0];
                case C2Emr1: return cpu2EventMask[0];
                case C2Imr2: return cpu2InterruptMask[1];
                case C2Emr2: return cpu2EventMask[1];
                default:
                    // SWIER is write-only on hardware; everything else here is a
                    // reserved window that the firmware may probe.
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case Rtsr1: rising[0] = value; return;
                case Ftsr1: falling[0] = value; return;
                case Swier1: pending[0] |= value; RefreshAll(); return;
                case Pr1: pending[0] &= ~value; RefreshAll(); return;
                case Rtsr2: rising[1] = value; return;
                case Ftsr2: falling[1] = value; return;
                case Swier2: pending[1] |= value; RefreshAll(); return;
                case Pr2: pending[1] &= ~value; RefreshAll(); return;
                case Imr1: interruptMask[0] = value; RefreshAll(); return;
                case Emr1: eventMask[0] = value; RefreshAll(); return;
                case Imr2: interruptMask[1] = value; RefreshAll(); return;
                case Emr2: eventMask[1] = value; RefreshAll(); return;
                case C2Imr1: cpu2InterruptMask[0] = value; return;
                case C2Emr1: cpu2EventMask[0] = value; return;
                case C2Imr2: cpu2InterruptMask[1] = value; return;
                case C2Emr2: cpu2EventMask[1] = value; return;
                default:
                    this.NoisyLog("unimplemented EXTI write at 0x{0:X} value 0x{1:X}", offset, value);
                    return;
            }
        }

        private void RefreshAll()
        {
            for(var line = 0; line < NumberOfLines; line++)
            {
                UpdateOutput(line);
            }
        }

        /// <summary>Drives the line's output GPIO: NVIC sees pending AND unmasked.</summary>
        private void UpdateOutput(int line)
        {
            var bank = Bank(line);
            var bit = Bit(line);
            connections[line].Set((pending[bank] & interruptMask[bank] & bit) != 0);
        }

        private static int Bank(int line)
        {
            return line < BankBoundary ? 0 : 1;
        }

        private static uint Bit(int line)
        {
            return 1u << (line - (Bank(line) * BankBoundary));
        }


    }
}
