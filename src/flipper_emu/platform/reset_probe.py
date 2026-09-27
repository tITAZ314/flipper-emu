"""Reset/crash probe: watch the firmware's reset requests and report the caller.

Load this into a running session with
``py -3.9 -m flipper_emu run --renode-include <this file>``.  It installs a
watchpoint on the ARM ``AIRCR`` register (``0xE000ED0C``), which is what
``NVIC_SystemReset()`` writes, and on every hit logs the CPU state, the caller's
call chain (any word on the stack that points into the firmware image) and a set
of marker registers.  Resolve the addresses with
``py -3.9 -m flipper_emu.console.symbols <firmware.elf> <address>``.

Two findings are baked in, both learned the hard way (docs/BRINGUP_LOG.md §8-9):

* the watchpoint is dropped when the platform resets (the NVIC is re-created), so
  it is re-armed from ``Machine.StateChanged`` - otherwise only the first reset
  is ever observed;
* a background thread takes one late snapshot of marker registers, so a run that
  never resets still shows how far the firmware got.
"""

from Antmicro.Renode.Peripherals.Bus import Access, SysbusAccessWidth
from System.Threading import Thread, ThreadStart
import time

OUT = "artifacts/reset-probe.log"

#: Registers worth seeing next to a reset: clock tree, RTC, power, radios.
MARKERS = (
    ("RCC_CR", 0x58000000),
    ("RCC_BDCR", 0x58000090),
    ("RCC_CSR", 0x58000094),
    ("RCC_APB1ENR1", 0x58000058),
    ("RCC_APB1RSTR1", 0x58000038),
    ("RCC_APB3RSTR", 0x58000044),
    ("PWR_CR1", 0x58000400),
    ("PWR_CR4", 0x5800040C),
    ("RTC_ISR", 0x4000280C),
    ("RTC_BKP0R", 0x40002850),
    ("IPCC_C1TOC2SR", 0x58000C0C),
    ("IPCC_C2TOC1SR", 0x58000C1C),
    ("USART1_ISR", 0x4001381C),
    ("FLASH_OPTR", 0x58004020),
)

#: ``__furi_check_message`` (release 1.4.3): points at the failed check's text.
CHECK_MESSAGE_POINTER = 0x20031364

SNAPSHOT_AFTER_SECONDS = 11
MAX_SAMPLES = 8


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


def read_u32(bus, address):
    data = bus.ReadBytes(address, 4)
    return int(data[0]) | (int(data[1]) << 8) | (int(data[2]) << 16) | (int(data[3]) << 24)


def read_string(bus, address, limit=120):
    characters = []
    for offset in range(limit):
        byte = int(bus.ReadBytes(address + offset, 1)[0])
        if byte == 0:
            break
        characters.append(chr(byte))
    return "".join(characters)


def get_register(cpu, index):
    value = cpu.GetRegister(index)
    for attribute in ("Value", "RawValue"):
        try:
            return long(getattr(value, attribute))
        except Exception:
            pass
    return None


log("RESET_PROBE_LOADED")
bus = self.Machine.SystemBus
machine = self.Machine
samples = []


def aircr_hook(cpu, address, width, value):
    if len(samples) >= MAX_SAMPLES:
        return
    samples.append(1)
    stack_pointer = get_register(cpu, 13)
    log("RESET_%d value=0x%08X PC=0x%08X LR=0x%08X SP=%s" % (
        len(samples), value, get_register(cpu, 15) or 0, get_register(cpu, 14) or 0,
        "0x%08X" % stack_pointer if stack_pointer else "?"))
    for label, target in MARKERS:
        try:
            log("  %-14s = 0x%08X" % (label, read_u32(bus, target)))
        except Exception as exc:
            log("  %-14s FAILED %s" % (label, exc))
    message_pointer = read_u32(bus, CHECK_MESSAGE_POINTER)
    if 0x08000000 <= message_pointer < 0x08100000:
        log("  CHECK_MESSAGE=%r" % read_string(bus, message_pointer))
    if not stack_pointer:
        return
    for index in range(32):
        word = read_u32(bus, stack_pointer + 4 * index)
        if 0x08000000 <= word < 0x08100000:
            log("  STACK[%02d] 0x%08X" % (index, word))


def arm():
    # Never call RemoveAllWatchpointHooks here: after the machine has been
    # disposed it walks into native tlib code and takes an AccessViolation
    # (observed at session teardown), and an armed-at-a-dead-address hook is
    # harmless because the old registration dies with its peripheral anyway.
    try:
        bus.AddWatchpointHook(0xE000ED0C, SysbusAccessWidth.DoubleWord, Access.Write, aircr_hook)
    except Exception as exc:
        log("ARM_FAIL=%s" % exc)


arm()


def on_state(machine_object, arguments):
    arm()


try:
    machine.StateChanged += on_state
    log("STATE_HOOK_SET")
except Exception as exc:
    log("STATE_HOOK_FAIL=%s" % exc)


def snapshot():
    time.sleep(SNAPSHOT_AFTER_SECONDS)
    log("SNAPSHOT resets=%d" % len(samples))
    for label, target in MARKERS:
        try:
            log("  %-14s = 0x%08X" % (label, read_u32(bus, target)))
        except Exception as exc:
            log("  %-14s FAILED %s" % (label, exc))


Thread(ThreadStart(snapshot)).Start()
