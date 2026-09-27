"""Symbol resolution for the firmware under test (ELF symbol table reader).

The debug console needs to turn the addresses the emulator reports — a CPU fault's
PC, the caller captured by a watchpoint hook, a peripheral access log line — into
function names. The release firmware ELF is enough for that, and reading it needs
nothing beyond the standard library (no `pip`, and no `arm-none-eabi-addr2line` in
this environment).

Supports the 32-bit little-endian ARM ELFs the Flipper firmware ships as.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

#: ELF section types that hold symbols.
_SHT_SYMTAB = 2
_SHT_DYNSYM = 11

#: Symbol types (the low nibble of st_info).
_STT_OBJECT = 1
_STT_FUNC = 2


@dataclass(frozen=True)
class Symbol:
    """One entry of the ELF symbol table."""

    name: str
    address: int
    size: int
    kind: int

    @property
    def end(self) -> int:
        return self.address + self.size

    @property
    def is_function(self) -> bool:
        return self.kind == _STT_FUNC


@dataclass(frozen=True)
class Resolution:
    """A resolved address: the containing symbol and the offset into it."""

    symbol: Symbol
    offset: int
    exact: bool

    def describe(self) -> str:
        suffix = "" if self.offset == 0 else "+0x%X" % self.offset
        return "%s%s (0x%08X)" % (self.symbol.name, suffix, self.symbol.address + self.offset)


class ElfError(Exception):
    """The file is not a usable ELF."""


class ElfSymbolTable:
    """The symbol table of a 32-bit little-endian ARM ELF."""

    def __init__(self, symbols: Sequence[Symbol], path: str = "") -> None:
        self.path = path
        self.symbols: List[Symbol] = sorted(
            (symbol for symbol in symbols if symbol.address), key=lambda symbol: symbol.address
        )

    @property
    def function_count(self) -> int:
        return sum(1 for symbol in self.symbols if symbol.is_function)

    def resolve(self, address: int) -> Optional[Resolution]:
        """Return the symbol containing ``address``, else the nearest one below."""
        best: Optional[Symbol] = None
        nearest: Optional[Symbol] = None
        for symbol in self.symbols:
            if symbol.address > address:
                break
            nearest = symbol
            if symbol.size and symbol.address <= address < symbol.end:
                # Prefer the innermost (smallest) containing symbol.
                if best is None or symbol.size < best.size:
                    best = symbol
        if best is not None:
            return Resolution(best, address - best.address, True)
        if nearest is not None:
            return Resolution(nearest, address - nearest.address, False)
        return None

    def describe(self, address: int) -> str:
        resolution = self.resolve(address)
        if resolution is None:
            return "0x%08X (unresolved)" % address
        prefix = "" if resolution.exact else "near "
        return "%s0x%08X -> %s" % (prefix, address, resolution.describe())

    @classmethod
    def read(cls, path: str) -> "ElfSymbolTable":
        with open(path, "rb") as handle:
            blob = handle.read()
        return cls.parse(blob, path)

    @classmethod
    def parse(cls, blob: bytes, path: str = "") -> "ElfSymbolTable":
        if len(blob) < 0x34 or blob[:4] != b"\x7fELF":
            raise ElfError("not an ELF file")
        if blob[4] != 1:
            raise ElfError("only 32-bit ELFs are supported (a Cortex-M image here)")
        if blob[5] != 1:
            raise ElfError("only little-endian ELFs are supported")

        section_offset = struct.unpack_from("<I", blob, 0x20)[0]
        section_size = struct.unpack_from("<H", blob, 0x2E)[0]
        section_count = struct.unpack_from("<H", blob, 0x30)[0]
        if not section_offset or not section_count:
            raise ElfError("no section headers")

        sections = []
        for index in range(section_count):
            base = section_offset + index * section_size
            if base + 40 > len(blob):
                break
            fields = struct.unpack_from("<IIIIIIIIII", blob, base)
            sections.append(
                {
                    "type": fields[1],
                    "offset": fields[4],
                    "size": fields[5],
                    "link": fields[6],
                    "entry_size": fields[9],
                }
            )

        symbols: List[Symbol] = []
        for section in sections:
            if section["type"] not in (_SHT_SYMTAB, _SHT_DYNSYM):
                continue
            if section["link"] >= len(sections):
                continue
            strings = sections[section["link"]]
            string_blob = blob[strings["offset"] : strings["offset"] + strings["size"]]

            entry_size = section["entry_size"] or 16
            for index in range(section["size"] // entry_size):
                base = section["offset"] + index * entry_size
                if base + 16 > len(blob):
                    break
                name_offset, value, size, info = struct.unpack_from("<IIIB", blob, base)
                if not name_offset or not value:
                    continue
                terminator = string_blob.find(b"\x00", name_offset)
                raw = (
                    string_blob[name_offset:terminator]
                    if terminator != -1
                    else string_blob[name_offset:]
                )
                name = raw.decode("ascii", errors="replace")
                if name:
                    symbols.append(Symbol(name, value, size, info & 0xF))
        return cls(symbols, path)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: symbols <firmware.elf> [address ...]")
        return 2
    table = ElfSymbolTable.read(args[0])
    print("%s: %d symbols, %d functions" % (table.path, len(table.symbols), table.function_count))
    for token in args[1:]:
        print(table.describe(int(token, 16)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

