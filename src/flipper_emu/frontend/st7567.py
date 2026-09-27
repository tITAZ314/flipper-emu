"""ST7567 LCD decoder: byte stream in, 128x64 framebuffer out.

The emulator-side model (`St7567Display.cs`) only records the SPI byte stream,
tagged with the A0 (command/data) line when it is wired, plus pin-change events.
All interpretation lives here so that it can be unit-tested against a recorded
stream without running an emulator - and so the UI can decode the same stream.

Framing follows the ST7567 command set used by u8g2's ST7565/ST7567 drivers, which
is what the firmware emits (measured: `E2 A2 A0 C8 40 25 81 20 2F A4 AF`, then
`0x10`/`0xB0-0xBF` addressing followed by 132 bytes of pixel data per page).

When the stream carries no A0 information (the board file has not wired the line
yet), command/data framing is *inferred*: parameter bytes are consumed after the
commands that take them, and a page-select command opens a 132-byte data run,
which matches how u8g2 writes full pages. Streams that do carry A0 tags are
decoded exactly, with no inference.

Record file format written by `St7567Display.cs` (tag byte + payload byte):

    0x01 data byte, A0 high (pixel data)
    0x02 data byte, A0 low  (command)
    0x03 A0 pin changed
    0x04 RESET pin changed
    0x05 data byte, A0 unknown (infer)
"""

from __future__ import annotations

import struct
import sys
import zlib
from typing import List, Optional

#: Visible panel geometry.
WIDTH = 128
HEIGHT = 64

#: The controller has 132 columns; u8g2 writes all of them per page.
COLUMNS = 132

#: Pages, each holding 8 vertical pixels.
PAGES = 8

#: Record file header written by the emulator-side model.
RECORD_MAGIC = b"FZDPI1\n"

TAG_DATA_A0_HIGH = 0x01
TAG_DATA_A0_LOW = 0x02
TAG_A0_CHANGED = 0x03
TAG_RESET_CHANGED = 0x04
TAG_DATA_A0_UNKNOWN = 0x05

#: Commands followed by exactly one parameter byte.
PARAMETER_COMMANDS = {
    0x81: "contrast",
    0xA8: "multiplex_ratio",
    0xF8: "booster_ratio",
    0xAC: "static_indicator",
    0xD7: "n_line_inversion",
}

