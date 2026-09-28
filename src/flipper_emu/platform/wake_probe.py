"""Is the CPU still parked in WFI, and if so, why does no interrupt arrive?

Run with `--renode-include`.  Samples, every 2 s:

* CPU: PC/LR, `ExecutedInstructions` (does it advance?), `IsHalted`
* the timer the firmware arms for tickless idle (LPTIM1 @ 0x40007C00) with
  *correct* offset labels -- a previous probe in this session printed the
  right values under the wrong names, so the offsets are listed explicitly
* whether the wake-up IRQ is enabled and pending in the NVIC (ISER/ISPR),
  which separates "the timer never fires" from "it fires but is masked"
* the RTC windows the firmware touches (0x40002000 backup, 0x40002800 RTC)
* the panel capture file size, so "CPU awake" and "drawing" are sampled together

Output: artifacts/wake-probe.log
"""

import os
import time
from System.Threading import Thread, ThreadStart

OUT = r"C:\Users\gtttr\auraesp32\flipper-emu\artifacts\wake-probe.log"
STREAM = r"C:\Users\gtttr\auraesp32\flipper-emu\artifacts\display-stream.bin"

#: LPTIM1 register offsets (Renode's STM32L0_LpTimer uses the real layout:
#: ISR/ICR/DIER/CFGR/CR/CMP/ARR/CNT).  Labelled, because guessing these was
#: the mistake that produced the earlier mislabelled dump.
LPTIM1_OFFSETS = (
    (0x00, "ISR"),
    (0x04, "ICR"),
    (0x08, "DIER"),
    (0x0C, "CFGR"),
    (0x10, "CR"),
    (0x14, "CMP"),
    (0x18, "ARR"),
    (0x1C, "CNT"),
    (0x20, "?0x20"),
    (0x24, "?0x24"),
)

NVIC_OFFSETS = (
    (0xE000E100, "ISER0"),
    (0xE000E104, "ISER1"),
    (0xE000E200, "ISPR0"),
    (0xE000E204, "ISPR1"),
    (0xE000E010, "SYST_CTRL"),
    (0xE000E014, "SYST_LOAD"),
    (0xE000E018, "SYST_VAL"),
)

RTC_OFFSETS = (
    (0x40002800 + 0x00, "RTC_TR"),
    (0x40002800 + 0x08, "RTC_CR"),
    (0x40002800 + 0x0C, "RTC_ISR"),
    (0x40002800 + 0x10, "RTC_PRER"),
    (0x40002000 + 0x50, "BKP1R"),
)


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


machine = self.Machine
bus = machine.SystemBus
resolved = self.TryFindPeripheralByName("cpu")
if isinstance(resolved, tuple):
    resolved = resolved[1] if len(resolved) > 1 else None
log("WAKE_PROBE_LOADED cpu=%r" % (resolved,))


def get_register(cpu, index):
    value = cpu.GetRegister(index)
    for attribute in ("Value", "RawValue"):
        try:
            return long(getattr(value, attribute))
        except Exception:
            pass
    return None


def read_u32(address):
    """Read a 32-bit register.

    The typed API comes first: these peripherals are registered as double-word
    devices, and the byte-wise path returned the wrong window for LPTIM1 (it
    produced the address-0 alias contents - ``0x20030000``/``0x08011B9D``, the
    initial SP and reset vector - where the monitor's own ``sysbus
    ReadDoubleWord`` returned LPTIM1's real registers).  Recorded so the next
    probe does not build a conclusion on the byte-wise path.
    """
    try:
        return long(bus.ReadDoubleWord(address))
    except Exception as exc:
        typed_error = str(exc)
    try:
        data = bus.ReadBytes(address, 4)
        return int(data[0]) | (int(data[1]) << 8) | (int(data[2]) << 16) | (int(data[3]) << 24)
    except Exception as exc:
        return "err:%s (typed: %s)" % (exc, typed_error)


def dump(offsets):
    parts = []
    for address, name in offsets:
        parts.append("%s=0x%08X" % (name, read_u32(address)))
    return " ".join(parts)


def sample():
    previous = None
    for index in range(10):
        time.sleep(2.0)
        try:
            pc = get_register(resolved, 15) or 0
            lr = get_register(resolved, 14) or 0
            instructions = getattr(resolved, "ExecutedInstructions")
        except Exception as exc:
            pc = lr = 0
            instructions = "err:%s" % exc
        try:
            halted = getattr(resolved, "IsHalted")
        except Exception as exc:
            halted = "err:%s" % exc
        try:
            size = os.path.getsize(STREAM)
        except Exception:
            size = -1
        delta = "-"
        if isinstance(previous, (int, long)) and isinstance(instructions, (int, long)):
            delta = "%+d" % (instructions - previous)
        if isinstance(instructions, (int, long)):
            previous = instructions
        log("t+%2ds PC=0x%08X LR=0x%08X halted=%s instr=%s delta=%s stream=%s"
            % (2 * (index + 1), pc, lr, halted, instructions, delta, size))
        log("      lptim1 %s" % dump(LPTIM1_OFFSETS))
        log("      nvic   %s" % dump(NVIC_OFFSETS))
        log("      rtc    %s" % dump(RTC_OFFSETS))


Thread(ThreadStart(sample)).Start()
