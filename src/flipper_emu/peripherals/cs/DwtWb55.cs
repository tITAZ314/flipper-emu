using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// Cortex-M4 data watchpoint and trace unit, of which this firmware only uses the
    /// free-running cycle counter <c>CYCCNT</c>.
    /// </summary>
    /// <remarks>
    /// The firmware gives CYCCNT two jobs with opposite requirements, and getting
    /// either wrong is silent:
    ///
    /// * <c>furi_hal_cortex_delay_us(n)</c> busy-waits until CYCCNT has advanced by
    ///   <c>64 * n</c> cycles (<c>FURI_HAL_CORTEX_INSTRUCTIONS_PER_MICROSECOND</c>),
    ///   so a counter that barely moves turns a delay into an indefinite spin;
    /// * <c>furi_hal_cortex_timer_get()</c> / <c>is_expired()</c> compare a CYCCNT
    ///   delta against <c>64 * timeout_us</c>, so a counter that jumps too far makes
    ///   every timeout look expired immediately.
    ///
    /// History, all three variants measured on this platform:
    ///
    /// * a stub that added 2^26 cycles per read made the delays instant but made the
    ///   firmware's first <c>sd_spi_wait_for_data()</c> (1000 ms = 64,000,000 cycles)
    ///   report a timeout one read after seeing the data token, so the card was
    ///   deselected with the 514-byte block payload still unread and the microSD never
    ///   mounted (docs/ISSUES_AND_LOGS.md, P21);
    /// * Renode's stock <c>Miscellaneous.DWT</c> counts honestly, but it needs one
    ///   clock entry per cycle: 64 MHz of events caps the emulator at roughly 30 ms of
    ///   emulated time per wall-clock second, so a single
    ///   <c>furi_delay_us(4000000)</c> in <c>furi_hal_power_init()</c> took minutes and
    ///   boot never reached the storage service;
    /// * a counter that never moves spins those same delay loops forever.
    ///
    /// So CYCCNT advances by 256 cycles (4 us) per *read*.  That keeps every reader
    /// honest where it matters - a delta is never larger than 4 us, while the smallest
    /// timeout this firmware uses is the ADC's internal regulator stabilisation delay
    /// (LL_ADC_DELAY_INTERNAL_REGUL_STAB_US ~ 20 us = 1280 cycles) - and it makes
    /// <c>furi_delay_us(n)</c> cost n/256 reads instead of n cycles of events, so the
    /// 4-second wait above becomes ~1 M reads rather than minutes.
    ///
    /// The step size is a deliberate compromise, because each read is a bus access and
    /// therefore costs host time (measured ~1-3 M reads/s on this machine):
    /// <c>furi_delay_us()</c> wants the counter to move fast, while the cortex timers
    /// want its deltas to stay small.  4 us sits five times below the tightest timeout
    /// in the firmware and cuts the boot's wall-clock cost about fourfold compared to a
    /// 1 us step - which matters because the GUI's first panel byte cannot be drawn
    /// until <c>furi_hal_power_init()</c>'s absent-gauge delays have finished.
    ///
    /// Writes to CYCCNT (the init zeroing) are ignored on purpose: every reader in this
    /// firmware compares deltas, and unsigned 32-bit subtraction makes the wrap
    /// harmless.
    /// </remarks>
    public class DwtWb55 : BasicDoubleWordPeripheral, IKnownSize
    {
        /// <summary>CTRL 0x00.</summary>
        private const long ControlRegister = 0x00;

        /// <summary>CYCCNT 0x04.</summary>
        private const long CycleCounterRegister = 0x04;

        /// <summary>CYCCNTENA, bit 0 of DWT_CTRL (stm32wb55xx.h).</summary>
        private const uint CycleCounterEnableBit = 1u << 0;

        /// <summary>CYCCNT is present: the firmware's init ORs in CYCCNTENA.</summary>
        private const uint ResetControl = CycleCounterEnableBit;

        /// <summary>
        /// CYCCNT step per read: 4 us of emulated time (64 cycles per microsecond at
        /// 64 MHz).  The two readers want opposite things - `furi_delay_us()` needs the
        /// counter to move, the cortex timers need its deltas to stay below 64 *
        /// timeout_us - so the step stays five times below the smallest timeout the
        /// firmware uses (the ADC's ~20 us regulator stabilisation delay).
        /// </summary>
        private const uint CyclesPerRead = 4 * 64;

        private uint control;
        private uint cycles;
        private long reads;

        public DwtWb55(IMachine machine) : base(machine)
        {
            Reset();
        }

        public long Size => 0x1000;

        /// <summary>CYCCNT reads served so far (for the bring-up log and probes).</summary>
        public long CycleCounterReads
        {
            get { return reads; }
        }

        public override void Reset()
        {
            control = ResetControl;
            cycles = 0;
            reads = 0;
        }

        public override uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case ControlRegister:
                    return control;
                case CycleCounterRegister:
                    reads++;
                    cycles += CyclesPerRead;
                    return cycles;
                default:
                    // CPI (0x08) and the watchpoint/ comparator registers are unused;
                    // the firmware only reads CTRL and CYCCNT.
                    this.NoisyLog("DWT: read of unused register 0x{0:X}", offset);
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case ControlRegister:
                    control = value;
                    return;
                case CycleCounterRegister:
                    this.NoisyLog("DWT: write of 0x{0:X8} to CYCCNT ignored (deltas only)", value);
                    return;
                default:
                    this.NoisyLog("DWT: write of 0x{0:X8} to unused register 0x{1:X}", value, offset);
                    return;
            }
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            return string.Format("DWT CTRL=0x{0:X8} CYCCNT=0x{1:X8} reads={2}", control, cycles, reads);
        }
    }
}
