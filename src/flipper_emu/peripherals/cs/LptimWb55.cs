using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 low-power timer (LPTIM) - the timer the firmware's FreeRTOS port uses
    /// as its tickless-idle wakeup (LPTIM1).
    /// </summary>
    /// <remarks>
    /// Why this replaced Renode's <c>Timers.STM32L0_LpTimer</c>: the firmware arms the
    /// timer one-shot with a *compare* value, masks interrupts around the sleep and then
    /// polls the flags rather than servicing an interrupt
    /// (<c>targets/f7/furi_hal/furi_hal_idle_timer.h</c>, <c>furi_hal_os.c:
    /// vPortSuppressTicksAndSleep</c>):
    ///
    /// <code>
    /// __disable_irq();
    /// LL_LPTIM_Enable(); while(!LL_LPTIM_IsEnabled());
    /// LL_LPTIM_EnableIT_CMPM();
    /// LL_LPTIM_SetCompare(count - 3); LL_LPTIM_SetAutoReload(count);
    /// LL_LPTIM_StartCounter(ONE_SHOT);
    /// furi_hal_power_sleep();                  // WFI
    /// after_cnt = CNT + skew + 3;              // assumes CNT wrapped/stopped at 0
    /// if(ISR.CMPM &amp;&amp; ISR.ARRM) after_tick += expected_idle_ticks;
    /// furi_hal_idle_timer_reset();             // RCC reset + NVIC_ClearPendingIRQ
    /// </code>
    ///
    /// The stock model fired on its own <c>LimitTimer</c> limit instead of the
    /// firmware's compare match ("Compare value (16117) cannot be greater than auto
    /// reload limit (1). Compare value will be ignored"), so it raised IRQ 47 at the
    /// wrong time and repeatedly; the firmware registers no ISR for that interrupt, so
    /// delivery ended in <c>furi_check(isr_descr->isr)</c> inside
    /// <c>furi_hal_interrupt_call</c> - 96 resets in 20 s, crash dump at
    /// <c>LPTIM1_IRQHandler+0x12</c>. See docs/ISSUES_AND_LOGS.md (P24).
    ///
    /// Register map (<c>LPTIM_TypeDef</c>, stm32wb55xx.h): ISR 0x00 (CMPM/ARRM/CMPOK/
    /// ARROK), ICR 0x04 (write 1 to clear), DIER 0x08 (CMPMIE/ARRMIE), CFGR 0x0C
    /// (PRESC), CR 0x10 (ENABLE/SNGSTRT/CNTSTRT), CMP 0x14, ARR 0x18, CNT 0x1C.
    ///
    /// * the counter runs at the LSE rate the firmware selects for the idle timer -
    ///   32768 Hz (<c>FURI_HAL_IDLE_TIMER_CLK_HZ</c>) - divided by <c>CFGR.PRESC</c>;
    /// * one-shot: reaching <c>CMP</c> sets <c>CMPM</c>, continuing to <c>ARR</c> sets
    ///   <c>ARRM</c>, then the counter stops and reads 0 (the firmware's tick
    ///   arithmetic only balances if a completed one-shot leaves <c>CNT</c> at 0);
    /// * the interrupt is level-based on <c>ISR &amp; DIER</c>, released by an
    ///   <c>ICR</c> write, by <c>CR.ENABLE = 0</c>, or by the RCC reset the firmware
    ///   performs - that last one is what completes the handshake, so this model
    ///   watches <c>RCC_APB1RSTR1.LPTIM1RST</c> / <c>APB1RSTR2.LPTIM2RST</c> on the bus
    ///   (the only way to model a reset line here: the RCC is an IronPython stub with
    ///   no handle on other peripherals - verified, PythonPeripheral exposes only Size
    ///   and Code).
    ///
    /// Not modelled (this firmware never reads them): CFGR clock-select/trigger input,
    /// encoder mode, CFGR2 filtering, UP/DOWN direction flags.
    /// </remarks>
    public class LptimWb55 : BasicDoubleWordPeripheral, INumberedGPIOOutput, IKnownSize
    {
        // Register offsets (LPTIM_TypeDef, stm32wb55xx.h).
        private const long InterruptAndStatus = 0x00;
        private const long InterruptClear = 0x04;
        private const long InterruptEnable = 0x08;
        private const long Configuration = 0x0C;
        private const long Control = 0x10;
        private const long CompareRegister = 0x14;
        private const long AutoReloadRegister = 0x18;
        private const long CounterRegister = 0x1C;
        private const long Configuration2 = 0x24;

        /// <summary>ISR/ICR/DIER bit: compare match.</summary>
        public const uint CompareMatchBit = 1u << 0;

        /// <summary>ISR/ICR/DIER bit: auto-reload match.</summary>
        public const uint AutoReloadMatchBit = 1u << 1;

        /// <summary>ISR/ICR bit: compare write synchronised (set on CMP writes).</summary>
        public const uint CompareOkBit = 1u << 3;

        /// <summary>ISR/ICR bit: auto-reload write synchronised (set on ARR writes).</summary>
        public const uint AutoReloadOkBit = 1u << 4;

        /// <summary>CR bit 0: counter enable.</summary>
        private const uint EnableBit = 1u << 0;

        /// <summary>CR bit 1: start a one-shot count.</summary>
        private const uint SingleStartBit = 1u << 1;

        /// <summary>CR bit 2: start a continuous count.</summary>
        private const uint ContinuousStartBit = 1u << 2;

        /// <summary>CFGR bits 2:0: clock prescaler (1 &lt;&lt; PRESC).</summary>
        private const uint PrescalerMask = 0x7u;

        /// <summary>Counter range: <c>FURI_HAL_IDLE_TIMER_MAX</c> (16-bit).</summary>
        private const uint CounterMax = 0xFFFFu;

        /// <summary>RCC_APB1RSTR1 (WB55): LPTIM1 reset line, bit 31.</summary>
        private const ulong Apb1ResetRegister1 = 0x58000038;

        /// <summary>RCC_APB1RSTR2 (WB55): LPTIM2 reset line, bit 5.</summary>
        private const ulong Apb1ResetRegister2 = 0x5800003C;

        private readonly ulong frequency;
        private readonly int index;
        private readonly ulong resetRegister;
        private readonly LimitTimer compareTimer;
        private readonly LimitTimer reloadTimer;
        private readonly GPIO irq = new GPIO();
        private readonly Dictionary<int, IGPIO> connections = new Dictionary<int, IGPIO>();

        private uint status;
        private uint interruptEnable;
        private uint configuration;
        private uint configuration2;
        private uint control;
        private uint compare;
        private uint autoReload;
        private bool running;
        private bool continuous;
        private bool resetLineAsserted;
        private bool startLogged;
        private bool compareAboveAutoReloadLogged;
        private long starts;
        private long compareMatches;
        private long autoReloadMatches;

        /// <param name="index">1 for LPTIM1, 2 for LPTIM2 - selects the RCC reset line
        /// this instance watches (APB1RSTR1 bit 31, APB1RSTR2 bit 5).</param>
        public LptimWb55(IMachine machine, ulong frequency = 32768, int index = 1) : base(machine)
        {
            if(index != 1 && index != 2)
            {
                throw new ArgumentOutOfRangeException(nameof(index), "only LPTIM1 (1) and LPTIM2 (2) exist");
            }

            this.frequency = frequency;
            this.index = index;
            resetRegister = index == 1 ? Apb1ResetRegister1 : Apb1ResetRegister2;

            compareTimer = new LimitTimer(
                machine.ClockSource, frequency, this, "cmp" + index,
                1, Direction.Ascending, false, WorkMode.OneShot, true, false, 1);
            compareTimer.LimitReached += OnCompareMatch;

            reloadTimer = new LimitTimer(
                machine.ClockSource, frequency, this, "reload" + index,
                1, Direction.Ascending, false, WorkMode.OneShot, true, false, 1);
            reloadTimer.LimitReached += OnAutoReloadMatch;

            connections[0] = irq;

            try
            {
                machine.SystemBus.AddWatchpointHook(
                    resetRegister, SysbusAccessWidth.DoubleWord, Access.Write,
                    (cpu, address, width, value) => OnResetRegisterWrite(value));
                this.InfoLog("LPTIM{0}: watching RCC reset 0x{1:X8} bit {2} at {3} Hz",
                             index, resetRegister, index == 1 ? 31 : 5, frequency);
            }
            catch(Exception exception)
            {
                this.WarningLog("LPTIM{0}: cannot watch the RCC reset line ({1}); a hardware "
                                + "reset of this timer will not be observed", index, exception.Message);
            }
        }

        public long Size => 0x400;

        /// <summary>Interrupt output 0: the NVIC line.</summary>
        public IReadOnlyDictionary<int, IGPIO> Connections => connections;

        /// <summary>
        /// The interrupt line, for a plain ``IRQ -> nvic@n`` connection. The property
        /// has to be the concrete <c>GPIO</c> type: Renode rejects <c>IGPIO</c> here
        /// with "Property 'IRQ' does not exist ... or is not of the GPIO type"
        /// (measured), which is also how Renode's own timer models declare it.
        /// </summary>
        public GPIO IRQ => irq;

        /// <summary>Counter running?</summary>
        public bool IsRunning => running;

        /// <summary>Starts seen (for probes).</summary>
        public long Starts => starts;

        /// <summary>Compare matches seen (for probes).</summary>
        public long CompareMatches => compareMatches;

        /// <summary>Auto-reload matches seen (for probes).</summary>
        public long AutoReloadMatches => autoReloadMatches;

        public override void Reset()
        {
            base.Reset();
            status = 0;
            interruptEnable = 0;
            configuration = 0;
            configuration2 = 0;
            control = 0;
            compare = 0;
            autoReload = 0;
            running = false;
            continuous = false;
            compareTimer.Enabled = false;
            reloadTimer.Enabled = false;
            compareTimer.Limit = 1;
            reloadTimer.Limit = 1;
            compareTimer.Value = 0;
            reloadTimer.Value = 0;
            // A reset drops the interrupt line - this is what lets the firmware's
            // "hard reset the timer, then clear the pending IRQ" sequence work.
            irq.Set(false);
            irqLevel = false;
        }

        public override uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case InterruptAndStatus:
                    return status;
                case InterruptEnable:
                    return interruptEnable;
                case Configuration:
                    return configuration;
                case Configuration2:
                    return configuration2;
                case Control:
                    return control;
                case CompareRegister:
                    return compare;
                case AutoReloadRegister:
                    return autoReload;
                case CounterRegister:
                    return (uint)CounterValue();
                case InterruptClear:
                    return 0; // write-only
                default:
                    this.NoisyLog("LPTIM{0}: read of unimplemented offset 0x{1:X}", index, offset);
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case InterruptClear:
                    status &= ~(value & (CompareMatchBit | AutoReloadMatchBit | CompareOkBit | AutoReloadOkBit));
                    UpdateInterrupt();
                    return;
                case InterruptEnable:
                    interruptEnable = value;
                    UpdateInterrupt();
                    return;
                case Configuration:
                    configuration = value;
                    return;
                case Configuration2:
                    configuration2 = value;
                    return;
                case Control:
                    control = value;
                    if((value & EnableBit) == 0)
                    {
                        StopCounter();
                    }
                    else if((value & SingleStartBit) != 0)
                    {
                        StartCounter(false);
                    }
                    else if((value & ContinuousStartBit) != 0)
                    {
                        StartCounter(true);
                    }
                    return;
                case CompareRegister:
                    compare = value & CounterMax;
                    if(running)
                    {
                        status |= CompareOkBit;
                    }
                    if(compare > autoReload && !compareAboveAutoReloadLogged)
                    {
                        compareAboveAutoReloadLogged = true;
                        this.WarningLog("LPTIM{0}: compare {1} is above auto-reload {2}: the "
                                        + "compare event cannot fire before the reload",
                                        index, compare, autoReload);
                    }
                    UpdateInterrupt();
                    return;
                case AutoReloadRegister:
                    autoReload = value & CounterMax;
                    if(running)
                    {
                        status |= AutoReloadOkBit;
                    }
                    return;
                case CounterRegister:
                    // LPTIM_CNT is read-only on this family (RM0434).
                    this.NoisyLog("LPTIM{0}: write 0x{1:X8} to read-only CNT ignored", index, value);
                    return;
                default:
                    this.NoisyLog("LPTIM{0}: write of 0x{1:X8} to unimplemented offset 0x{2:X}",
                                  index, value, offset);
                    return;
            }
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            return string.Format(
                "LPTIM{0} CR=0x{1:X8} ISR=0x{2:X8} DIER=0x{3:X8} CFGR=0x{4:X8} CMP={5} ARR={6} "
                + "CNT={7} running={8} continuous={9} starts={10} cmpm={11} arrm={12} irq={13}",
                index, control, status, interruptEnable, configuration, compare, autoReload,
                CounterValue(), running, continuous, starts, compareMatches, autoReloadMatches, irqLevel);
        }

        /// <summary>Interrupt line level, kept next to the GPIO so DumpState can report it.</summary>
        private bool irqLevel;

        private void StartCounter(bool continuousMode)
        {
            continuous = continuousMode;
            // A fresh one-shot starts from a clean flag state.
            status &= ~(CompareMatchBit | AutoReloadMatchBit);
            running = true;
            starts++;
            if(!startLogged)
            {
                startLogged = true;
                this.InfoLog("LPTIM{0}: one-shot armed (CMP={1} ARR={2} PRESC={3}, {4} Hz)",
                             index, compare, autoReload, configuration & PrescalerMask,
                             frequency / (1UL << (int)(configuration & PrescalerMask)));
            }

            var divider = 1UL << (int)(configuration & PrescalerMask);
            compareTimer.Divider = divider;
            reloadTimer.Divider = divider;
            compareTimer.Value = 0;
            reloadTimer.Value = 0;

            reloadTimer.Limit = autoReload == 0 ? 1 : autoReload;
            reloadTimer.Mode = continuous ? WorkMode.Periodic : WorkMode.OneShot;
            reloadTimer.AutoUpdate = continuous;
            reloadTimer.Enabled = true;

            if(compare != 0 && compare <= autoReload)
            {
                compareTimer.Limit = compare;
                compareTimer.Enabled = true;
            }
            else
            {
                compareTimer.Enabled = false;
            }

            UpdateInterrupt();
        }

        private void StopCounter()
        {
            running = false;
            compareTimer.Enabled = false;
            reloadTimer.Enabled = false;
            // Clearing ENABLE releases the interrupt lines, as on silicon.
            UpdateInterrupt();
        }

        private void OnCompareMatch()
        {
            compareMatches++;
            status |= CompareMatchBit;
            UpdateInterrupt();
        }

        private void OnAutoReloadMatch()
        {
            autoReloadMatches++;
            status |= AutoReloadMatchBit;
            if(!continuous)
            {
                running = false;
                compareTimer.Enabled = false;
                reloadTimer.Enabled = false;
            }
            UpdateInterrupt();
        }

        /// <summary>
        /// The RCC reset line this timer hangs off. The firmware stops the idle timer
        /// this way and only this way (`furi_hal_idle_timer_reset`), so observing the
        /// register is what makes "flag set -> interrupt -> firmware clears pending"
        /// terminate instead of re-pending forever.
        /// </summary>
        private void OnResetRegisterWrite(ulong value)
        {
            var asserted = (value & (index == 1 ? (1UL << 31) : (1UL << 5))) != 0;
            if(asserted && !resetLineAsserted)
            {
                this.InfoLog("LPTIM{0}: reset line asserted (RCC 0x{1:X8} = 0x{2:X8})",
                             index, resetRegister, (uint)value);
                Reset();
            }
            resetLineAsserted = asserted;
        }

        private ulong CounterValue()
        {
            if(!running)
            {
                // Never started, stopped, or a completed one-shot reads 0: the
                // firmware's `after_cnt = CNT + skew + 3` and its
                // `after_tick += expected_idle_ticks` only balance this way.
                return 0;
            }
            return reloadTimer.Value;
        }

        private void UpdateInterrupt()
        {
            var active = ((status & CompareMatchBit) != 0 && (interruptEnable & CompareMatchBit) != 0)
                      || ((status & AutoReloadMatchBit) != 0 && (interruptEnable & AutoReloadMatchBit) != 0);
            irqLevel = active;
            irq.Set(active);
        }
    }
}
