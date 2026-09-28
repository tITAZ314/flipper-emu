# -*- coding: utf-8 -*-
"""What does the firmware actually send when it looks for internal storage?

Route B of the first-start-slideshow work (docs/ISSUES_AND_LOGS.md P25): the
Desktop only enters its slideshow scene `if(storage_file_exists(storage,
"/int/.slideshow"))`, and this emulator has no storage backend at all - so the
gate is invisible from the outside.  This probe turns it into a measurement by
hooking the SPI data registers, which is where every byte the firmware sends to
a storage device has to pass.

Why these two registers:

* ``SPI1`` (``0x40013000``) is the bus the firmware's
  ``furi_hal_spi_bus_handle_external`` handle hangs off (CS ``gpio_ext_pa4``,
  pins PA6/PA7/PB3 - ``fh_furi_hal_spi_config.c``), which is also where the run
  log shows storage-shaped traffic ("SPI transmission while no SPI peripheral is
  connected").
* ``SPI2`` (``0x40003800``) carries the ST7567 panel, which decodes cleanly; it
  is included as a **control**: the panel stream would be visibly corrupted by
  storage traffic on that bus, so seeing flash/SD opcodes here would mean the
  bus assumption is wrong and must be re-derived rather than assumed.

Three access widths are armed on each (Renode hooks are width-specific and the
width a model registers with is not visible from the platform file); every log
line is tagged with the width that actually fired, so the answer shows which one
the peripheral uses instead of a stream tripled by guesswork.

Output: artifacts/storage-probe.log
"""

from Antmicro.Renode.Peripherals.Bus import Access, SysbusAccessWidth
from System.Threading import Thread, ThreadStart
import time

OUT = r"C:\Users\gtttr\auraesp32\flipper-emu\artifacts\storage-probe.log"

#: ``(label, address)`` - SPI DR is offset 0x0C (CR1 0x00, CR2 0x04, SR 0x08);
#: QUADSPI's command register CCR is 0x14 and its data register DR is 0x20.
#: QUADSPI is in the probe because the ZERO flash commands on either SPI bus in
#: the v1 run left open the possibility that the internal volume sits on the
#: quad interface our address map declares "unused".
BUSES = (
    ("spi1", 0x40013000 + 0x0C),
    ("spi2", 0x40003800 + 0x0C),
    ("qspi-ccr", 0xA0001000 + 0x14),
    ("qspi-dr", 0xA0001000 + 0x20),
)

#: Registers worth counting *reads* of: polling ``QUADSPI_SR`` is what a driver
#: does while a flash command is in flight, so the count separates "never used"
#: from "used but answering nothing".
READS = (
    ("qspi-sr", 0xA0001000 + 0x08),
)

#: ``(label, width)`` - the model's registration width is not visible from the
#: platform file, so arm all three and let the log say which one fires.
WIDTHS = (
    ("byte", SysbusAccessWidth.Byte),
    ("word", SysbusAccessWidth.Word),
    ("dword", SysbusAccessWidth.DoubleWord),
)

#: Command patterns that identify a *device* rather than a coincidence: panel
#: pixel data contains bytes equal to 0x9F/0x40 all the time (the v1 summary
#: "recognised" SD CMD0 and flash opcodes inside a frame of the display), so a
#: hit only counts when the whole opening sequence matches.  A flash chip
#: answers 0x9F with a 3-byte JEDEC id, 0x5A with an SFDP header ``SFDP`` and
#: 0xAB on release-power-down; an SPI-mode card starts with 0x40 (CMD0) followed
#: by the 0x95 CRC, then CMD55/ACMD41 (0x77/0x69) while it spins up.
COMMAND_PATTERNS = (
    ("flash RDID 9F", "\x9F"),
    ("flash SFDP 5A + SFDP hdr", "\x5A\x53\x46\x44\x50"),
    ("flash SFDP 5A", "\x5A"),
    ("flash RELEASE-POWER-DOWN AB", "\xAB"),
    ("flash READ 03 00 00 00", "\x03\x00\x00\x00"),
    ("flash RDSR 05", "\x05"),
    ("flash WREN 06", "\x06"),
    ("sd CMD0 40 00 00 00 00 95", "\x40\x00\x00\x00\x00\x95"),
    ("sd CMD55 77", "\x77"),
    ("sd ACMD41 69", "\x69"),
)
COMMAND_SAMPLE_LIMIT = 10

