"""Tests for the live display tail - no emulator and no window required.

`DisplayStreamTail` is the piece that makes the UI live: it follows the recorder
file written by `St7567Display` and feeds whole records into the decoder. These
tests drive it with files written on the fly, including the awkward cases (partial
records, a file that restarts, and a backlog too large to catch up with).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.frontend import st7567, ui_tk  # noqa: E402


def records(tag: int, values) -> bytes:
    blob = bytearray()
    for value in values:
        blob.extend([tag, value])
    return bytes(blob)


class TailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "display-stream.bin")
        with open(self.path, "wb") as handle:
            handle.write(st7567.RECORD_MAGIC)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def append(self, blob: bytes) -> None:
        with open(self.path, "ab") as handle:
            handle.write(blob)

    def test_commands_alone_do_not_change_the_framebuffer(self) -> None:
        tail = ui_tk.DisplayStreamTail(self.path)
        self.assertFalse(tail.poll())
        # A0 low = command: display on, then page 0 (which opens a 132-byte run).
        self.append(records(st7567.TAG_DATA_A0_LOW, [0xAF, 0xB0]))
        self.assertFalse(tail.poll(), "display-on and page-select do not touch page memory")
        self.assertGreater(tail.events, 0)
        self.assertTrue(tail.decoder.display_on)
        self.assertEqual(tail.decoder.page, 0)

    def test_pixel_data_is_tailed_incrementally(self) -> None:
        tail = ui_tk.DisplayStreamTail(self.path)
        self.append(records(st7567.TAG_DATA_A0_LOW, [0xAF, 0xB0]))
        tail.poll()
        # A0 high = pixel data, landing at page 0, column 0 onwards.
        self.append(records(st7567.TAG_DATA_A0_HIGH, [0x0F, 0x00, 0xAA]))
        self.assertTrue(tail.poll(), "data write changes the framebuffer")
        self.assertEqual(tail.decoder.memory[0], 0x0F)
        self.assertEqual(tail.decoder.memory[1], 0x00)
        self.assertEqual(tail.decoder.memory[2], 0xAA)
        self.assertEqual(tail.frames, 1)
        self.assertEqual(tail.poll(), False, "nothing new to consume")

    def test_partial_record_is_held_back(self) -> None:
        tail = ui_tk.DisplayStreamTail(self.path)
        self.append(records(st7567.TAG_DATA_A0_LOW, [0xAF, 0xB0]))
        self.assertFalse(tail.poll())
        # A recorder writes tag+payload pairs, so half a record is not decodable:
        # the tail must keep it for the next poll rather than shift the framing.
        self.append(bytes([st7567.TAG_DATA_A0_HIGH]))
        self.assertFalse(tail.poll())
        self.assertEqual(tail.decoder.memory[0], 0x00)
        self.append(bytes([0x5A]))  # completes the record
        self.assertTrue(tail.poll(), "the completed record is consumed")
        self.assertEqual(tail.decoder.memory[0], 0x5A)

    def test_restarted_file_resets_the_decoder(self) -> None:
        tail = ui_tk.DisplayStreamTail(self.path)
        self.append(records(st7567.TAG_DATA_A0_LOW, [0xAF, 0xB0]))
        self.append(records(st7567.TAG_DATA_A0_HIGH, [0xFF]))
        tail.poll()
        self.assertEqual(tail.decoder.memory[0], 0xFF)
        # A new run recreates the file with just a header.
        with open(self.path, "wb") as handle:
            handle.write(st7567.RECORD_MAGIC)
        self.assertFalse(tail.poll())
        self.assertEqual(tail.decoder.memory[0], 0x00)

    def test_large_backlog_is_skipped_to_stay_live(self) -> None:
        tail = ui_tk.DisplayStreamTail(self.path)
        tail.poll()
        self.append(records(st7567.TAG_DATA_A0_HIGH, [0x00] * (ui_tk.SKIP_THRESHOLD + 64)))
        tail.poll()
        self.assertEqual(tail.resyncs, 1, "the tail jumped forward instead of lagging")
        # Records live on a grid that starts after the 7-byte header, so the new
        # offset must stay on that grid (else every following record is misaligned).
        self.assertEqual(tail.offset % 2, len(st7567.RECORD_MAGIC) % 2, "resync stays aligned")
        self.assertGreater(tail.offset, len(st7567.RECORD_MAGIC))

    def test_missing_file_is_tolerated(self) -> None:
        tail = ui_tk.DisplayStreamTail(os.path.join(self.directory.name, "not-there.bin"))
        self.assertFalse(tail.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
