"""Tests for the SPI2 stream audit - no emulator required.

The audit exists because the SD card shares SPI2 with the panel, so the panel
model's recorder file is the only place the card conversation is visible. Its
first version reported "CMD0 count=128000" *and* crashed on the second hit shape
(it kept unpacking a `first` offset after the scanner started returning a list),
which is exactly the kind of failure that would have been read as real traffic.
These tests pin the parsing, the counting and the verified-context output.

The fixtures below are literal copies of the two streams measured from the
firmware: the no-card baseline (panel bytes only) and the card-present run,
which opens with 80 dummy clocks followed by the driver's CMD0 frame.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.platform import sd_stream_audit  # noqa: E402

#: What the card-present run put on the wire right after the 80 dummy clocks:
#: two ignored bytes, then ``sd_spi_send_cmd(CMD0, 0, 0x95, R1)`` verbatim.
CMD0_FRAME = b"\x40\x00\x00\x00\x00\x95"


def record(tag: int, payload: int) -> bytes:
    return bytes([tag, payload])


class ParseTests(unittest.TestCase):
    def write_stream(self, blob: bytes, with_magic: bool = True) -> str:
        handle, path = tempfile.mkstemp(suffix=".bin")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as stream:
            if with_magic:
                # Same 7-byte header the recorder model writes ("FZDPI1\n").
                stream.write(b"FZDPI1\n")
            stream.write(blob)
        return path

    def test_header_is_stripped_and_only_data_tags_become_payload(self) -> None:
        blob = (
            record(sd_stream_audit.TAG_A0_CHANGED, 0)
            + record(sd_stream_audit.TAG_RESET_CHANGED, 1)
            + record(sd_stream_audit.TAG_DATA_A0_LOW, 0xE2)
            + record(sd_stream_audit.TAG_DATA_A0_HIGH, 0x3C)
            + record(sd_stream_audit.TAG_DATA_UNKNOWN_A0, 0x9F)
        )
        path = self.write_stream(blob)
        tag_counts, payload, raw = sd_stream_audit.parse(path)
        self.assertEqual(payload, bytes([0xE2, 0x3C, 0x9F]))
        self.assertEqual(tag_counts[sd_stream_audit.TAG_A0_CHANGED], 1)
        self.assertEqual(tag_counts[sd_stream_audit.TAG_DATA_A0_HIGH], 1)
        # `raw` is the record bytes after the header, which is what the audit
        # prints as "raw bytes: N -> records: N/2".
        self.assertEqual(raw, len(blob))

    def test_stream_without_the_recorder_header_is_still_readable(self) -> None:
        blob = record(sd_stream_audit.TAG_DATA_A0_LOW, 0x11)
        path = self.write_stream(blob, with_magic=False)
        _, payload, _ = sd_stream_audit.parse(path)
        self.assertEqual(payload, b"\x11")

    def test_trailing_odd_byte_is_ignored(self) -> None:
        blob = record(sd_stream_audit.TAG_DATA_A0_LOW, 0x11) + b"\x01"
        path = self.write_stream(blob)
        _, payload, _ = sd_stream_audit.parse(path)
        self.assertEqual(payload, b"\x11")


class ScanTests(unittest.TestCase):
    def test_cmd0_frame_is_counted_with_its_offsets(self) -> None:
        payload = b"\xFF" * 8 + CMD0_FRAME + b"\xFF\xFF" + CMD0_FRAME
        hits = dict(
            (label, (count, offsets))
            for label, count, offsets in sd_stream_audit.scan(
                payload, (("CMD0", CMD0_FRAME),)
            )
        )
        count, offsets = hits["CMD0"]
        self.assertEqual(count, 2)
        # The scanner must return a list of ints: the first version returned a
        # list where the report still expected a scalar and raised TypeError.
        self.assertEqual(offsets, [8, 16])
        self.assertTrue(all(isinstance(item, int) for item in offsets))

    def test_lone_opcode_byte_is_not_a_command_frame(self) -> None:
        # Panel pixel data contains 0x40 all the time; only the full frame counts.
        payload = b"\x40" * 32
        hits = sd_stream_audit.scan(payload, (("CMD0", CMD0_FRAME),))
        self.assertEqual(hits, [])

    def test_histogram_ranks_the_most_common_bytes(self) -> None:
        text = sd_stream_audit.histogram(b"\x00\x00\x00\xFF\x95")
        self.assertTrue(text.startswith("00=3"))

    def test_hexdump_shows_bytes_before_the_hit(self) -> None:
        rendered = sd_stream_audit.hexdump(b"\xAA\xBB" + CMD0_FRAME, 2, length=8)
        self.assertEqual(rendered, "AA BB 40 00 00 00 00 95")


class ReportTests(unittest.TestCase):
    def test_report_prints_hit_context_and_the_no_flash_conclusion(self) -> None:
        blob = b"\xFF" * 4 + CMD0_FRAME
        handle, path = tempfile.mkstemp(suffix=".bin")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "wb") as stream:
            stream.write(b"FZDPI1\n")
            for value in blob:
                stream.write(record(sd_stream_audit.TAG_DATA_A0_LOW, value))

        captured = io.StringIO()
        with redirect_stdout(captured):
            sd_stream_audit.report(path)
        text = captured.getvalue()

        self.assertIn("payload bytes (what the master shifted out): 10", text)
        self.assertIn("CMD0", text)
        self.assertIn("count=1", text)
        self.assertIn("context: FF FF FF FF 40 00 00 00 00 95", text)
        self.assertIn("flash opcodes:", text)
        self.assertIn("    none", text)


if __name__ == "__main__":
    unittest.main()
