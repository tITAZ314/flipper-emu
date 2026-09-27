"""Boot probe: find which decision sends the firmware into its DFU splash.

Load into a session with
``py -3.9 -m flipper_emu run --renode-include src/flipper_emu/platform/boot_probe.py``.

It answers two questions at once:

* does the boot code read the RTC boot-mode/flags register (BKP1R at 0x40002054,
  the address the firmware's own `furi_hal_rtc_*` accessors use), and what does it
  see when it does?
* which of `main`'s branches actually executes: the DFU splash, the recovery
  splash, the update path, or the normal `furi_init()`?

Addresses come from the release ELF's symbols (Thumb bit cleared).
"""

from Antmicro.Renode.Peripherals.Bus import Access, SysbusAccessWidth
from System.Threading import Thread, ThreadStart
import time

OUT = "artifacts/boot-probe.log"

#: RTC backup register 1: boot mode in bits 19:16, flags below.
BKP1R = 0x40002054

#: Branch targets inside `main` we care about (even addresses).
FUNCTIONS = (
    ("dfu_splash", 0x080126B0),
    ("recovery_splash", 0x08011A28),
    ("update_exec", 0x08011DF8),
    ("furi_init", 0x080178C2),
)

MAX_HITS = 6
_snapshot_after = 14


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


def read_u32(bus, address):
    data = bus.ReadBytes(address, 4)
    return int(data[0]) | (int(data[1]) << 8) | (int(data[2]) << 16) | (int(data[3]) << 24)


def get_register(cpu, index):
    value = cpu.GetRegister(index)
    for attribute in ("Value", "RawValue"):
        try:
            return long(getattr(value, attribute))
        except Exception:
            pass
    return None


log("BOOT_PROBE_LOADED")
bus = self.Machine.SystemBus
machine = self.Machine
_hits = {}


def bkp1r_hook(cpu, address, width, value):
    count = _hits.get("bkp1r", 0) + 1
    _hits["bkp1r"] = count
    if count <= MAX_HITS:
        log("BKP1R read #%d PC=0x%08X LR=0x%08X value=0x%08X"
            % (count, get_register(cpu, 15) or 0, get_register(cpu, 14) or 0,
               read_u32(bus, BKP1R)))


def make_exec_hook(label):
    def hook(cpu, address, width, value):
        count = _hits.get(label, 0) + 1
        _hits[label] = count
        if count <= MAX_HITS:
            log("%-16s entered #%d PC=0x%08X LR=0x%08X"
                % (label, count, get_register(cpu, 15) or 0, get_register(cpu, 14) or 0))
    return hook


def arm():
    try:
        bus.AddWatchpointHook(BKP1R, SysbusAccessWidth.DoubleWord, Access.Read, bkp1r_hook)
    except Exception as exc:
        log("ARM_BKP1R_FAIL=%s" % exc)
    for label, address in FUNCTIONS:
        try:
            bus.AddWatchpointHook(address, SysbusAccessWidth.DoubleWord, Access.Execute,
                                  make_exec_hook(label))
        except Exception as exc:
            log("ARM_%s_FAIL=%s" % (label, exc))


arm()


def on_state(machine_object, arguments):
    arm()


try:
    machine.StateChanged += on_state
    log("STATE_HOOK_SET")
except Exception as exc:
    log("STATE_HOOK_FAIL=%s" % exc)


def snapshot():
    time.sleep(_snapshot_after)
    log("SNAPSHOT hits=%s" % _hits)
    for label, offset in (("BKP0R", 0x50), ("BKP1R", 0x54), ("BKP2R", 0x58)):
        try:
            log("  %s (0x%08X) = 0x%08X" % (label, 0x40002000 + offset,
                                            read_u32(bus, 0x40002000 + offset)))
        except Exception as exc:
            log("  %s FAILED %s" % (label, exc))


Thread(ThreadStart(snapshot)).Start()
