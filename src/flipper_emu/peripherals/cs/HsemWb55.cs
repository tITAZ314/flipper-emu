using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 HSEM (hardware semaphore) block.
    /// </summary>
    /// <remarks>
    /// Why a model at all: the platform used to serve `0x58001400` with a Python
    /// stub that returned 0 for every read and dropped every write, and the 1.4.3
    /// firmware reads a semaphore's state as part of its wireless-stack bring-up:
    ///
    /// <code>
    /// furi_hal_bt_init:            furi_check(LL_HSEM_1StepLock(HSEM, CFG_HW_CLK48_CONFIG_SEMID) == 0);
    /// furi_hal_bt_start_radio_stack: the same CLK48 semaphore
    /// furi_hal_bt_nvm_sram_sem_acquire/release: CFG_HW_BLE_NVM_SRAM_SEMID
    /// furi_hal_power_sleep/shutdown:  RCC + ENTRY_STOP_MODE semaphores
    /// furi_hal_random_get/fill_buf:   RNG semaphore
    /// </code>
    ///
    /// Register layout, derived from the firmware's own code rather than guessed -
    /// every `LDR`/`STR` the image performs against the HSEM base literal was
    /// decoded and each one matches the semaphore id the firmware documents for
    /// it (`targets/f7/ble_glue/hsem_map.h`):
    ///
    /// <code>
    /// R_i    = 0x00 + 4*i   (i = 0..31)   set/release; a write with LOCK takes it
    /// RLR_i  = 0x80 + 4*i   (i = 0..31)   read-only owner/lock state
    /// </code>
    ///
    /// <code>
    /// read  [HSEM+0x80] -> RLR0, RNG            (id 0)  furi_hal_random_get
    /// read  [HSEM+0x8C] -> RLR3, RCC            (id 3)  furi_hal_power_sleep
    /// read  [HSEM+0x90] -> RLR4, ENTRY_STOP     (id 4)  furi_hal_power_sleep
    /// read  [HSEM+0x94] -> RLR5, CLK48          (id 5)  furi_hal_bt_init, start_radio_stack
    /// read  [HSEM+0xA0] -> RLR8, BLE_NVM_SRAM   (id 8)  furi_hal_bt_nvm_sram_sem_acquire
    /// write [HSEM+0x20] -> R8  = 0x400 (COREID 4, LOCK clear = release)  nvm_sram_sem_release
    /// </code>
    ///
    /// LOCK (bit 31) and COREID (bits 11:8) are the fields the firmware compares
    /// against: it requires a semaphore it wants to hold to read back as
    /// `0x80000400` (LOCK set, COREID 4 = CPU1).
    ///
    /// The interrupt and clear registers are stored (and logged) but their offsets
    /// are the one part of this map not derived from firmware evidence - the image
    /// never touches them, so they are marked as unverified rather than presented
    /// as measured.
    ///
    /// Known deviation: a write is honoured with whatever COREID it carries. Real
    /// silicon validates it against the writing master; nothing else in this
    /// emulator can take a semaphore, so the distinction is not observable here.
    /// </remarks>
    public class HsemWb55 : BasicDoubleWordPeripheral, IKnownSize
    {
        /// <summary>Write/read register bank: R_i at 4*i.</summary>
        private const long LockBank = 0x00;

        /// <summary>Read-only lock register bank: RLR_i at 0x80 + 4*i.</summary>
        private const long ReadLockBank = 0x80;

        // Interrupt/clear registers (offsets not exercised by this firmware).
        private const long C1Ier = 0x100;
        private const long C1Icr = 0x104;
        private const long C1Isr = 0x108;
        private const long C1Misr = 0x10C;
        private const long C2Ier = 0x110;
        private const long C2Icr = 0x114;
        private const long C2Isr = 0x118;
        private const long C2Misr = 0x11C;

        /// <summary>Clear register (key protected on silicon).</summary>
        private const long ClearRegister = 0x140;

        /// <summary>LOCK bit of a semaphore register.</summary>
        public const uint LockBit = 1u << 31;

        /// <summary>COREID field: bits 11:8.</summary>
        public const int CoreIdShift = 8;

        /// <summary>COREID of CPU1 (the Cortex-M4) on this board.</summary>
        public const uint Cpu1CoreId = 4;

        /// <summary>Semaphores the WB55 exposes.</summary>
        public const int MaxSemaphores = 32;

        private readonly uint[] owner = new uint[MaxSemaphores];
        private readonly bool[] locked = new bool[MaxSemaphores];
        private readonly Dictionary<int, bool> accessLogged = new Dictionary<int, bool>();

        private uint c1Ier;
        private uint c1Icr;
        private uint c1Isr;
        private uint c1Misr;
        private uint c2Ier;
        private uint c2Icr;
        private uint c2Isr;
        private uint c2Misr;
        private uint clearRegister;

        private long accesses;
        private uint bootChainLockMask;
        private uint arbitratedSemaphores;
        private bool coreIdWriteClaims;

        public HsemWb55(IMachine machine, int numberOfSemaphores = MaxSemaphores,
                        uint bootChainLockMask = 0, bool coreIdWriteClaims = true,
                        uint arbitratedSemaphores = 0) : base(machine)
        {
            if(numberOfSemaphores < 1 || numberOfSemaphores > MaxSemaphores)
            {
                throw new ArgumentOutOfRangeException(nameof(numberOfSemaphores));
            }
            NumberOfSemaphores = numberOfSemaphores;
            this.coreIdWriteClaims = coreIdWriteClaims;
            // Applied through the property, like the boot-chain mask: Renode passes
            // platform keys as constructor arguments, and applying this only in
            // Reset() did not take effect (measured: no arbitration log, RLR0 still
            // read as free).
            ArbitratedSemaphores = arbitratedSemaphores;
            // Set through the property so the stand-in takes effect immediately:
            // Renode sets platform-file keys as constructor arguments, not as
            // property assignments, and the value has to be right before the first
            // reset the machine performs.
            BootChainLockMask = bootChainLockMask;
        }

        public long Size => 0x400;

        public int NumberOfSemaphores { get; }

        /// <summary>
        /// Semaphores to leave held by CPU1 as if the boot chain had taken them.
        /// </summary>
        /// <remarks>
        /// This exists because the emulator starts the application image directly,
        /// and that image is the application alone (the 1.4.3 `-full` package is a
        /// single element at 0x08000000, 768,132 bytes, and the flash image's reset
        /// vector is the application's own entry point). The application's
        /// `furi_hal_bt_init` does not take the CLK48 semaphore - the compiled code
        /// only *reads* `RLR5` and requires it to read `LOCK | COREID 4` - so the
        /// lock has to come from the code that runs before the application on real
        /// hardware (the boot loader), which this emulator does not run.
        ///
        /// Set from the platform file (`bootChainLockMask: 0x20` = CLK48, semaphore
        /// 5) so the stand-in is visible and reversible in one place, and re-applied
        /// on every reset because on hardware the boot loader runs on every reset.
        /// </remarks>
        public uint BootChainLockMask
        {
            get { return bootChainLockMask; }
            set
            {
                bootChainLockMask = value;
                ApplyBootChainLocks();
            }
        }

        /// <summary>
        /// Semaphores handed to CPU1 whenever they are free (semantics (A)).
        /// </summary>
        /// <remarks>
        /// Set from the platform file (`arbitratedSemaphores: 0x1` = the RNG
        /// semaphore). The application waits for the RNG semaphore to be held by CPU1
        /// before using it and writes its core id afterwards, and nothing in the
        /// image ever takes it, so without a holder there the application spins in
        /// `furi_hal_random_fill_buf` forever (measured). On silicon the other side
        /// of that arbitration is the FUS on CPU2; this property is that stand-in.
        /// </remarks>
        public uint ArbitratedSemaphores
        {
            get { return arbitratedSemaphores; }
            set
            {
                arbitratedSemaphores = value;
                ApplyArbitration();
            }
        }

        public override void Reset()
        {
            Array.Clear(owner, 0, owner.Length);
            Array.Clear(locked, 0, locked.Length);
            c1Ier = c1Icr = c1Isr = c1Misr = 0;
            c2Ier = c2Icr = c2Isr = c2Misr = 0;
            clearRegister = 0;
            accessLogged.Clear();
            accesses = 0;
            ApplyBootChainLocks();
            ApplyArbitration();
        }

        /// <summary>
        /// Semantics (A): an arbitrated semaphore is held by CPU1 from reset, and
        /// handed straight back to it whenever it is freed, because nothing in this
        /// emulator plays the other side of the arbitration (the FUS on CPU2, which
        /// owns the RNG semaphore on real silicon).
        /// </summary>
        private void ApplyArbitration()
        {
            if(arbitratedSemaphores == 0)
            {
                return;
            }

            for(var index = 0; index < MaxSemaphores; index++)
            {
                if((arbitratedSemaphores & (1u << index)) != 0)
                {
                    locked[index] = true;
                    owner[index] = Cpu1CoreId;
                }
            }
            this.InfoLog(
                "HSEM: no arbitration here - semaphores 0x{0:X} are held by core {1} from reset",
                arbitratedSemaphores, Cpu1CoreId);
        }

        private void ApplyBootChainLocks()
        {
            if(bootChainLockMask == 0)
            {
                return;
            }

            for(var index = 0; index < MaxSemaphores; index++)
            {
                if((bootChainLockMask & (1u << index)) != 0)
                {
                    locked[index] = true;
                    owner[index] = Cpu1CoreId;
                }
            }
            this.InfoLog(
                "HSEM: standing in for the boot loader - semaphores 0x{0:X} are already held by core {1}",
                bootChainLockMask, Cpu1CoreId);
        }

        /// <summary>Semaphore ids as the firmware names them (targets/f7/ble_glue/hsem_map.h).</summary>
        public static string SemaphoreName(int index)
        {
            switch(index)
            {
                case 0: return "RNG";
                case 1: return "PKA";
                case 2: return "FLASH";
                case 3: return "RCC";
                case 4: return "ENTRY_STOP_MODE";
                case 5: return "CLK48";
                case 6: return "BLOCK_FLASH_REQ_BY_CPU1";
                case 7: return "BLOCK_FLASH_REQ_BY_CPU2";
                case 8: return "BLE_NVM_SRAM";
                case 9: return "THREAD_NVM_SRAM";
                case 10: return "PWR_STANDBY";
                default: return "unassigned";
            }
        }

        public override uint ReadDoubleWord(long offset)
        {
            accesses++;
            if(IsReadLockRegister(offset))
            {
                return ReadSemaphore(IndexInReadLockBank(offset), "RLR");
            }
            if(IsLockRegister(offset))
            {
                return ReadSemaphore(IndexInLockBank(offset), "R");
            }

            switch(offset)
            {
                case C1Ier: return c1Ier;
                case C1Icr: return 0; // write-only on silicon
                case C1Isr: return c1Isr;
                case C1Misr: return c1Misr;
                case C2Ier: return c2Ier;
                case C2Icr: return 0;
                case C2Isr: return c2Isr;
                case C2Misr: return c2Misr;
                case ClearRegister: return clearRegister;
                default:
                    this.NoisyLog("HSEM: unimplemented read at 0x{0:X}", offset);
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            accesses++;
            if(IsReadLockRegister(offset))
            {
                this.WarningLog("HSEM: write of 0x{0:X8} to read-only RLR at 0x{1:X} ignored",
                                value, offset);
                return;
            }
            if(IsLockRegister(offset))
            {
                var index = IndexInLockBank(offset);
                var coreId = (value >> CoreIdShift) & 0xF;
                if((value & LockBit) != 0)
                {
                    TryAcquire(index, coreId, value);
                }
                else if(coreIdWriteClaims && coreId != 0)
                {
                    // Semantics (B): writing a core id *without* the LOCK bit claims
                    // the semaphore for that core (the hardware sets LOCK), rather
                    // than releasing it. The application's RNG path relies on this:
                    // it waits for the lock, uses the RNG, then writes 0x400.
                    TryClaim(index, coreId, value);
                }
                else
                {
                    TryRelease(index, coreId, value);
                }
                return;
            }

            switch(offset)
            {
                case C1Ier:
                    c1Ier = value;
                    if(value != 0)
                    {
                        // Nothing here can raise a CPU2 event, so an enable is only
                        // worth noticing: the firmware never does it today.
                        this.InfoLog("HSEM: CPU1 interrupt enable wrote 0x{0:X8} (no source to raise)",
                                     value);
                    }
                    return;
                case C1Icr: c1Icr = value; c1Isr &= ~value; return;
                case C1Isr: return; // read-only
                case C1Misr: return;
                case C2Ier: c2Ier = value; return;
                case C2Icr: c2Icr = value; c2Isr &= ~value; return;
                case C2Isr: return;
                case C2Misr: return;
                case ClearRegister: clearRegister = value; return;
                default:
                    this.NoisyLog("HSEM: unimplemented write at 0x{0:X} value 0x{1:X8}", offset, value);
                    return;
            }
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            var held = new List<string>();
            for(var index = 0; index < MaxSemaphores; index++)
            {
                if(locked[index])
                {
                    held.Add(string.Format("{0}({1})=core{2}", index, SemaphoreName(index), owner[index]));
                }
            }
            return string.Format("HSEM accesses={0} held=[{1}]",
                                 accesses, string.Join(", ", held.ToArray()));
        }

        private static bool IsLockRegister(long offset)
        {
            return offset >= LockBank && offset < LockBank + MaxSemaphores * 4 && (offset % 4) == 0;
        }

        private static bool IsReadLockRegister(long offset)
        {
            return offset >= ReadLockBank && offset < ReadLockBank + MaxSemaphores * 4
                && (offset % 4) == 0;
        }

        private static int IndexInLockBank(long offset)
        {
            return (int)((offset - LockBank) / 4);
        }

        private static int IndexInReadLockBank(long offset)
        {
            return (int)((offset - ReadLockBank) / 4);
        }

        /// <summary>Owner/lock state as the firmware reads it.</summary>
        private uint ReadSemaphore(int index, string bank)
        {
            var value = (locked[index] ? LockBit : 0) | (owner[index] << CoreIdShift);
            LogSemaphore(index, bank, value, "");
            return value;
        }

        private void TryAcquire(int index, uint coreId, uint written)
        {
            if(locked[index] && owner[index] != coreId)
            {
                LogSemaphore(index, "R write", written,
                             string.Format(" - refused, core {0} already holds it", owner[index]));
                return;
            }

            locked[index] = true;
            owner[index] = coreId;
            LogSemaphore(index, "R write", written,
                         string.Format(" - now held by core {0}", coreId));
        }

        /// <summary>Semantics (B): a core-id write without LOCK claims the semaphore.</summary>
        private void TryClaim(int index, uint coreId, uint written)
        {
            if(locked[index] && owner[index] != coreId)
            {
                LogSemaphore(index, "R write", written,
                             string.Format(" - claim refused, core {0} holds it", owner[index]));
                return;
            }

            locked[index] = true;
            owner[index] = coreId;
            LogSemaphore(index, "R write", written,
                         string.Format(" - claimed by core {0} (write without LOCK)", coreId));
        }

        /// <summary>
        /// Semantics (A) stand-in: nothing else in this emulator arbitrates the
        /// semaphores, so one that is "arbitrated" is handed straight back to CPU1
        /// when it becomes free - i.e. CPU1 always wins it.
        /// </summary>
        private void GrantArbitrated(int index)
        {
            if((arbitratedSemaphores & (1u << index)) == 0)
            {
                return;
            }

            locked[index] = true;
            owner[index] = Cpu1CoreId;
            LogSemaphore(index, "arbiter", LockBit | (Cpu1CoreId << CoreIdShift),
                         " - granted back to CPU1 (no CPU2 arbitration here)");
        }

        private void TryRelease(int index, uint coreId, uint written)
        {
            if(!locked[index])
            {
                LogSemaphore(index, "R write", written, " - release with no owner");
                return;
            }
            if(owner[index] != coreId)
            {
                LogSemaphore(index, "R write", written,
                             string.Format(" - release refused, core {0} holds it", owner[index]));
                return;
            }

            locked[index] = false;
            owner[index] = 0;
            LogSemaphore(index, "R write", written, " - released");
            GrantArbitrated(index);
        }

        /// <summary>
        /// Log the first access to each semaphore: one line per semaphore is enough
        /// to see which locks the firmware takes, without flooding the log.
        /// </summary>
        private void LogSemaphore(int index, string what, uint value, string note)
        {
            if(accessLogged.ContainsKey(index))
            {
                return;
            }
            accessLogged[index] = true;
            this.InfoLog("HSEM {0}[{1}] ({2}): 0x{3:X8}{4}",
                         what, index, SemaphoreName(index), value, note);
        }
    }
}
