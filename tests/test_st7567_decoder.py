"""Tests for the ST7567 decoder - no emulator required.

The byte sequences here are copied verbatim from the stream captured off the real
firmware (see `docs/BRINGUP_LOG.md` §10), so the inference rules are checked
against what the emulated firmware actually sends.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.frontend import st7567  # noqa: E402

#: Init sequence and the start of the first page write, captured from the firmware:
#: E2 A2 A0 C8 (reset, bias 1/9, ADC normal, COM reverse)
#: 40 25 81 20 2F A4 AF (start line, resistor ratio, contrast=0x20, power on,
#:                        all-points normal, display on)
#: 10 00 B0 (column 0, page 0) followed by 132 data bytes.
CAPTURED_INIT = bytes(
    [0xE2, 0xA2, 0xA0, 0xC8, 0x40, 0x25, 0x81, 0x20, 0x2F, 0xA4, 0xAF, 0x10, 0x00, 0xB0]
)
CAPTURED_PAGE = bytes([0x00] * 132)


class CapturedStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decoder = st7567.St7567Decoder()
        self.decoder.feed_records(CAPTURED_INIT + CAPTURED_PAGE)

    def test_init_sequence_is_understood(self) -> None:
        self.assertTrue(self.decoder.display_on)
        self.assertEqual(self.decoder.contrast, 0x20)
        self.assertTrue(self.decoder.com_reversed)
        self.assertFalse(self.decoder.adc_reversed)
        self.assertFalse(self.decoder.all_points_on)
        self.assertEqual(self.decoder.start_line, 0)

    def test_page_write_lands_in_page_memory(self) -> None:
        self.assertEqual(self.decoder.data_bytes, 132)
        self.assertEqual(bytes(self.decoder.memory[:132]), CAPTURED_PAGE)
        self.assertEqual(self.decoder.column, 0)  # wrapped after 132 columns

    def test_blank_page_renders_blank(self) -> None:
        for row in self.decoder.render():
            self.assertEqual(set(row), {0})

    def test_describe_mentions_the_state(self) -> None:
        text = self.decoder.describe()
        self.assertIn("on=True", text)
        self.assertIn("contrast=32", text)


class FramingTests(unittest.TestCase):
    def test_command_parameter_is_not_written_as_data(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF, 0x81, 0x24, 0xB0]) + bytes([0xAA] * 132))
        self.assertEqual(decoder.contrast, 0x24)
        self.assertEqual(decoder.memory[0], 0xAA)

    def test_data_that_looks_like_a_command_is_data_inside_a_run(self) -> None:
        decoder = st7567.St7567Decoder()
        # 0x81/0xB0 inside the 132-byte run must be stored, not interpreted.
        payload = bytes([0x81, 0xB0, 0x00] + [0x11] * 129)
        decoder.feed_records(bytes([0xAF, 0xB0]) + payload)
        self.assertEqual(decoder.contrast, 0)
        self.assertEqual(bytes(decoder.memory[0:3]), bytes([0x81, 0xB0, 0x00]))

    def test_column_addressing_uses_two_nibbles(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF, 0x12, 0x03, 0xB0, 0x55]))
        self.assertEqual(decoder.column, 0x24)  # 0x23 written, then incremented
        self.assertEqual(decoder.memory[0x23], 0x55)

    def test_explicit_a0_tags_override_inference(self) -> None:
        decoder = st7567.St7567Decoder()
        # A blob without the record header is decoded as raw panel bytes, so the
        # tagged path is exercised through feed_byte directly: 0x81 then its
        # parameter both arrive in command mode.
        decoder.feed_byte(0x81, a0=0)
        decoder.feed_byte(0x20, a0=0)
        self.assertEqual(decoder.contrast, 0x20)
        self.assertEqual(decoder.data_bytes, 0)

    def test_record_stream_with_header_is_parsed_as_records(self) -> None:
        decoder = st7567.St7567Decoder()
        blob = (
            st7567.RECORD_MAGIC
            + bytes([st7567.TAG_DATA_A0_LOW, 0xAF])
            + bytes([st7567.TAG_DATA_A0_LOW, 0xB0])
            + bytes([st7567.TAG_DATA_A0_HIGH, 0x0F])
        )
        decoder.feed_records(blob)
        self.assertTrue(decoder.display_on)
        self.assertEqual(decoder.data_bytes, 1)
        self.assertEqual(decoder.memory[0], 0x0F)

    def test_software_reset_clears_memory(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF, 0xB0]) + bytes([0xFF] * 132))
        self.assertEqual(decoder.memory[0], 0xFF)
        decoder.feed_records(bytes([0xE2]))
        self.assertEqual(decoder.memory[0], 0x00)
        self.assertFalse(decoder.display_on)

    def test_reset_line_keeps_the_last_image(self) -> None:
        """A /RESET pulse clears the addressing state but not the visible frame.

        The Flipper boot path pulses /RESET once per frame and repaints every page;
        clearing the panel on each pulse would show the host blank flashes between
        good frames (that is exactly how the boot splash looked "torn").
        """
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF, 0xB0]) + bytes([0xFF] * 132))
        self.assertEqual(decoder.memory[0], 0xFF)
        decoder.feed_records(st7567.RECORD_MAGIC + bytes([st7567.TAG_RESET_CHANGED, 1]))
        self.assertEqual(decoder.memory[0], 0xFF, "the last frame stays on screen")
        self.assertTrue(decoder.in_reset)
        self.assertEqual(decoder.page, 0)
        self.assertEqual(decoder.column, 0)
        self.assertFalse(decoder.display_on)
        decoder.feed_records(st7567.RECORD_MAGIC + bytes([st7567.TAG_RESET_CHANGED, 0]))
        self.assertFalse(decoder.in_reset)
        # Data sent after the release lands where the firmware addresses it (the
        # records need the header: without it a blob is raw panel bytes by design).
        decoder.feed_records(st7567.RECORD_MAGIC + bytes([st7567.TAG_DATA_A0_LOW, 0xB1]))
        decoder.feed_records(st7567.RECORD_MAGIC + bytes([st7567.TAG_DATA_A0_HIGH, 0x0F]))
        self.assertEqual(decoder.memory[st7567.COLUMNS], 0x0F)


class RenderTests(unittest.TestCase):
    def test_bit0_is_the_top_row_of_the_page(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF]))
        decoder.set_pixel(5, 1)
        rows = decoder.render()
        self.assertEqual(rows[1][5], 255)
        self.assertEqual(rows[0][5], 0)

    def test_display_off_blanks_the_render(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAE]))
        decoder.set_pixel(3, 3)
        self.assertEqual(set(decoder.render()[3]), {0})

    def test_invert_flips_pixels(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xAF, 0xA7]))
        decoder.set_pixel(3, 3)
        self.assertEqual(decoder.render()[3][3], 0)
        self.assertEqual(decoder.render()[3][4], 255)

    def test_all_points_on_lights_everything(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(bytes([0xA5]))
        self.assertEqual(decoder.render()[10][10], 255)

    def test_png_is_written(self) -> None:
        decoder = st7567.St7567Decoder()
        decoder.feed_records(CAPTURED_INIT + bytes([0xFF] * 132))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "frame.png")
            decoder.to_png(path, scale=2)
            blob = Path(path).read_bytes()
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(int.from_bytes(blob[16:20], "big"), st7567.WIDTH * 2)
        self.assertEqual(int.from_bytes(blob[20:24], "big"), st7567.HEIGHT * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
