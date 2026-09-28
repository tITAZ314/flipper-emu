"""Live Flipper Zero window: tails the display stream and injects button presses.

Everything happens on the host side, which is why no emulator-side display code is
needed beyond the recorder:

* `DisplayStreamTail` follows the recorder file written by `St7567Display` and
  feeds new records into `St7567Decoder` incrementally;
* whenever the framebuffer changes, the canvas image is replaced with a freshly
  encoded 1:1 PNG - Tk does the 6x scaling, so encoding stays cheap;
* key presses are translated by `keymap` and sent over the Renode monitor, which
  drives the firmware's real button GPIO lines (and therefore its EXTI edges).

`flipper_emu ui` starts Renode, waits for the stream, and opens this window.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from typing import Dict, Optional

from ..console.monitor import MonitorError, RenodeMonitor, check_reply
from . import keymap, st7567

DEFAULT_STREAM = os.path.join("artifacts", "display-stream.bin")
DEFAULT_SCALE = 6
POLL_MS = 40

#: Renode hooks the self-test arms *over the monitor*, each printing a marker into the
#: session log, so the answers come from inside the firmware.  Addresses from
#: firmware/flipper-z-f7-firmware-1.4.3.elf, Thumb bit cleared:
#:
#:   0x080827E8  input_isr        the input service's GPIO callback: the press reached
#:                                the firmware, not just the emulated pin
#:   0x08082500  view_port_input  the gui handing the event to the active view port
#:
#: Without them a selftest can only see the panel move, which the idle animation does
#: on its own - which is how "the keys do nothing" could go unnoticed.
FIRMWARE_MARKERS = (
    ("isr", 0x080827E8),
    ("view", 0x08082500),
)
MARKER_PREFIX = "BTNMARK"

#: Decoding budget per poll, and when to jump forward instead of catching up.
#: The firmware can outrun a host-side decoder: it writes a full page whenever the
#: GUI changes.  (`DwtWb55` paces `furi_delay_us()` waits in real emulated time;
#: before that handshake the delays were collapsed into a single read, which gave
#: far more redraws per second - see `docs/BRINGUP_LOG.md` §22.)
MAX_CHUNK = 256 * 1024
SKIP_THRESHOLD = 512 * 1024
KEEP_BYTES = 64 * 1024

#: Records begin after the header, which is an odd number of bytes - so a resync
#: has to be aligned to the record grid, not to file offset zero.
RECORD_START = len(st7567.RECORD_MAGIC)

#: Background colour of the emulated panel (ST7567 pixels are on/off; the panel's
#: backlight makes "off" look like a lit amber pixel, but plain dark reads best).
PANEL_BACKGROUND = "#101010"
PIXEL_ON = "#e8e8e8"


class DisplayStreamTail:
    """Incrementally decodes a display recorder file while it grows."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.decoder = st7567.St7567Decoder()
        self.offset = 0
        self.header_seen = False
        self.leftover = b""
        self.events = 0
        self.frames = 0
        self.resyncs = 0
        self.error: Optional[str] = None

    def poll(self) -> bool:
        """Consume whatever is new; ``True`` when the framebuffer changed.

        The firmware can still redraw faster than a host-side decoder consumes the
        stream (a full page write per GUI change), so the tail can fall behind.
        When it does, the tail skips forward to the newest records instead of
        falling further behind: page/column commands arrive constantly, so the
        picture heals within a frame or two, and the UI stays live.
        """
        if not os.path.exists(self.path):
            return False
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:
                # A new run recreated the file: start over.
                self.decoder = st7567.St7567Decoder()
                self.offset = 0
                self.header_seen = False
                self.leftover = b""
            backlog = size - self.offset
            if backlog > SKIP_THRESHOLD:
                self.resyncs += 1
                # Jump to the newest whole records: page/column commands arrive
                # constantly, so the panel heals within a frame or two.
                keep = size - KEEP_BYTES
                self.offset = RECORD_START + ((keep - RECORD_START) // 2) * 2
                if self.offset < RECORD_START:
                    self.offset = RECORD_START
                self.leftover = b""  # it belongs to the bytes we are skipping
            if size == self.offset:
                return False
            before = bytes(self.decoder.memory)
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                blob = handle.read(MAX_CHUNK)
            self.offset += len(blob)
        except OSError as exc:
            self.error = str(exc)
            return False

        if not self.header_seen:
            if blob.startswith(st7567.RECORD_MAGIC):
                blob = blob[len(st7567.RECORD_MAGIC) :]
            self.header_seen = True

        blob = self.leftover + blob
        self.leftover = b""
        if len(blob) % 2:
            # Keep the trailing half of a record for the next poll.
            self.leftover = blob[-1:]
            blob = blob[:-1]

        self.events += self.decoder.feed_record_body(blob)
        if bytes(self.decoder.memory) != before:
            self.frames += 1
            return True
        return False

class FlipperWindow:
    """Tk window showing the emulated panel and forwarding key presses."""

    def __init__(self, tail: DisplayStreamTail, injector, scale: int = DEFAULT_SCALE) -> None:
        self.tail = tail
        self.injector = injector
        self.scale = max(1, scale)

        self.root = tk.Tk()
        self.root.title("Flipper Zero (flipper-emu)")
        self.root.configure(bg=PANEL_BACKGROUND)
        self.canvas = tk.Canvas(
            self.root,
            width=st7567.WIDTH * self.scale,
            height=st7567.HEIGHT * self.scale,
            bg=PANEL_BACKGROUND,
            highlightthickness=0,
        )
        self.canvas.pack()
        self.photo = None
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.status = tk.StringVar(value="waiting for the firmware to draw...")
        tk.Label(
            self.root, textvariable=self.status, anchor="w", bg=PANEL_BACKGROUND, fg="#909090"
        ).pack(fill="x")

        self.root.bind("<KeyPress>", self.on_key_press)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.focus_force()
        self._render()
        self.root.after(POLL_MS, self._tick)

    # -- rendering --------------------------------------------------------
    def _render(self) -> None:
        """Encode the framebuffer at 1:1 and let Tk do the scaling."""
        try:
            base = tk.PhotoImage(data=self.tail.decoder.png_bytes(1))
        except tk.TclError as exc:  # pragma: no cover - depends on the Tk build
            raise RuntimeError(
                "this Tk build cannot load PNG data (%s); Tk 8.6 or newer is required" % exc
            )
        self.photo = base.zoom(self.scale) if self.scale > 1 else base
        self.canvas.itemconfigure(self.image_item, image=self.photo)

    def _tick(self) -> None:
        try:
            if self.tail.poll():
                self._render()
        except Exception as exc:  # keep the window alive on a bad stream
            self.status.set("stream error: %s" % exc)
        else:
            self.status.set(
                "%s | frames=%d events=%d%s"
                % (
                    self.tail.decoder.describe(),
                    self.tail.frames,
                    self.tail.events,
                    "" if self.injector is not None else " | buttons disabled",
                )
            )
        self.root.after(POLL_MS, self._tick)

    # -- input ------------------------------------------------------------
    def on_key_press(self, event):
        button = keymap.button_for_key(event.keysym)
        if button is None:
            return None
        if self.injector is not None:
            try:
                self.injector.press(button.name)
            except MonitorError as exc:
                self.status.set("monitor error: %s" % exc)
        return "break"

    def on_key_release(self, event):
        button = keymap.button_for_key(event.keysym)
        if button is None:
            return None
        if self.injector is not None:
            try:
                self.injector.release(button.name)
            except MonitorError as exc:
                self.status.set("monitor error: %s" % exc)
        return "break"

    def close(self) -> None:
        if self.injector is not None:
            self.injector.release_all()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()

def arm_markers(monitor) -> int:
    """Install the marker hooks over the monitor; returns how many were accepted."""
    armed = 0
    for label, address in FIRMWARE_MARKERS:
        command = 'cpu AddHook 0x%08X "print(\'%s %s\')"' % (address, MARKER_PREFIX, label)
        try:
            reply = monitor.command(command, wait=0.2)
        except MonitorError as exc:
            print("selftest: cannot arm the %s marker: %s" % (label, exc))
            continue
        problem = check_reply(reply)
        if problem:
            print("selftest: the %s marker was refused: %s" % (label, problem))
        else:
            armed += 1
    return armed


def log_size(path: Optional[str]) -> int:
    """Size of the session log, so markers can be counted after the tap only."""
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_markers(path: Optional[str], offset: int = 0) -> Dict[str, int]:
    """Count each marker printed since ``offset`` in the session log.

    A missing or unreadable log reports zero for every marker rather than raising: the
    caller's question is always "did this stage happen?", and a log that is not there
    has not shown any stage.
    """
    text = ""
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                text = handle.read()
        except OSError:
            text = ""
    return {label: text.count("%s %s" % (MARKER_PREFIX, label)) for label, _ in FIRMWARE_MARKERS}


def selftest(args) -> int:
    """Headless check: tail the stream, tap OK, report what changed.

    The panel changing is not proof that a button arrived - the idle animation redraws
    by itself - so the check that matters is made *inside the firmware*: two hooks are
    armed over the monitor (the input service's ISR, and the gui's view-port delivery),
    each printing a marker into the session log, and the markers are read back after the
    tap.  That turns "the keys do nothing" into the stage that is missing.
    """
    monitor = RenodeMonitor(args.host, args.monitor_port)
    if not monitor.connect(attempts=20, delay=0.5):
        print("FAIL: monitor unreachable on %s:%d" % (args.host, args.monitor_port))
        return 1
    injector = keymap.ButtonInjector(monitor)
    tail = DisplayStreamTail(args.stream)

    if args.firmware_log:
        armed = arm_markers(monitor)
        print("selftest: armed %d firmware marker(s) in %s" % (armed, args.firmware_log))
    log_offset = log_size(args.firmware_log)

    deadline = time.time() + args.selftest
    tap_at = time.time() + max(2.0, args.selftest / 3.0)
    tapped = False
    frames_at_tap = 0
    changed_after_tap = False

    print("selftest: tailing %s for %gs" % (args.stream, args.selftest))
    while time.time() < deadline:
        tail.poll()
        if not tapped and time.time() >= tap_at:
            try:
                injector.tap("OK")
            except MonitorError as exc:
                print("FAIL: %s" % exc)
                return 1
            frames_at_tap = tail.frames
            tapped = True
            print("selftest: injected OK at %d frames, %d events" % (tail.frames, tail.events))
        if tapped and tail.frames > frames_at_tap:
            changed_after_tap = True
        time.sleep(0.01)

    print(
        "selftest: events=%d frames=%d resyncs=%d"
        % (tail.events, tail.frames, tail.resyncs)
    )
    if tail.events == 0:
        print("FAIL: nothing decoded - is the emulator running and drawing?")
        return 1

    markers = read_markers(args.firmware_log, log_offset)
    if args.firmware_log:
        print("selftest: firmware markers after the tap: %s" % (markers or "none"))
        if markers.get("isr", 0) == 0:
            print(
                "FAIL: the press never reached the firmware's input service.  The "
                "monitor command was sent, so check that this session owns the "
                "monitor port (an orphaned Renode would have taken it)."
            )
            return 1
        if markers.get("view", 0) == 0:
            print(
                "FAIL: the input service saw the press, but the gui never delivered it "
                "to a view port - that is the emulator-side bug to chase."
            )
            return 1
    if not changed_after_tap:
        print("FAIL: the panel did not change after the injected button press")
        return 1
    print("OK: display decoded live and buttons reach the firmware")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live Flipper Zero window over the display stream")
    parser.add_argument("--stream", default=DEFAULT_STREAM, help="display recorder file to tail")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--monitor-port", type=int, default=3456)
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE, help="6 gives a 768x384 window")
    parser.add_argument("--no-buttons", action="store_true", help="render only, ignore the monitor")
    parser.add_argument(
        "--firmware-log",
        default=None,
        help="Renode session log to read the firmware markers from (the self-test arms "
        "hooks over the monitor and counts them there, which is what proves a button "
        "press reached the firmware rather than just the emulated pin)",
    )
    parser.add_argument(
        "--selftest",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="headless: run for SECONDS, inject a button, report the result",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(args)

    monitor = None
    injector = None
    if not args.no_buttons:
        monitor = RenodeMonitor(args.host, args.monitor_port)
        if monitor.connect(attempts=8, delay=0.5):
            injector = keymap.ButtonInjector(monitor)
        else:
            print("warning: Renode monitor not reachable; buttons disabled", file=sys.stderr)

    tail = DisplayStreamTail(args.stream)
    window = FlipperWindow(tail, injector, scale=args.scale)
    try:
        window.run()
    finally:
        if injector is not None:
            injector.release_all()
        if monitor is not None:
            monitor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


