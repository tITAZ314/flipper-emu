"""Tests for the button keymap and the Renode monitor client.

The pin table is checked against the firmware's own resource listing
(`targets/f7/furi_hal/furi_hal_resources.c`, tag 1.4.3), so a mismatch shows up
here rather than as a dead button in the UI.
"""

from __future__ import annotations

import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.console.monitor import RenodeMonitor  # noqa: E402
from flipper_emu.frontend import keymap  # noqa: E402


class ButtonTableTests(unittest.TestCase):
    def test_pins_match_the_firmware(self) -> None:
        expected = {
            "UP": ("gpioPortB", 10, True),
            "DOWN": ("gpioPortC", 6, True),
            "RIGHT": ("gpioPortB", 12, True),
            "LEFT": ("gpioPortB", 11, True),
            "OK": ("gpioPortH", 3, False),
            "BACK": ("gpioPortC", 13, True),
        }
        for name, (port, pin, active_low) in expected.items():
            button = keymap.BUTTONS[name]
            self.assertEqual((button.port, button.pin, button.active_low), (port, pin, active_low), name)

    def test_polarity_levels(self) -> None:
        self.assertFalse(keymap.BUTTONS["UP"].level(True), "active-low button reads 0 when pressed")
        self.assertTrue(keymap.BUTTONS["UP"].level(False))
        self.assertTrue(keymap.BUTTONS["OK"].level(True), "OK is active-high")
        self.assertFalse(keymap.BUTTONS["OK"].level(False))

    def test_monitor_commands(self) -> None:
        self.assertEqual(keymap.BUTTONS["UP"].command(True), "gpioPortB OnGPIO 10 false")
        self.assertEqual(keymap.BUTTONS["UP"].command(False), "gpioPortB OnGPIO 10 true")
        self.assertEqual(keymap.BUTTONS["OK"].command(True), "gpioPortH OnGPIO 3 true")
        self.assertEqual(keymap.BUTTONS["BACK"].command(True), "gpioPortC OnGPIO 13 false")

    def test_keyboard_mapping(self) -> None:
        self.assertEqual(keymap.button_for_key("Up").name, "Up")
        self.assertEqual(keymap.button_for_key("Left").name, "Left")
        self.assertEqual(keymap.button_for_key("Return").name, "OK")
        self.assertEqual(keymap.button_for_key("space").name, "OK")
        self.assertEqual(keymap.button_for_key("Escape").name, "Back")
        self.assertIsNone(keymap.button_for_key("q"))


class InjectorTests(unittest.TestCase):
    class FakeMonitor:
        def __init__(self) -> None:
            self.sent = []

        def send(self, text: str) -> None:
            self.sent.append(text)

    def test_press_and_release(self) -> None:
        injector = keymap.ButtonInjector(self.FakeMonitor())
        self.assertTrue(injector.press("OK"))
        self.assertIn("OK", injector.held)
        self.assertTrue(injector.release("OK"))
        self.assertEqual(
            injector.monitor.sent,
            ["gpioPortH OnGPIO 3 true", "gpioPortH OnGPIO 3 false"],
        )
        self.assertEqual(injector.held, set())

    def test_tap_and_release_all(self) -> None:
        injector = keymap.ButtonInjector(self.FakeMonitor())
        self.assertTrue(injector.tap("UP", hold_seconds=0.01))
        injector.press("BACK")
        injector.press("LEFT")
        injector.release_all()
        self.assertEqual(injector.held, set())
        self.assertEqual(len(injector.monitor.sent), 6)

    def test_unknown_button_is_ignored(self) -> None:
        injector = keymap.ButtonInjector(self.FakeMonitor())
        self.assertFalse(injector.press("NOPE"))
        self.assertEqual(injector.monitor.sent, [])


class MonitorClientTests(unittest.TestCase):
    """The client is exercised against a real (fake) monitor socket."""

    def test_command_is_line_terminated_and_reply_is_cleaned(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []

        def serve() -> None:
            connection, _ = server.accept()
            connection.sendall(b"\x1b[91m(monitor)\x1b[0m ")
            received.append(connection.recv(256))
            connection.sendall(b"ok\r\n")
            connection.close()
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        monitor = RenodeMonitor("127.0.0.1", port, timeout=2.0)
        self.assertTrue(monitor.connect(attempts=1))
        reply = monitor.command("gpioPortH OnGPIO 3 true", wait=0.3)
        monitor.close()
        thread.join(timeout=2)

        self.assertEqual(received[0], b"gpioPortH OnGPIO 3 true\n")
        self.assertIn("ok", reply)
        self.assertNotIn("\x1b", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
