"""Tests for the intersection probe's own logic - no emulator required.

`_renode/intersect_probe.py` is Renode-side IronPython, but everything that decides
*what* it compares is plain Python, and that is the part the measurement stands on:

* the u8g2 window offsets (+0x48 user_x0, +0x4A user_x1, +0x4C user_y0, +0x4E
  user_y1, +0x50..+0x56 the clip window, +0x8C is_clip_window, +0x34 the tile buffer
  pointer) are decoded out of the firmware's own instructions, so they are asserted
  against a hand-filled structure here;
* the XBM -> tile buffer transform is asserted against the *measured* frame: the
  slideshow's first frame really does produce 1224 set bits and page 2 starting
  60 30 18 08 0C 04 06 06, which is what artifacts/slide50-postdraw.log shows the
  canvas holding right after the draw.

The artifact-backed test skips itself when artifacts/sdcard.img has not been built
(``py -3.9 src/flipper_emu/platform/sdcard_build.py``).
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "_renode"))

import intersect_probe  # noqa: E402

from flipper_emu.platform.sdcard_build import read_fat16  # noqa: E402

CANVAS = 0x200073B8
BUFFER = 0x20001004


class FakeBus:
    """A byte-addressable RAM the probe's reads can be pointed at."""

    def __init__(self, size: int = 0x400) -> None:
        self.memory = bytearray(size)

    def ReadBytes(self, address, count):
        if address + count > len(self.memory):
            raise ValueError("out of range")
        return self.memory[address : address + count]


class FakeCpu:
    def __init__(self, bus) -> None:
        self.Bus = bus


def window_bytes(user_x, user_y, clip_x, clip_y, is_clip):
    """A u8g2 context, with only the fields the probe reads filled in."""
    memory = bytearray(0x100)
    memory[0x34:0x38] = struct.pack("<I", BUFFER)
    memory[0x48:0x4A] = struct.pack("<H", user_x[0])
    memory[0x4A:0x4C] = struct.pack("<H", user_x[1])
    memory[0x4C:0x4E] = struct.pack("<H", user_y[0])
    memory[0x4E:0x50] = struct.pack("<H", user_y[1])
    memory[0x50:0x52] = struct.pack("<H", clip_x[0])
    memory[0x52:0x54] = struct.pack("<H", clip_x[1])
    memory[0x54:0x56] = struct.pack("<H", clip_y[0])
    memory[0x56:0x58] = struct.pack("<H", clip_y[1])
    memory[0x8C] = is_clip
    return memory


class WindowTests(unittest.TestCase):
    def test_field_offsets_and_full_screen_window(self) -> None:
        # The window every measured slideshow draw saw: the whole panel, no clipping.
        cpu = FakeCpu(FakeBus())
        cpu.Bus.memory[:0x100] = window_bytes((0, 128), (0, 64), (0, 0xFFFF), (0, 0xFFFF), 1)
        text = intersect_probe.window(cpu, 0)
        self.assertIn("buf=0x20001004", text)
        self.assertIn("user_x=0..128", text)
        self.assertIn("user_y=0..64", text)
        self.assertIn("clip_x=0..65535", text)
        self.assertIn("clip_y=0..65535", text)
        self.assertIn("is_clip=1", text)

    def test_a_narrowed_window_is_reported_as_such(self) -> None:
        # A page window like u8g2_apply_clip_window leaves behind: page 7 only.
        cpu = FakeCpu(FakeBus())
        cpu.Bus.memory[:0x100] = window_bytes((0, 128), (56, 64), (0, 0xFFFF), (0, 0xFFFF), 1)
        self.assertIn("user_y=56..64", intersect_probe.window(cpu, 0))

    def test_unreadable_structure_is_not_silently_a_window(self) -> None:
        cpu = FakeCpu(FakeBus(size=0x10))
        self.assertIn("unreadable", intersect_probe.window(cpu, CANVAS))


class TransformTests(unittest.TestCase):
    def xbm(self, dark):
        """A blank 128x64 XBM with the given (x, y) pixels set."""
        payload = bytearray(128 * 64 // 8)
        for x, y in dark:
            payload[y * 16 + (x // 8)] |= 1 << (x % 8)
        return payload

    def test_tile_layout_is_page_major_with_bit_y_mod_8(self) -> None:
        expected = intersect_probe.expected_buffer(self.xbm([(0, 0), (7, 3), (8, 8)]), 16, 0)
        self.assertEqual(expected[0], 0x01)  # page 0, column 0, bit 0 = pixel (0, 0)
        self.assertEqual(expected[7], 0x08)  # page 0, column 7, bit 3 = pixel (7, 3)
        self.assertEqual(expected[128 + 8], 0x01)  # page 1, column 8 = pixel (8, 8)
        self.assertEqual(intersect_probe.count_set_bits(expected), 3)

    def test_inverted_polarity_is_the_complement(self) -> None:
        xbm = self.xbm([(0, 0), (100, 40)])
        direct = intersect_probe.expected_buffer(xbm, 16, 0)
        inverted = intersect_probe.expected_buffer(xbm, 16, 1)
        self.assertEqual(len(direct), len(inverted))
        for index in range(len(direct)):
            self.assertEqual(direct[index] ^ inverted[index], 0xFF)

    def test_page_hex_reads_one_page(self) -> None:
        data = bytearray(1024)
        data[128] = 0xAB  # page 1
        self.assertEqual(intersect_probe.page_hex(data, 1), "AB00000000000000")
        self.assertEqual(intersect_probe.page_hex(data, 0), "0000000000000000")


class MeasuredFrameTests(unittest.TestCase):
    """The transform against the frame whose canvas bytes were measured."""

    def setUp(self) -> None:
        self.image = ROOT / "artifacts" / "sdcard.img"
        if not self.image.exists():
            self.skipTest("artifacts/sdcard.img not built")

    def test_frame_zero_matches_the_measured_canvas(self) -> None:
        with open(self.image, "rb") as handle:
            files = read_fat16(handle.read())
        name = [key for key in files if key.lower().endswith("slideshow")][0]
        blob = files[name]
        magic, version, width, height, count = struct.unpack_from("<IBBBB", blob, 0)
        self.assertEqual(magic, 0x72676468)
        self.assertEqual((width, height), (128, 64))
        self.assertGreaterEqual(count, 1)
        (size,) = struct.unpack_from("<H", blob, 8)
        payload = blob[10 : 10 + size]
        self.assertEqual(payload[0], 0x00, "raw (uncompressed) frame tag")
        xbm = payload[1:]
        self.assertEqual(len(xbm), 1024)

        expected = intersect_probe.expected_buffer(bytearray(xbm), 16, 0)
        # Exactly what artifacts/slide50-postdraw.log reports for POSTDRAW 1 and for
        # pages 2..7 of FLUSH 1/2 (the status bar covers pages 0..1 at flush time).
        self.assertEqual(intersect_probe.count_set_bits(expected), 1224)
        self.assertEqual(intersect_probe.page_hex(expected, 2), "603018080C040606")
        self.assertEqual(intersect_probe.page_hex(expected, 4), "00000000F806F10C")
        self.assertEqual(intersect_probe.page_hex(expected, 5), "0000A854AF70C798")
        self.assertEqual(intersect_probe.page_hex(expected, 6), "0000000102050205")
        self.assertEqual(intersect_probe.page_hex(expected, 0), "0000000000000000")


if __name__ == "__main__":
    unittest.main()
