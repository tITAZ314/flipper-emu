"""Keyboard mapping and button injection for the emulated Flipper front panel.

The pins come from the firmware's own resource table
(`targets/f7/furi_hal/furi_hal_resources.c`, tag 1.4.3), including the polarity:
every button is active-low except OK, which is active-high. A press is therefore a
pin *level*, and the firmware reads that level (plus the EXTI edge our Wb55Exti
model latches) exactly as it would on hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Set

from ..console.monitor import MonitorError, check_reply

#: Tk keysym -> button name.
KEYMAP: Dict[str, str] = {
    "Up": "UP",
    "Down": "DOWN",
    "Left": "LEFT",
    "Right": "RIGHT",
    "Return": "OK",
    "KP_Enter": "OK",
    "space": "OK",
    "Escape": "BACK",
    "BackSpace": "BACK",
}


@dataclass(frozen=True)
class Button:
    """One front-panel button: its GPIO line and its active level."""

    name: str
    port: str
    pin: int
    active_low: bool

    def level(self, pressed: bool) -> bool:
        """Pin level for a press or release, honouring the polarity."""
        return (not pressed) if self.active_low else pressed

    def command(self, pressed: bool) -> str:
        """The monitor command that drives the pin."""
        return "%s OnGPIO %d %s" % (
            self.port,
            self.pin,
            "true" if self.level(pressed) else "false",
        )


BUTTONS: Dict[str, Button] = {
    "UP": Button("Up", "gpioPortB", 10, active_low=True),
    "DOWN": Button("Down", "gpioPortC", 6, active_low=True),
    "RIGHT": Button("Right", "gpioPortB", 12, active_low=True),
    "LEFT": Button("Left", "gpioPortB", 11, active_low=True),
    "OK": Button("OK", "gpioPortH", 3, active_low=False),
    "BACK": Button("Back", "gpioPortC", 13, active_low=True),
}


def button_for_key(keysym: str) -> Optional[Button]:
    """Translate a Tk keysym into a button, or ``None`` if it is not mapped."""
    name = KEYMAP.get(keysym)
    return BUTTONS[name] if name else None


class ButtonInjector:
    """Drives button GPIO levels through a Renode monitor connection."""

    def __init__(self, monitor) -> None:
        self.monitor = monitor
        self.held: Set[str] = set()

    def press(self, name: str) -> bool:
        button = BUTTONS.get(name)
        if button is None:
            return False
        problem = check_reply(self.monitor.send(button.command(True)))
        if problem:
            raise MonitorError("press %s was refused: %s" % (name, problem))
        self.held.add(name)
        return True

    def release(self, name: str) -> bool:
        button = BUTTONS.get(name)
        if button is None:
            return False
        problem = check_reply(self.monitor.send(button.command(False)))
        if problem:
            raise MonitorError("release %s was refused: %s" % (name, problem))
        self.held.discard(name)
        return True

    def tap(self, name: str, hold_seconds: float = 0.05) -> bool:
        """Press and release, for scripted/self-test use."""
        import time

        if not self.press(name):
            return False
        time.sleep(hold_seconds)
        return self.release(name)

    def release_all(self) -> None:
        for name in sorted(self.held):
            self.release(name)
