"""Tests for the DfuSe firmware loader — no emulator required.

The suite runs against the real 1.4.3 release package when it is present in
``firmware/`` (override with ``FLIPPER_EMU_FW_DIR``) and always exercises the
parser with synthetic images, including the classic DfuSe fallback layout.
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.fw import dfu  # noqa: E402

FIRMWARE_DIR = Path(os.environ.get("FLIPPER_EMU_FW_DIR", str(ROOT / "firmware")))
REAL_DFU = FIRMWARE_DIR / "flipper-z-f7-full-1.4.3.dfu"

EXPECTED_NAME = "Flipper Zero F7"
EXPECTED_ELEMENT_ADDRESS = 0x08000000
EXPECTED_ELEMENT_SIZE = 768132
FLASH_APP_END = 0x080C0000


def build_classic_image(segments: List[Tuple[int, bytes]], name: str = "@Internal Flash") -> bytes:
    """Build a plain DfuSe 1.1a image (the fallback layout)."""
    body = name.encode("ascii").ljust(255, b"\x00")
    body += struct.pack("<I", sum(len(data) for _, data in segments))
    body += bytes([len(segments)])
    for address, data in segments:
        body += struct.pack("<II", address, len(data)) + data
    # dwImageSize counts everything up to the DFU suffix, header included.
    header = dfu.DFU_SIGNATURE + bytes([1]) + struct.pack("<I", len(body) + 11) + bytes([1])
    return header + body


class RealFirmwareTests(unittest.TestCase):
    """Assertions against the official package that ships these releases."""

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_DFU.exists():
            raise unittest.SkipTest("no firmware package at %s" % REAL_DFU)
        cls.image = dfu.parse_file(str(REAL_DFU))

    def test_layout_and_target_shape(self) -> None:
        self.assertEqual(self.image.layout, "flipper")
        self.assertEqual(self.image.version, 1)
        self.assertEqual(len(self.image.targets), 1)
        self.assertEqual(self.image.targets[0].name, EXPECTED_NAME)
        self.assertEqual(self.image.element_count, 1)

    def test_element_lands_in_flash(self) -> None:
        element = list(self.image.iter_elements())[0]
        self.assertEqual(element.address, EXPECTED_ELEMENT_ADDRESS)
        self.assertEqual(element.size, EXPECTED_ELEMENT_SIZE)
        # The application must stay clear of the reserved wireless-stack area.
        self.assertLessEqual(element.end, FLASH_APP_END)

    def test_element_chain_matches_the_file_tail(self) -> None:
        """The element data must be exactly the bytes sitting before the suffix."""
        element = list(self.image.iter_elements())[0]
        raw = REAL_DFU.read_bytes()
        start = self.image.payload_end - element.size
        self.assertEqual(raw[start : self.image.payload_end], element.data)
        self.assertEqual(self.image.target_bytes, self.image.payload_end - 11)

    def test_vector_table_looks_like_a_reset_vector(self) -> None:
        stack_pointer, reset_vector = list(self.image.iter_elements())[0].vector_words
        self.assertTrue(0x20000000 < stack_pointer <= 0x20040000, hex(stack_pointer))
        self.assertTrue(0x08000000 <= reset_vector < FLASH_APP_END, hex(reset_vector))
        self.assertEqual(reset_vector & 1, 1, "reset vector must have the Thumb bit set")

    def test_suffix_identifies_st_and_is_not_crc_enforced(self) -> None:
        suffix = self.image.suffix
        self.assertIsNotNone(suffix)
        self.assertEqual(suffix.id_vendor, dfu.STM_DFU_VID)
        self.assertIsNone(suffix.crc_ok, "CRC verification is opt-in")

    def test_official_crc_field_does_not_use_the_dfu_convention(self) -> None:
        """Documents an empirical fact: we cannot rely on this field."""
        image = dfu.parse_file(str(REAL_DFU), verify_crc=True)
        self.assertFalse(image.suffix.crc_ok)


class SyntheticImageTests(unittest.TestCase):
    def test_classic_layout_parses(self) -> None:
        segments = [(0x08000000, b"\x00\x10\x00\x20\x01\x00\x00\x08"),
                    (0x08001000, b"\xAA" * 64)]
        image = dfu.parse_bytes(build_classic_image(segments))
        self.assertEqual(image.layout, "classic")
        addresses = [(element.address, element.size) for element in image.iter_elements()]
        self.assertEqual(addresses, [(0x08000000, 8), (0x08001000, 64)])

    def test_bad_signature_is_rejected(self) -> None:
        with self.assertRaises(dfu.DfuFormatError):
            dfu.parse_bytes(b"NOTDF" + b"\x00" * 32)

    def test_truncated_file_is_rejected(self) -> None:
        with self.assertRaises(dfu.DfuFormatError):
            dfu.parse_bytes(b"DfuSe\x01")

    def test_declared_size_mismatch_is_rejected(self) -> None:
        blob = bytearray(build_classic_image([(0x08000000, b"\x00" * 16)]))
        blob[6:10] = struct.pack("<I", 999)
        with self.assertRaises(dfu.DfuValidationError):
            dfu.parse_bytes(bytes(blob))

    def test_garbage_body_is_rejected(self) -> None:
        blob = dfu.DFU_SIGNATURE + bytes([1]) + struct.pack("<I", 4096) + bytes([1])
        blob += b"\xCC" * 4096
        with self.assertRaises(dfu.DfuValidationError):
            dfu.parse_bytes(blob)

    def test_element_address_outside_memory_is_rejected(self) -> None:
        blob = build_classic_image([(0x12345678, b"\x00" * 16)])
        blob = bytearray(blob)
        blob[-24:-16] = struct.pack("<II", 0x12345678, 16)
        with self.assertRaises(dfu.DfuValidationError):
            dfu.parse_bytes(bytes(blob))


class CrcTests(unittest.TestCase):
    def test_dfu_flavour_check_value(self) -> None:
        """CRC-32 with final inversion (BZIP2/DFU-tooling flavour)."""
        self.assertEqual(dfu.crc32_dfu(b"123456789"), 0xFC891918)

    def test_stm32_flavour_check_value(self) -> None:
        """Same polynomial without final inversion, matching the CRC peripheral."""
        self.assertEqual(dfu.crc32_stm32(b"123456789"), 0x0376E6E7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
