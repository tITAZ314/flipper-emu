"""How many distinct frames does a recorded panel stream actually contain?

A capture that "keeps growing" is not the same as an animation: it can be one frame
redrawn, or a screen that only updates when something happens. This tool replays a
recorder file through the incremental decoder and snapshots the framebuffer at fixed
record intervals, so the answer is a count of distinct framebuffer states and a
timeline of when they changed.

Usage:

    py -3.9 -m flipper_emu.frontend.frame_stats artifacts/display-stream.bin
    py -3.9 -m flipper_emu.frontend.frame_stats <stream> --records 64 --png best.png
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from typing import List, Optional, Tuple

from . import st7567


def _framebuffer_bytes(decoder: st7567.St7567Decoder) -> bytes:
    return b"".join(bytes(row) for row in decoder.render())


def _lit_pixels(frame: bytes) -> int:
    """Lit pixels: ``render()`` yields one byte per pixel, 0 or 255."""
    return sum(1 for byte in frame if byte)


def analyse(
    path: str, records_per_snapshot: int = 64
) -> Tuple[List[Tuple[int, str, int]], List[int]]:
    """Replay ``path``, returning the change timeline and the lit-pixel series."""
    with open(path, "rb") as handle:
        blob = handle.read()

    header = len(st7567.RECORD_MAGIC)
    body = blob[header:] if blob.startswith(st7567.RECORD_MAGIC) else blob
    decoder = st7567.St7567Decoder()

    changes: List[Tuple[int, str, int]] = []
    lit_series: List[int] = []
    previous: Optional[str] = None
    step = max(1, records_per_snapshot) * 2  # one record is a tag plus a payload
    for offset in range(0, len(body) - step + 1, step):
        decoder.feed_record_body(body[offset : offset + step])
        frame = _framebuffer_bytes(decoder)
        digest = hashlib.sha1(frame).hexdigest()[:12]
        lit = _lit_pixels(frame)
        lit_series.append(lit)
        if digest != previous:
            changes.append((offset, digest, lit))
            previous = digest
    return changes, lit_series


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream", help="recorder file written by St7567Display")
    parser.add_argument("--records", type=int, default=64, help="records per snapshot")
    parser.add_argument("--png", default=None, help="write the fullest frame here")
    parser.add_argument("--scale", type=int, default=4)
    args = parser.parse_args(argv)

    changes, lit_series = analyse(args.stream, args.records)
    if not lit_series:
        print("no records decoded from %s" % args.stream)
        return 1

    import os

    size = os.path.getsize(args.stream)
    print("stream: %s (%d bytes, %d snapshots of %d records)"
          % (args.stream, size, len(lit_series), args.records))
    print("distinct framebuffer states: %d" % len(changes))
    print("lit pixels: min=%d max=%d final=%d" % (min(lit_series), max(lit_series), lit_series[-1]))
    print("first changes:")
    for offset, digest, lit in changes[:8]:
        print("  byte %8d  %s  lit=%d" % (offset, digest, lit))
    if len(changes) > 8:
        print("  ... %d more" % (len(changes) - 8))

    if args.png:
        decoder = st7567.decode_records(open(args.stream, "rb").read())
        decoder.to_png(args.png, scale=args.scale)
        print("wrote %s" % args.png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
