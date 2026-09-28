"""Tests for the `run`/`ui` start-up options - no emulator required.

`--card` exists because the emulated machine has no card unless it is told to: the
generated platform script ends with `gpioPortC OnGPIO 10 true`, i.e. SD card detect
high, and the firmware gates storage init (and therefore `/int/.slideshow`, and
therefore the desktop's first-start slideshow scene) on that pin.  `flipper_emu ui`
used to pass no start-up commands at all, so a UI session could never show anything
but the idle animation.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu import cli  # noqa: E402

CARD_SCRIPT = ROOT / "src" / "flipper_emu" / "platform" / "card_present.resc"


class StartupIncludeTests(unittest.TestCase):
    def test_card_maps_to_the_platform_script(self) -> None:
        commands = cli.startup_includes(None, card=True)
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("include @"))
        # Renode prefers forward slashes, and the file has to exist for the include.
        self.assertNotIn("\\", commands[0])
        self.assertIn("src/flipper_emu/platform/card_present.resc", commands[0])
        self.assertTrue(CARD_SCRIPT.exists())

    def test_card_comes_before_probe_includes(self) -> None:
        commands = cli.startup_includes(["_renode/hooks_intersect.resc"], card=True)
        self.assertEqual(len(commands), 2)
        self.assertIn("card_present.resc", commands[0])
        self.assertIn("hooks_intersect.resc", commands[1])

    def test_no_options_is_no_commands(self) -> None:
        # The plain `run`/`ui` case: the platform script stays the only include.
        self.assertEqual(cli.startup_includes(None), [])
        self.assertEqual(cli.startup_includes([]), [])


class ParserTests(unittest.TestCase):
    def test_ui_takes_card_and_includes(self) -> None:
        args = cli.build_parser().parse_args(
            ["ui", "--card", "--renode-include", "probe.resc", "--renode-include", "b.resc"]
        )
        self.assertTrue(args.card)
        self.assertEqual(args.renode_include, ["probe.resc", "b.resc"])
        self.assertIs(args.func, cli.cmd_ui)

    def test_ui_defaults_to_no_card(self) -> None:
        args = cli.build_parser().parse_args(["ui"])
        self.assertFalse(args.card)
        self.assertIsNone(args.renode_include)

    def test_run_takes_card_too(self) -> None:
        args = cli.build_parser().parse_args(["run", "--card", "--seconds", "5"])
        self.assertTrue(args.card)
        self.assertIs(args.func, cli.cmd_run)

    def test_ui_forwards_the_session_options(self) -> None:
        args = cli.build_parser().parse_args(
            ["ui", "--monitor-port", "3555", "--scale", "4", "--attach", "--no-buttons"]
        )
        self.assertEqual(args.monitor_port, 3555)
        self.assertEqual(args.scale, 4)
        self.assertTrue(args.attach)
        self.assertTrue(args.no_buttons)


if __name__ == "__main__":
    unittest.main()
