# -*- coding: utf-8 -*-
"""Audit the recorded SPI2 stream for storage-device traffic.

Why this exists (Route B of the first-start-slideshow work): the SD card on
f18/WB55 is *not* on its own bus.  ``fh_furi_hal_spi_config.c`` puts both SD
handles on the display bus:

    const FuriHalSpiBusHandle furi_hal_spi_bus_handle_sd_slow = {
        .bus  = &furi_hal_spi_bus_d,      // the panel's bus (SPI2)
        .miso = &gpio_spi_d_miso, .mosi = &gpio_spi_d_mosi, .sck = &gpio_spi_d_sck,
        .cs   = &gpio_sdcard_cs,
    };

and in the platform file the panel is the *only* slave attached to ``spi2``:

    st7567: Antmicro.Renode.Peripherals.FlipperEmu.St7567Display @ spi2

So every byte the firmware shifts out on SPI2 - panel or card - is recorded by
`St7567Display` into ``artifacts/display-stream.bin`` as ``[tag][payload]``
records (tag 0x01 data/A0 high, 0x02 data/A0 low, 0x03 A0 changed, 0x04 RESET
changed, 0x05 data before A0 was known).  Parsing that file is a *sturdier*
instrument than an IronPython watchpoint: it is written by the C# model itself,
it cannot be lost when a probe thread dies, and it needs no re-run to re-read.

What the audit reports per stream:

* payload byte count and record counts per tag (a card conversation shows up as
  a jump in payload bytes with no matching panel traffic);
* the number of A0 changes, which tells whether the extra bytes were framed as
  panel writes or arrived while A0 sat still (the SD driver never touches A0);
* matches for the SD command frames the firmware's own driver sends - its
  ``sd_spi_send_cmd`` writes ``(cmd | 0x40)`` then the 4 argument bytes then
  ``crc | 0x01``, so CMD0 is exactly ``40 00 00 00 00 95`` (the constant in
  ``fh_furi_hal_sd.c``), CMD8 is ``48 00 00 01 AA 87``, and the app-init loop
  uses ``77`` (CMD55) and ``69`` (ACMD41);
* the byte offset of the first hit, which separates "the card was probed at
  boot" from "opcodes that merely resemble a frame inside pixel data".

Usage::

    py -3.9 src/flipper_emu/platform/sd_stream_audit.py artifacts/display-stream.bin
"""

# Host-side tool: plain Python (it only reads a file our emulator wrote).

from __future__ import annotations

import sys

TAG_DATA_A0_HIGH = 0x01
TAG_DATA_A0_LOW = 0x02
TAG_A0_CHANGED = 0x03
TAG_RESET_CHANGED = 0x04
TAG_DATA_UNKNOWN_A0 = 0x05

TAG_NAMES = {
    TAG_DATA_A0_HIGH: "data/A0 high",
    TAG_DATA_A0_LOW: "data/A0 low",
    TAG_A0_CHANGED: "A0 changed",
    TAG_RESET_CHANGED: "RESET changed",
    TAG_DATA_UNKNOWN_A0: "data (A0 unknown)",
}

#: ``(label, sequence)`` - the frames ``fh_furi_hal_sd.c`` actually transmits.
#: Full command frames only: a bare ``0x77`` inside pixel data is a coincidence.
SD_PATTERNS = (
    ("CMD0  GO_IDLE_STATE     40 00 00 00 00 95", b"\x40\x00\x00\x00\x00\x95"),
    ("CMD8  SEND_IF_COND      48 00 00 01 AA 87", b"\x48\x00\x00\x01\xAA\x87"),
    ("CMD55 APP_CMD           77 00 00 00 00 01", b"\x77\x00\x00\x00\x00\x01"),
    ("CMD55 APP_CMD (any arg) 77", b"\x77"),
    ("CMD58 READ_OCR          7A 00 00 00 00 01", b"\x7A\x00\x00\x00\x00\x01"),
    ("CMD17 READ_SINGLE_BLOCK 51", b"\x51"),
    ("CMD24 WRITE_SINGLE_BLOCK 58", b"\x58"),
    ("CMD13 SEND_STATUS       4D", b"\x4D"),
    ("ACMD41 SD_APP_OP_COND   69", b"\x69"),
    ("data token 0xFE", b"\xFE"),
)

#: A flash chip on the same bus would answer these; kept so the audit rules the
#: quad/SPI flash theory out on the bus that actually carries storage traffic.
FLASH_PATTERNS = (
    ("flash RDID 9F", b"\x9F"),
    ("flash SFDP 5A + header", b"\x5A\x53\x46\x44\x50"),
    ("flash RELEASE-POWER-DOWN AB", b"\xAB"),
)


def parse(path: str):
    with open(path, "rb") as stream:
        blob = stream.read()
    if blob[:7] == b"FZDPI1\n":
        blob = blob[7:]
    payload = bytearray()
    tag_counts = {}
    index = 0
    while index + 1 < len(blob):
        tag, value = blob[index], blob[index + 1]
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if tag in (TAG_DATA_A0_HIGH, TAG_DATA_A0_LOW, TAG_DATA_UNKNOWN_A0):
            payload.append(value)
        index += 2
    return tag_counts, bytes(payload), len(blob)


def scan(blob: bytes, patterns):
    hits = []
    for label, pattern in patterns:
        offsets = []
        start = 0
        while len(offsets) < 5:
            found = blob.find(pattern, start)
            if found < 0:
                break
            offsets.append(found)
            start = found + 1
        if offsets:
            # Count the rest without keeping the offsets: a hit only means
            # something when the surrounding window is shown next to it, because
            # panel pixel data is repetitive enough to fake short opcodes.
            count = len(offsets)
            start = offsets[-1] + 1
            while count < 1000000:
                found = blob.find(pattern, start)
                if found < 0:
                    break
                count += 1
                start = found + 1
            hits.append((label, count, offsets))
    return hits


def hexdump(blob: bytes, offset: int, length: int = 32) -> str:
    start = max(0, offset - 8)
    return " ".join("%02X" % value for value in blob[start:start + length])


def histogram(blob: bytes, top: int = 6):
    counts = {}
    for value in blob:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: -item[1])[:top]
    return " ".join("%02X=%d" % (value, count) for value, count in ranked)


def report(path: str) -> None:
    tag_counts, payload, raw = parse(path)
    print("stream: %s" % path)
    print("  raw bytes: %d -> records: %d" % (raw, sum(tag_counts.values())))
    for tag in sorted(tag_counts):
        print("    tag 0x%02X %-20s %d" % (tag, TAG_NAMES.get(tag, "?"), tag_counts[tag]))
    print("  payload bytes (what the master shifted out): %d" % len(payload))
    print("  payload byte histogram (top 6): %s" % histogram(payload))

    print("  SD command frames:")
    sd_hits = scan(payload, SD_PATTERNS)
    if not sd_hits:
        print("    none")
    for label, count, offsets in sd_hits:
        print("    %-42s count=%-8d first@%d" % (label, count, offsets[0]))
        if count <= 64:
            print("        offsets: %s" % ", ".join(str(item) for item in offsets))
        print("        context: %s" % hexdump(payload, offsets[0]))

    print("  flash opcodes:")
    flash_hits = scan(payload, FLASH_PATTERNS)
    if not flash_hits:
        print("    none")
    for label, count, offsets in flash_hits:
        print("    %-42s count=%-8d first@%d" % (label, count, offsets[0]))
        if count <= 64:
            print("        offsets: %s" % ", ".join(str(item) for item in offsets))
        print("        context: %s" % hexdump(payload, offsets[0]))


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    for path in argv[1:]:
        report(path)
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
