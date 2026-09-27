using System;
using System.Collections.Generic;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 GPIO port with a real *input* path.
    /// </summary>
    /// <remarks>
    /// Why this model exists instead of Renode's <c>GPIOPort.STM32_GPIOPort</c>:
    /// that model has no way to make an input pin read high. Measured on this
    /// Renode build (1.17.0), with <c>GPIOB</c> driven by the 1.4.3 bootloader:
    ///
    /// <code>
    /// GPIOB IDR                  = 0x00000003  // only the two *output* pins
    /// GPIOB PUPDR (PB10..PB12)   = pull-up, and IDR still 0
    /// gpioPortB OnGPIO 11 true   -> IDR unchanged (the injection never lands)
    /// write IDR = 0x1C00         -> read back 0x00000003 (read-only for inputs)
    /// write ODR = 0x1C00         -> IDR unchanged (ODR only shows in output mode)
    /// </code>
    ///
    /// So every input pin read "pressed", and the bootloader's own button checks
    /// (polled levels, not EXTI - `main` at 0x08011954: LEFT selects the DFU
    /// splash, UP the recovery splash) always took the DFU path, which is why the
    /// firmware showed the "Update &amp; Recovery Mode / DFU Started" screen
    /// instead of reaching <c>furi_init()</c>.
    ///
    /// Register map (GPIO_TypeDef, stm32wb55xx.h):
    ///
    /// <code>
    /// MODER 0x00  OTYPER 0x04  OSPEEDR 0x08  PUPDR 0x0C  IDR 0x10  ODR 0x14
    /// BSRR  0x18  LCKR   0x1C  AFRL    0x20  AFRH  0x24  BRR 0x28
    /// </code>
    ///
    /// The input path, in the order the firmware's own HAL uses it:
    ///
    /// 1. a pin in output mode (MODER 01) or alternate function (MODER 10) reads
    ///    back the level the port drives (ODR) - what real silicon does for a
    ///    push-pull output;
    /// 2. an input pin (MODER 00) reads the *board* level: the value injected via
    ///    <see cref="OnGPIO"/> (monitor <c>OnGPIO</c> commands, the UI's buttons,
    ///    the start-up levels in the generated <c>.resc</c>) or, when nothing
    ///    drives it, the idle level PUPDR selects (01 pulls high, 10 pulls low);
    /// 3. a floating input (PUPDR 00) with nothing driving it reads 0 - a
    ///    documented deviation (silicon would be undefined, and there is no
    ///    analog model here to resolve it).
    ///
    /// Injected levels model the world *outside* the chip, so they deliberately
    /// survive <see cref="Reset"/> (rebooting the MCU does not press a button),
    /// while every register returns to its reset value.
    ///
    /// Every pin also drives a numbered output, so the platform's existing
    /// <c>[0-15] -&gt; exti@[0-15]</c> wiring keeps working: the output carries
    /// the pin's effective level and is only written when it changes, which is
    /// exactly what the edge detector in <see cref="Wb55Exti"/> latches on.
    ///
    /// Deliberate deviations: the LCKR key sequence is not enforced (the value is
    /// stored for read-back only), and a pin in alternate-function mode reports
    /// its ODR bit rather than a peripheral's own output.
    /// </remarks>
    public class GpioWb55Port : BasicDoubleWordPeripheral, IGPIOReceiver, INumberedGPIOOutput, IKnownSize
    {
        // Register offsets (GPIO_TypeDef, stm32wb55xx.h).
        private const long Moder = 0x00;
        private const long Otyper = 0x04;
        private const long Ospeedr = 0x08;
        private const long Pupdr = 0x0C;
        private const long Idr = 0x10;
        private const long Odr = 0x14;
        private const long Bsrr = 0x18;
        private const long Lckr = 0x1C;
        private const long Afrl = 0x20;
        private const long Afrh = 0x24;
        private const long Brr = 0x28;

        /// <summary>Pins per port (GPIOA..GPIOH all expose 16).</summary>
        public const int PinCount = 16;

        /// <summary>MODE field: input (the reset state).</summary>
        public const uint ModeInput = 0x0;

        /// <summary>MODE field: general-purpose output.</summary>
        public const uint ModeOutput = 0x1;

        /// <summary>MODE field: alternate function.</summary>
        public const uint ModeAlternate = 0x2;

        /// <summary>PUPDR field: pull-up.</summary>
        public const uint PullUp = 0x1;

        /// <summary>PUPDR field: pull-down.</summary>
        public const uint PullDown = 0x2;

        private readonly bool[] injectedLevels = new bool[PinCount];
        private readonly bool[] injectedValid = new bool[PinCount];
        private readonly bool[] drivenLevels = new bool[PinCount];
        private readonly Dictionary<int, IGPIO> connections = new Dictionary<int, IGPIO>();

        private uint moder;
        private uint otyper;
        private uint ospeedr;
        private uint pupdr;
        private uint odr;
        private uint lckr;
        private uint afrl;
        private uint afrh;

        private bool firstIdrReadLogged;
        private bool firstInjectionLogged;

        public GpioWb55Port(IMachine machine, string portName = "GPIO") : base(machine)
        {
            PortName = portName;
            for(var pin = 0; pin < PinCount; pin++)
            {
                // One output per pin; the platform file connects these to EXTI.
                connections[pin] = new GPIO();
            }
        }

        /// <summary>Port letter, for log messages ("A".."H").</summary>
        public string PortName { get; set; }

        public long Size => 0x400;

        public IReadOnlyDictionary<int, IGPIO> Connections => connections;

        public override void Reset()
        {
            moder = 0;
            otyper = 0;
            ospeedr = 0;
            pupdr = 0;
            odr = 0;
            lckr = 0;
            afrl = 0;
            afrh = 0;
            firstIdrReadLogged = false;
            // Injected levels are board wiring, not peripheral state: they are
            // kept across a reset, exactly like a released button stays released
            // when the MCU reboots.
            RefreshAll();
        }

        /// <summary>
        /// Board-side pin drive: input 0..15 is a pin of this port.
        /// </summary>
        /// <remarks>
        /// This is what the monitor's <c>OnGPIO</c> command, the UI's buttons and
        /// the generated <c>.resc</c> all reach. The level is remembered as the
        /// board's, so it outlives a peripheral reset, and the pin's effective
        /// level is recomputed (which is what makes EXTI see the edge).
        /// </remarks>
        public void OnGPIO(int number, bool value)
        {
            if(number < 0 || number >= PinCount)
            {
                this.WarningLog("GPIO{0}: input {1} does not exist (0-{2} only)",
                                PortName, number, PinCount - 1);
                return;
            }

            injectedLevels[number] = value;
            injectedValid[number] = true;
            if(!firstInjectionLogged)
            {
                firstInjectionLogged = true;
                this.InfoLog("GPIO{0}: board levels are being injected (first: pin {1} = {2})",
                             PortName, number, value ? "high" : "low");
            }
            RefreshPin(number);
        }

        public override uint ReadDoubleWord(long offset)
        {
            var value = ReadRegister(offset);
            if(offset == Idr)
            {
                LogFirstIdrRead(value);
            }
            return value;
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            WriteRegister(offset, value);
        }

        // Sub-word accesses are not overridden: BasicDoubleWordPeripheral offers no
        // virtual byte/half-word accessors, so the bus serves them with a
        // read-modify-write of the double word. That is fine here because every
        // GPIO access the firmware performs is 32-bit (the HAL and LL write whole
        // BSRR/BRR/ODR registers, never a half-word of them).

        /// <summary>Register value as the firmware reads it (no side effects).</summary>
        private uint ReadRegister(long offset)
        {
            switch(offset)
            {
                case Moder: return moder;
                case Otyper: return otyper;
                case Ospeedr: return ospeedr;
                case Pupdr: return pupdr;
                case Idr: return ComputeIdr();
                case Odr: return odr;
                case Lckr: return lckr;
                case Afrl: return afrl;
                case Afrh: return afrh;
                default:
                    // BSRR/BRR are write-only and the rest of the 1 KiB window is
                    // reserved; silicon reads both back as 0.
                    return 0;
            }
        }

        private void WriteRegister(long offset, uint value)
        {
            switch(offset)
            {
                case Moder: moder = value; RefreshAll(); return;
                case Otyper: otyper = value; return;
                case Ospeedr: ospeedr = value; return;
                case Pupdr: pupdr = value; RefreshAll(); return;
                case Idr:
                    // Read-only on silicon. This is also where the old port let a
                    // write through silently while never reflecting its inputs.
                    this.WarningLog("GPIO{0}: write of 0x{1:X8} to read-only IDR ignored",
                                    PortName, value);
                    return;
                case Odr: odr = value; RefreshAll(); return;
                case Bsrr: ApplyBsrr(value); return;
                case Lckr: lckr = value; return;
                case Afrl: afrl = value; return;
                case Afrh: afrh = value; RefreshAll(); return;
                case Brr: odr &= ~value; RefreshAll(); return;
                default:
                    this.NoisyLog("GPIO{0}: unimplemented write at 0x{1:X} value 0x{2:X8}",
                                  PortName, offset, value);
                    return;
            }
        }

        /// <summary>BSRR: low half sets pins, high half resets them (write-only).</summary>
        private void ApplyBsrr(uint value)
        {
            odr = (odr | (value & 0xFFFF)) & ~(value >> 16);
            RefreshAll();
        }

        /// <summary>Effective level of one pin; see the class remarks.</summary>
        public bool PinLevel(int pin)
        {
            var mode = (moder >> (pin * 2)) & 0x3;
            if(mode == ModeOutput || mode == ModeAlternate)
            {
                return ((odr >> pin) & 1) != 0;
            }

            if(injectedValid[pin])
            {
                return injectedLevels[pin];
            }

            switch((pupdr >> (pin * 2)) & 0x3)
            {
                case PullUp: return true;
                case PullDown: return false;
                default: return false;
            }
        }

        private uint ComputeIdr()
        {
            var value = 0u;
            for(var pin = 0; pin < PinCount; pin++)
            {
                if(PinLevel(pin))
                {
                    value |= 1u << pin;
                }
            }
            return value;
        }

        private uint InjectedMask()
        {
            var mask = 0u;
            for(var pin = 0; pin < PinCount; pin++)
            {
                if(injectedValid[pin])
                {
                    mask |= 1u << pin;
                }
            }
            return mask;
        }

        /// <summary>Re-drive one pin's EXTI output if its level moved.</summary>
        private void RefreshPin(int pin)
        {
            var level = PinLevel(pin);
            if(drivenLevels[pin] == level)
            {
                return;
            }
            drivenLevels[pin] = level;
            connections[pin].Set(level);
        }

        private void RefreshAll()
        {
            for(var pin = 0; pin < PinCount; pin++)
            {
                RefreshPin(pin);
            }
        }

        /// <summary>
        /// Log the first IDR read at Info level: that one line is the evidence for
        /// whether the boot code saw its buttons as released.
        /// </summary>
        private void LogFirstIdrRead(uint value)
        {
            if(firstIdrReadLogged)
            {
                return;
            }
            firstIdrReadLogged = true;
            this.InfoLog(
                "GPIO{0} first IDR read: 0x{1:X4} (MODER=0x{2:X8} PUPDR=0x{3:X8} injected=0x{4:X4})",
                PortName, value, moder, pupdr, InjectedMask());
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            var builder = new StringBuilder();
            builder.AppendFormat(
                "GPIO{0} MODER=0x{1:X8} PUPDR=0x{2:X8} ODR=0x{3:X4} IDR=0x{4:X4} injected=0x{5:X4}",
                PortName, moder, pupdr, odr, ComputeIdr(), InjectedMask());
            builder.Append(" pins[0-15]=");
            for(var pin = 0; pin < PinCount; pin++)
            {
                builder.Append(PinLevel(pin) ? '1' : '0');
            }
            return builder.ToString();
        }
    }
}
