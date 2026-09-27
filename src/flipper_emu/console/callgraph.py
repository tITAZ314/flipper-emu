"""Call graph for a Cortex-M image, straight from the bytes - no disassembler.

Bring-up tool: this is how the bootloader's decision logic was read in this project
(docs/BRINGUP_LOG.md section 14).  Cortex-M code is Thumb, so control transfers are
32-bit halfword pairs: ``BL`` has bits 15..14 = 11 on the second halfword and
``B.W`` (a tail branch) has 10.  Rebuilding the target from S:J1:J2:imm10:imm11 and
resolving it against the ELF's symbols gives a call graph that answers questions
like "which function draws this screen?" and "what does this dispatcher call?".

Addresses in an ELF symbol table carry the Thumb bit, so a function whose symbol
reads ``0x0800C03D`` starts at ``0x0800C03C`` - pass the even address as a target.

Usage::

    py -3.9 -m flipper_emu.console.callgraph <fw.elf> <target-hex> [...]
    py -3.9 -m flipper_emu.console.callgraph <fw.elf> --from 0x08011954 0x08011A18
    py -3.9 -m flipper_emu.console.callgraph <fw.elf> --dump 0x0800C03C 16
    py -3.9 -m flipper_emu.console.callgraph <fw.elf> --lit 0x0800C03C
"""

from __future__ import annotations

import struct
import sys
from typing import Dict, Iterator, List, Optional, Tuple

from .symbols import ElfSymbolTable

CALL = "call"
TAIL = "tail"


class ImageError(Exception):
    """The file is not a 32-bit little-endian ELF (a Cortex-M image here)."""


def _parse_sections(blob: bytes) -> List[dict]:
    section_offset = struct.unpack_from("<I", blob, 0x20)[0]
    section_size = struct.unpack_from("<H", blob, 0x2E)[0]
    section_count = struct.unpack_from("<H", blob, 0x30)[0]
    name_index = struct.unpack_from("<H", blob, 0x32)[0]
    sections = []
    for index in range(section_count):
        fields = struct.unpack_from(
            "<IIIIIIIIII", blob, section_offset + index * section_size
        )
        sections.append(
            {"name_off": fields[0], "addr": fields[3], "offset": fields[4], "size": fields[5]}
        )
    strings = sections[name_index]
    table = blob[strings["offset"] : strings["offset"] + strings["size"]]
    for section in sections:
        end = table.find(b"\x00", section["name_off"])
        section["name"] = table[section["name_off"] : end].decode("ascii", "replace")
    return sections


def decode_branches(code: bytes, base: int) -> Iterator[Tuple[int, int, str]]:
    """``(site, target, kind)`` for every BL/B.W in ``code`` (already at ``base``)."""
    for offset in range(0, len(code) - 4, 2):
        first, second = struct.unpack_from("<HH", code, offset)
        if (first & 0xF800) != 0xF000:
            continue
        top = second & 0xC000
        if top not in (0x8000, 0xC000):
            continue
        kind = TAIL if top == 0x8000 else CALL
        delta = ((first & 0x7FF) << 12) | ((second & 0x7FF) << 1)
        if (first >> 10) & 1:
            delta -= 1 << 23
        yield (base + offset, base + offset + 4 + delta, kind)


class ThumbImage:
    """Just enough ELF to decode branches out of ``.text``."""

    def __init__(self, blob: bytes, path: str = "") -> None:
        if len(blob) < 0x34 or blob[:4] != b"\x7fELF" or blob[4] != 1 or blob[5] != 1:
            raise ImageError("expected a 32-bit little-endian ELF")
        self.blob = blob
        self.path = path
        self.sections = _parse_sections(blob)
        text = self.section(".text")
        if text is None:
            raise ImageError("no .text section")
        self.base = text["addr"]
        self.code = blob[text["offset"] : text["offset"] + text["size"]]
        self.symbols = ElfSymbolTable.read(path) if path else None

    def section(self, name: str) -> Optional[dict]:
        for section in self.sections:
            if section["name"] == name:
                return section
        return None

    def callers(self) -> Dict[int, List[Tuple[int, str]]]:
        index: Dict[int, List[Tuple[int, str]]] = {}
        for site, target, kind in decode_branches(self.code, self.base):
            index.setdefault(target, []).append((site, kind))
        return index

    def describe(self, address: int) -> str:
        if self.symbols is None:
            return "0x%08X" % address
        return self.symbols.describe(address)

    def bytes_at(self, address: int, length: int) -> bytes:
        start = address - self.base
        return self.code[start : start + length]

    def literal_at(self, address: int) -> Tuple[int, int]:
        """Decode ``LDR Rt, [pc, #imm]`` at ``address``; returns (literal, value)."""
        first = struct.unpack_from("<H", self.code, address - self.base)[0]
        if (first & 0xF800) != 0x4800:
            raise ValueError("not an LDR (literal) at 0x%08X" % address)
        literal = ((address + 4) & ~3) + (first & 0xFF) * 4
        return literal, struct.unpack_from("<I", self.code, literal - self.base)[0]


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: callgraph <firmware.elf> <target-hex>... | --from A B | --dump A N | --lit A")
        return 2
    with open(args[0], "rb") as handle:
        image = ThumbImage(handle.read(), args[0])
    rest = args[1:]

    if rest[0] == "--dump":
        address, length = int(rest[1], 16), int(rest[2], 16)
        for index in range(0, length, 16):
            row = image.bytes_at(address + index, 16)
            print("  0x%08X  %s" % (address + index, " ".join("%02X" % byte for byte in row)))
        return 0
    if rest[0] == "--lit":
        literal, value = image.literal_at(int(rest[1], 16))
        print("literal at 0x%08X = 0x%08X" % (literal, value))
        return 0

    index = image.callers()
    if rest[0] == "--from":
        first, last = int(rest[1], 16), int(rest[2], 16)
        sites = [
            (site, kind, target)
            for target, entries in index.items()
            for site, kind in entries
            if first <= site <= last
        ]
        for site, kind, target in sorted(sites):
            print("  0x%08X  %-4s -> %s" % (site, kind, image.describe(target)))
        return 0

    for token in rest:
        target = int(token, 16)
        print("\n=== branches to 0x%08X (%s) ===" % (target, image.describe(target)))
        found = index.get(target)
        if not found:
            print("   none (table/indirect call, or not a branch target)")
            continue
        for site, kind in sorted(found):
            print("   %-4s from 0x%08X -> %s" % (kind, site, image.describe(site)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