class St7567Decoder:
    """Reconstructs the panel's 132x64 page memory from an SPI byte stream."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Software (0xE2) or power-on state: everything, including the panel."""
        self.memory = bytearray(PAGES * COLUMNS)
        self._reset_controller()
        self.commands = 0
        self.data_bytes = 0
        self.in_reset = False

    def _reset_controller(self) -> None:
        """Addressing/flag state only, leaving the panel contents alone."""
        self.page = 0
        self.column = 0
        self.start_line = 0
        self.display_on = False
        self.inverted = False
        self.all_points_on = False
        self.adc_reversed = False
        self.com_reversed = False
        self.contrast = 0
        self.pending_parameter: Optional[int] = None
        self.expected_data = 0

    def set_reset(self, asserted: bool) -> None:
        """Track the /RESET line (payload 1 = asserted, i.e. the line is low).

        The panel goes blank while it is held in reset, but the framebuffer is
        deliberately *kept*: firmware that pulses /RESET repaints every page right
        afterwards - the Flipper boot path does exactly that once per frame - so
        clearing on every pulse would show the host blank flashes between perfectly
        good frames. Everything the firmware sends afterwards overwrites what was
        there, so a real screen change still shows up.
        """
        self.in_reset = asserted
        if asserted:
            self._reset_controller()

    def feed_records(self, blob: bytes) -> int:
        """Feed a recorder file (with header); returns the number of events read.

        A blob without the header is treated as raw panel bytes with unknown A0,
        which makes it easy to decode a stream captured by other means.
        """
        if not blob.startswith(RECORD_MAGIC):
            for value in blob:
                self.feed_byte(value)
            return len(blob)
        return self.feed_record_body(blob[len(RECORD_MAGIC) :])

    def feed_record_body(self, body: bytes) -> int:
        """Feed tag/payload records without the header (live tailing).

        The caller is responsible for handing over whole records; a trailing
        partial record should be kept back and prepended to the next chunk.
        """
        if len(body) % 2:
            body = body[:-1]
        events = 0
        for index in range(0, len(body), 2):
            tag = body[index]
            payload = body[index + 1]
            events += 1
            if tag == TAG_DATA_A0_HIGH:
                self.feed_byte(payload, a0=1)
            elif tag == TAG_DATA_A0_LOW:
                self.feed_byte(payload, a0=0)
            elif tag == TAG_DATA_A0_UNKNOWN:
                self.feed_byte(payload)
            elif tag == TAG_RESET_CHANGED:
                self.set_reset(bool(payload))
        return events

    def feed_byte(self, value: int, a0: Optional[int] = None) -> None:
        """Feed one byte; ``a0`` is 1 for data, 0 for command, None to infer."""
        value &= 0xFF
        if a0 == 0:
            # A parameter byte (after 0x81 etc.) also arrives in command mode, so
            # parameter consumption takes precedence over the A0 classification.
            if self.pending_parameter is not None:
                self._apply_parameter(value)
            else:
                self._command(value)
            return
        if a0 == 1:
            self._data(value)
            return
        if self.pending_parameter is not None:
            self._apply_parameter(value)
            return
        if self.expected_data > 0:
            self._data(value)
            return
        if value in PARAMETER_COMMANDS or self._looks_like_command(value):
            self._command(value)
            return
        self._data(value)

    # -- command handling -------------------------------------------------
    def _looks_like_command(self, value: int) -> bool:
        """ST7567 command ranges (used only when A0 is unknown)."""
        if value <= 0x1F:  # column address, low/high nibble
            return True
        if 0x20 <= value <= 0x2F:  # V0 resistor ratio / power control
            return True
        if 0x40 <= value <= 0x7F:  # display start line
            return True
        if value in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xAE, 0xAF):
            return True
        if 0xB0 <= value <= 0xBF:  # page address
            return True
        if 0xC0 <= value <= 0xCF:  # COM scan direction
            return True
        if 0xE0 <= value <= 0xE3:  # read-modify-write
            return True
        return False

    def _command(self, value: int) -> None:
        self.commands += 1
        if value == 0xE2:  # software reset
            self.reset()
            return
        if value <= 0x0F:  # column address, low nibble
            self.column = (self.column & 0xF0) | value
            self.expected_data = 0
            return
        if 0x10 <= value <= 0x1F:  # column address, high nibble
            self.column = ((value & 0x0F) << 4) | (self.column & 0x0F)
            self.expected_data = 0
            return
        if 0x20 <= value <= 0x2F:  # V0 resistor ratio / power control
            return
        if 0x40 <= value <= 0x7F:  # display start line
            self.start_line = value & 0x3F
            return
        if value in (0xA0, 0xA1):  # ADC select (column direction)
            self.adc_reversed = value == 0xA1
            return
        if value in (0xA2, 0xA3):  # LCD bias
            return
        if value in (0xA4, 0xA5):  # all points off / on
            self.all_points_on = value == 0xA5
            return
        if value in (0xA6, 0xA7):  # normal / reverse display
            self.inverted = value == 0xA7
            return
        if value in (0xAE, 0xAF):  # display off / on
            self.display_on = value == 0xAF
            return
        if 0xB0 <= value <= 0xBF:  # page address: opens u8g2's 132-byte data run
            self.page = (value & 0x07) % PAGES
            self.expected_data = COLUMNS
            return
        if 0xC0 <= value <= 0xCF:  # COM scan direction
            self.com_reversed = value >= 0xC8
            return
        if 0xE0 <= value <= 0xE3:  # read-modify-write
            return
        if value in PARAMETER_COMMANDS:
            self.pending_parameter = value
            return
        # Unrecognised commands are ignored on purpose: the panel has registers
        # this decoder does not need to model (booster, temperature, ...).

    def _apply_parameter(self, value: int) -> None:
        command = self.pending_parameter
        self.pending_parameter = None
        if command == 0x81:
            self.contrast = value & 0x3F

    def _data(self, value: int) -> None:
        self.data_bytes += 1
        if self.expected_data > 0:
            self.expected_data -= 1
        if self.column < COLUMNS:
            self.memory[self.page * COLUMNS + self.column] = value
        self.column += 1
        if self.column >= COLUMNS:
            self.column = 0

    # -- output -----------------------------------------------------------
    def render(self) -> List[bytearray]:
        """Pixels as rows of 0/255, top row first, honouring the panel state."""
        rows = [bytearray(WIDTH) for _ in range(HEIGHT)]
        for page in range(PAGES):
            for column in range(COLUMNS):
                byte = self.memory[page * COLUMNS + column]
                x = column if not self.adc_reversed else (COLUMNS - 1 - column)
                if x >= WIDTH:
                    continue
                for bit in range(8):
                    y = page * 8 + bit
                    if self.com_reversed:
                        y = HEIGHT - 1 - y
                    if y < 0 or y >= HEIGHT:
                        continue
                    on = bool(byte & (1 << bit))
                    if self.all_points_on:
                        on = True
                    if self.inverted:
                        on = not on
                    if not self.display_on and not self.all_points_on:
                        on = False
                    rows[y][x] = 255 if on else 0
        if self.com_reversed:
            rows.reverse()
        return rows

    def to_ascii(self, on: str = "#", off: str = ".") -> str:
        """Render as text so a frame can be inspected in any terminal."""
        lines = []
        for index, row in enumerate(self.render()):
            lines.append("%02d %s" % (index, "".join(on if value else off for value in row)))
        return "\n".join(lines)

    def set_pixel(self, x: int, y: int, on: bool = True) -> None:
        """Set a pixel in page memory (used by tests and by synthetic frames)."""
        if not (0 <= x < COLUMNS and 0 <= y < HEIGHT):
            return
        page = y // 8
        bit = y % 8
        index = page * COLUMNS + x
        if on:
            self.memory[index] |= 1 << bit
        else:
            self.memory[index] &= ~(1 << bit) & 0xFF

    def png_bytes(self, scale: int = 1) -> bytes:
        """The frame encoded as PNG bytes (no file needed - the UI uses this)."""
        rows = self.render()
        raw = bytearray()
        for row in rows:
            scaled = bytearray()
            for value in row:
                scaled.extend([value] * scale)
            for _ in range(scale):
                raw.append(0)  # PNG filter type: none
                raw.extend(scaled)

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH * scale, HEIGHT * scale, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )

    def to_png(self, path: str, scale: int = 4) -> str:
        """Write the frame as an 8-bit grayscale PNG, integer-scaled for viewing."""
        with open(path, "wb") as handle:
            handle.write(self.png_bytes(scale))
        return path

    def describe(self) -> str:
        return (
            "display: page=%d column=%d start_line=%d on=%s invert=%s all_on=%s "
            "adc_rev=%s com_rev=%s contrast=%d | commands=%d data_bytes=%d"
            % (
                self.page, self.column, self.start_line, self.display_on, self.inverted,
                self.all_points_on, self.adc_reversed, self.com_reversed, self.contrast,
                self.commands, self.data_bytes,
            )
        )


def decode_records(blob: bytes) -> St7567Decoder:
    """Decode a whole recorder file (or raw stream)."""
    decoder = St7567Decoder()
    decoder.feed_records(blob)
    return decoder


def decode_file(path: str) -> St7567Decoder:
    with open(path, "rb") as handle:
        return decode_records(handle.read())


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: st7567 <stream.bin> [frame.png] [--ascii] [--scale N]")
        return 2
    decoder = decode_file(args[0])
    print(decoder.describe())
    if "--ascii" in args:
        print(decoder.to_ascii())
    if len(args) > 1 and not args[1].startswith("--"):
        scale = int(args[args.index("--scale") + 1]) if "--scale" in args else 4
        print("wrote %s" % decoder.to_png(args[1], scale=scale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



