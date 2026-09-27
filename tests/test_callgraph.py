"""Tests for the Thumb branch decoder used to read the bootloader's logic."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.console import callgraph  # noqa: E402

REAL_ELF = ROOT / "firmware" / "flipper-z-f7-firmware-1.4.3.elf"


def bl(site: int, target: int) -> bytes:
    """Assemble a Thumb BL from ``site`` to ``target`` (little-endian halfwords)."""
    delta = target - (site + 4)
    value = delta & 0xFFFFFF
    first = 0xF000 | ((value >> 12) & 0x7FF)
    second = 0xC000 | ((value >> 1) & 0x7FF)
    if delta < 0:
        first |= 1 << 10
    return struct.pack("<HH", first, second)


def bw(site: int, target: int) -> bytes:
    """Assemble an unconditional Thumb B.W from ``site`` to ``target``."""
    delta = target - (site + 4)
    value = delta & 0xFFFFFF
    first = 0xF000 | ((value >> 12) & 0x7FF)
    second = 0x8000 | ((value >> 1) & 0x7FF)
    if delta < 0:
        first |= 1 << 10
    return struct.pack("<HH", first, second)


class BranchTests(unittest.TestCase):
    def test_backward_and_forward_calls_decode_exactly(self) -> None:
        base = 0x08001000
        code = bytearray(16)
        code[0:4] = bl(base, base + 0x40)          # forward call
        code[4:8] = bl(base + 4, base - 0x20)      # backward call
        code[8:12] = bw(base + 8, base + 0x80)     # tail branch
        decoded = sorted(callgraph.decode_branches(bytes(code), base))
        self.assertIn((base, base + 0x40, callgraph.CALL), decoded)
        self.assertIn((base + 4, base - 0x20, callgraph.CALL), decoded)
        self.assertIn((base + 8, base + 0x80, callgraph.TAIL), decoded)

    def test_data_that_looks_like_an_opcode_is_not_a_branch(self) -> None:
        blob = b"FZDPI1\n" + b"\x00\xF0\x00\x00"  # 0xF000 but not a BL/B.W pair
        self.assertEqual(list(callgraph.decode_branches(blob, 0x08000000)), [])

    def test_non_elf_is_rejected(self) -> None:
        with self.assertRaises(callgraph.ImageError):
            callgraph.ThumbImage(b"not an elf at all, really")


@unittest.skipUnless(REAL_ELF.exists(), "release firmware ELF not present")
class RealImageTests(unittest.TestCase):
    """The two addresses that explain the boot path (docs/BRINGUP_LOG.md section 14)."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(REAL_ELF, "rb") as handle:
            cls.image = callgraph.ThumbImage(handle.read(), str(REAL_ELF))
        cls.callers = cls.image.callers()

    def test_dfu_splash_is_drawn_by_the_dfu_loop_entered_from_boot_main(self) -> None:
        # flipper_boot_dfu_show_splash is drawn from inside flipper_boot_dfu_exec's
        # loop, and that loop is entered from the boot main's DFU branch - which is
        # why the splash keeps being redrawn (and the panel re-initialised) while
        # the bootloader waits.
        splash = sorted(site for site, _ in self.callers.get(0x080126B0, ()))
        self.assertEqual(splash, [0x0801270A])
        dfu_exec = sorted(site for site, _ in self.callers.get(0x08012708, ()))
        self.assertIn(0x080119B6, dfu_exec)

    def test_boot_mode_getter_reads_its_base_from_the_literal_pool(self) -> None:
        # furi_hal_rtc_get_boot_mode: LDR r3,=0x40002000 then [r3, #0x54].
        literal, value = self.image.literal_at(0x0800C03C)
        self.assertEqual(value, 0x40002000)
        self.assertEqual(self.image.bytes_at(0x0800C03C, 4).hex(), "024bd3f8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
