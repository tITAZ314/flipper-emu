# -*- coding: utf-8 -*-
"""Dump the LPTIM models' own state while the firmware runs.

Deliberately *not* a bus read: byte-wise ``ReadBytes`` calls from this Python thread
returned the wrong window for LPTIM1 earlier in this project (the address-0 alias
contents - the initial SP and reset vector), so this probe asks the model itself via
``DumpState()`` instead. That path is reliable and shows exactly what the firmware's
tickless handshake did: starts, compare/auto-reload matches, and the IRQ level.

Output: artifacts/lptim-probe.log
"""

import time
from System.Threading import Thread, ThreadStart

OUT = r"C:\Users\gtttr\auraesp32\flipper-emu\artifacts\lptim-probe.log"
SAMPLES = 6
INTERVAL = 2.0


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


def find_peripheral(name):
    resolved = self.TryFindPeripheralByName(name)
    if isinstance(resolved, tuple):
        return resolved[1] if len(resolved) > 1 else None
    return resolved


machine = self.Machine
log("LPTIM_PROBE_LOADED")


def sample():
    for index in range(SAMPLES):
        time.sleep(INTERVAL)
        for name in ("lptim1", "lptim2"):
            peripheral = find_peripheral(name)
            try:
                log("t+%2ds %-7s %s" % (INTERVAL * (index + 1), name, peripheral.DumpState()))
            except Exception as exc:
                log("t+%2ds %-7s FAILED %s" % (INTERVAL * (index + 1), name, exc))


Thread(ThreadStart(sample)).Start()