#: Bytes kept per (bus, width), and the pause before the summary is printed.
#: The v1 run capped this at 4096 and truncated SPI2 after ~2 frames of panel
#: data, which hid whatever came later - the display bus alone produces ~4 KB/s.
LIMIT = 1500000
SUMMARY_AFTER_SECONDS = 12


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


#: ``label -> [bytes]`` captured per firing width, plus read counters.
captured = {}
armed = []
read_counts = {}


def make_hook(label, width_label):
    def hook(cpu, address, width, value):
        buffer = captured.setdefault((label, width_label), [])
        if len(buffer) < LIMIT:
            buffer.append(int(value) & 0xFF)
    return hook


log("STORAGE_PROBE_LOADED")

bus = self.Machine.SystemBus


def make_read_hook(label):
    def hook(cpu, address, width, value):
        read_counts[label] = read_counts.get(label, 0) + 1
    return hook


def arm():
    # Never call RemoveAllWatchpointHooks: after the machine is disposed it walks
    # into native code and takes an AccessViolation (learned in reset_probe.py).
    for label, address in BUSES:
        for width_label, width in WIDTHS:
            try:
                bus.AddWatchpointHook(address, width, Access.Write, make_hook(label, width_label))
                armed.append("%s/%s" % (label, width_label))
            except Exception as exc:
                log("ARM_FAIL %s/%s = %s" % (label, width_label, exc))
    for label, address in READS:
        for width_label, width in WIDTHS:
            try:
                bus.AddWatchpointHook(address, width, Access.Read, make_read_hook(label))
                armed.append("read:%s/%s" % (label, width_label))
            except Exception as exc:
                log("ARM_FAIL read:%s/%s = %s" % (label, width_label, exc))


arm()
log("ARMED %s" % ", ".join(armed))


def on_state(machine_object, arguments):
    arm()


try:
    self.Machine.StateChanged += on_state
    log("STATE_HOOK_SET")
except Exception as exc:
    log("STATE_HOOK_FAIL=%s" % exc)


def compress(chunk, minimum_run=8):
    """Collapse long runs: panel page data is mostly zeros and drowns the log."""
    parts = []
    index = 0
    while index < len(chunk):
        byte = chunk[index]
        run = 1
        while index + run < len(chunk) and chunk[index + run] == byte and run < 4096:
            run += 1
        if run >= minimum_run:
            parts.append("%02Xx%d" % (byte, run))
        else:
            for step in range(run):
                parts.append("%02X" % chunk[index + step])
        index += run
    return parts


def count_patterns(blob):
    """Count the device-identifying command sequences present in ``blob``."""
    hits = []
    for name, pattern in COMMAND_PATTERNS:
        count = 0
        first = None
        start = 0
        while count < 100000:
            found = blob.find(pattern, start)
            if found < 0:
                break
            if first is None:
                first = found
            count += 1
            start = found + 1
        if count:
            hits.append((name, count, first))
    return hits


def summary():
    time.sleep(SUMMARY_AFTER_SECONDS)
    log("SUMMARY after %ds - captured: %s"
        % (SUMMARY_AFTER_SECONDS,
           ", ".join("%s/%s=%d" % (label, width, len(data))
                     for (label, width), data in sorted(captured.items())) or "none"))
    log("  read counts: %s" % (read_counts if read_counts else "none"))
    for (label, width), data in sorted(captured.items()):
        if not data:
            continue
        parts = compress(data)
        log("  %s/%s: %d bytes -> %d tokens; header: %s"
            % (label, width, len(data), len(parts), " ".join(parts[:32])))
        blob = "".join(chr(byte) for byte in data)
        hits = count_patterns(blob)
        for name, count, first in hits[:COMMAND_SAMPLE_LIMIT]:
            log("      pattern %-30s count=%d first@%d" % (name, count, first))
        if not hits:
            log("      no flash/SD command sequence present on this bus")


Thread(ThreadStart(summary)).Start()