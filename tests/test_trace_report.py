"""Tests for the debug console's log analysis.

The sample lines below are verbatim formats captured from a Renode 1.17 session
of this project, so the tests fail if the analyser stops matching reality.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.console import trace  # noqa: E402

SAMPLE_LOG = "\n".join(
    [
        "[10:00:00.0000] [INFO] sysbus: Loading block of 1048576 bytes length at 0x8000000.",
        "[10:00:00.0001] [INFO] cpu: Setting initial values: PC = 0x8011B9D, SP = 0x20030000.",
        "[10:00:00.0002] [WARNING] sysbus: [cpu: 0x8011B4E] ReadDoubleWord from non existing "
        "peripheral at 0x58000C00.",
        "[10:00:00.0003] [WARNING] sysbus: [cpu: 0x8011B54] WriteDoubleWord to non existing "
        "peripheral at 0x58000C00, value 0x1.",
        "[10:00:00.0004] [WARNING] rcc: Unhandled read from offset 0x58 (ControlStatus+0x8).",
        "[10:00:00.0005] [WARNING] rcc: Unhandled write to offset 0x8. Unhandled bits: [16-18] "
        "when writing value 0x70000. Tags: RESERVED (0x7).",
        "[10:00:00.0006] [WARNING] exti: Unhandled read from offset 0x80 (PendingRegister+0x6c).",
        "[10:00:00.0007] [INFO] nvic: Resetting platform with SYSRESETREQ",
        "[10:00:00.0008] [WARNING] sysbus: Tried to access bytes at non-existing peripheral in "
        "range <0x08000000, 0x080FFFFF>.",
        "Unhandled exception. Microsoft.Scripting.ArgumentTypeException: expected UInt64, "
        "got NoneType",
    ]
)


class LogAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = trace.analyse(SAMPLE_LOG)

    def test_unmapped_accesses_are_grouped(self) -> None:
        accesses = [hit for hit in self.report.accesses if hit.kind in ("read", "write")]
        self.assertEqual(len(accesses), 2)
        self.assertEqual(sorted(hit.kind for hit in accesses), ["read", "write"])
        self.assertTrue(all(hit.address == 0x58000C00 for hit in accesses))

    def test_unmapped_access_is_attributed_to_the_bus_map(self) -> None:
        """0x58000C00 is the IPCC block, which we only stub."""
        self.assertIn("IPCC", self.report.missing_peripherals())
        description = self.report.accesses[0].describe()
        self.assertIn("IPCC", description)
        self.assertIn("stub:", description)

    def test_model_offsets_are_counted_per_peripheral_and_direction(self) -> None:
        self.assertEqual(self.report.model_offsets[("rcc", 0x58, "read")], 1)
        self.assertEqual(self.report.model_offsets[("rcc", 0x8, "write")], 1)
        self.assertEqual(self.report.model_offsets[("exti", 0x80, "read")], 1)

    def test_resets_and_cpu_starts_are_tracked(self) -> None:
        self.assertEqual(self.report.resets, {"SYSRESETREQ": 1})
        self.assertEqual(self.report.cpu_starts, [(0x08011B9D, 0x20030000)])
        self.assertFalse(self.report.boot_loops)

    def test_boot_loop_is_detected(self) -> None:
        looping = trace.analyse("\n".join([SAMPLE_LOG] * 4))
        self.assertEqual(looping.reset_count, 4)
        self.assertTrue(looping.boot_loops)

    def test_range_access_is_reported(self) -> None:
        ranges = [hit for hit in self.report.accesses if hit.kind == "range"]
        self.assertEqual([hit.address for hit in ranges], [0x08000000])

    def test_model_errors_are_not_reported_as_cpu_faults(self) -> None:
        self.assertEqual(self.report.faults, [])
        self.assertEqual(len(self.report.model_errors), 1)
        self.assertIn("Microsoft.Scripting", self.report.model_errors[0])

    def test_render_mentions_every_section(self) -> None:
        text = self.report.render()
        for marker in ("unmapped accesses", "models do not implement", "platform resets"):
            self.assertIn(marker, text)

    def test_ansi_escapes_are_stripped(self) -> None:
        coloured = "\x1b[91m(monitor) \x1b[0m[WARNING] rcc: Unhandled read from offset 0x58."
        self.assertEqual(len(trace.analyse(coloured).model_offsets), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
