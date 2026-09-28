"""Tests for the button-injection plumbing - no emulator and no window required.

Everything here exists because of one failure mode: `flipper_emu ui` could send
`gpioPortB OnGPIO 12 false` at a Renode that was not listening (or not the one showing
frames), and nothing said so - the keys simply "did nothing".  So: refused commands are
surfaced, an occupied monitor port is avoided, and the self-test's firmware markers are
counted rather than assumed.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu import cli, runner  # noqa: E402
from flipper_emu.console.monitor import MonitorError, check_reply  # noqa: E402
from flipper_emu.frontend import keymap, ui_tk  # noqa: E402


class FakeMonitor:
    """Stands in for the TCP client: records commands, returns a canned reply."""

    def __init__(self, reply: str = "") -> None:
        self.sent = []
        self.reply = reply

    def send(self, text: str) -> str:
        self.sent.append(text)
        return self.reply

    def command(self, text: str, wait: float = 0.15) -> str:
        self.sent.append(text)
        return self.reply


class ReplyTests(unittest.TestCase):
    def test_a_normal_echo_is_not_a_failure(self) -> None:
        self.assertEqual(check_reply("gpioPortB OnGPIO 12 false (flipper)"), "")
        self.assertEqual(check_reply(""), "")
        self.assertEqual(check_reply(None), "")

    def test_a_refused_command_is_reported(self) -> None:
        text = "Could not find peripheral 'gpioPortB' in the machine"
        self.assertIn("Could not find peripheral", check_reply(text))

    def test_button_injection_raises_when_refused(self) -> None:
        injector = keymap.ButtonInjector(FakeMonitor("Could not find peripheral 'gpioPortH'"))
        with self.assertRaises(MonitorError):
            injector.press("OK")
        self.assertEqual(injector.held, set())

    def test_button_injection_still_works_when_accepted(self) -> None:
        fake = FakeMonitor("gpioPortH OnGPIO 3 true (flipper)")
        injector = keymap.ButtonInjector(fake)
        self.assertTrue(injector.press("OK"))
        self.assertTrue(injector.release("OK"))
        self.assertEqual(fake.sent, ["gpioPortH OnGPIO 3 true", "gpioPortH OnGPIO 3 false"])


class MonitorPortTests(unittest.TestCase):
    def test_a_free_port_is_kept(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertEqual(cli.free_monitor_port(free), free)

    def test_an_occupied_port_is_avoided(self) -> None:
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            chosen = cli.free_monitor_port(taken)
            self.assertNotEqual(chosen, taken)
            # ... and the choice is usable, i.e. Renode could bind it.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", chosen))


class MarkerTests(unittest.TestCase):
    def test_markers_are_counted_from_an_offset(self) -> None:
        directory = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(directory.name, "renode.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("boot noise\nBTNMARK isr\n")
            start = os.path.getsize(path)
            self.assertEqual(ui_tk.read_markers(path, start), {"isr": 0, "view": 0})
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("BTNMARK isr BTNMARK view\n")
            counts = ui_tk.read_markers(path, start)
            self.assertEqual(counts["isr"], 1)
            self.assertEqual(counts["view"], 1)
        finally:
            directory.cleanup()

    def test_a_missing_log_is_not_an_error(self) -> None:
        self.assertEqual(ui_tk.read_markers(None), {"isr": 0, "view": 0})
        self.assertEqual(ui_tk.read_markers("does/not/exist.log"), {"isr": 0, "view": 0})
        self.assertEqual(ui_tk.log_size(None), 0)

    def test_arming_hooks_the_firmware_entry_points(self) -> None:
        fake = FakeMonitor("(flipper)")
        armed = ui_tk.arm_markers(fake)
        self.assertEqual(armed, len(ui_tk.FIRMWARE_MARKERS))
        joined = " ".join(fake.sent)
        self.assertIn("cpu AddHook 0x080827E8", joined)  # input_isr
        self.assertIn("cpu AddHook 0x08082500", joined)  # view_port_input
        self.assertIn("BTNMARK", joined)


class BootScriptTests(unittest.TestCase):
    """One generated script, because several `-e` arguments do not survive."""

    def test_every_startup_command_lands_in_one_script(self) -> None:
        directory = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(directory.name, runner.BOOT_SCRIPT)
            runner.write_boot_script(
                path,
                [
                    "include @C:/x/platform/flipper_zero.resc",
                    "include @C:/x/src/flipper_emu/platform/card_present.resc",
                    "print('x')",
                ],
            )
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("include @C:/x/platform/flipper_zero.resc", text)
            self.assertIn("include @C:/x/src/flipper_emu/platform/card_present.resc", text)
            self.assertIn("print('x')", text)
            # The platform include comes first: its `start` is what resets board state.
            self.assertLess(
                text.index("flipper_zero.resc"),
                text.index("card_present.resc"),
            )
        finally:
            directory.cleanup()

    def test_a_single_e_command_is_what_the_runner_passes(self) -> None:
        argv = runner.build_command(
            "renode.exe", 3456, ["include @C:/x/session_boot.resc"], None, console=False
        )
        self.assertEqual(argv.count("-e"), 1)
        self.assertIn("include @C:/x/session_boot.resc", argv)


if __name__ == "__main__":
    unittest.main()

