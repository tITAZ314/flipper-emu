"""DfuSe (``.dfu``) firmware image parsing.

This module is the *firmware loader* boundary of the project: it turns a real,
unmodified Flipper Zero ``.dfu`` package (the very file qFlipper writes over
USB) into ``(address, data)`` elements that the emulated STM32WB55 internal
flash can absorb.

Flipper packages use a *variant* of the DfuSe layout: each target record
carries 11 extra bytes before the 255-byte target name and 3 padding bytes
between ``bNumElements`` and the first element record.  Both the classic DfuSe
layout and the Flipper variant are supported, and the parser only accepts a
chain that consumes exactly the declared payload, so a mis-parse is reported
instead of silently loading garbage.

Nothing here depends on the emulation backend, which keeps the loader
independently testable (see ``tests/test_dfu_loader.py``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: DfuSe prefix signature (5 bytes), followed by a 1-byte format version.
DFU_SIGNATURE = b"DfuSe"

#: Length of the trailing DFU suffix (``bcdDevice`` .. ``dwCRC``).
DFU_SUFFIX_SIZE = 16

#: Magic in the DFU suffix (``ucDfuSignature``).
DFU_SUFFIX_MAGIC = b"UFD"

#: ST vendor id, as used by the Flipper DFU suffix.
STM_DFU_VID = 0x0483

#: Regions an element address may legally fall into.  Deliberately coarse: the
#: loader only has to reject nonsense, the memory map owns the details.
PLAUSIBLE_ELEMENT_REGIONS: Tuple[Tuple[int, int], ...] = (
    (0x08000000, 0x08100000),  # internal flash, 1 MiB
    (0x1FFF0000, 0x20000000),  # OTP, option bytes, system memory
    (0x20000000, 0x20040000),  # SRAM1 + SRAM2a + SRAM2b, 256 KiB
)

#: Candidate header layouts: (bytes before name, name length, padding after
#: ``bNumElements``).  "flipper" is what the official packages use today,
#: "classic" is the plain DfuSe 1.1a layout kept as a fallback.
_LAYOUT_FLIPPER = ("flipper", 11, 255, 3)
_LAYOUT_CLASSIC = ("classic", 0, 255, 0)

#: Length of the DfuSe prefix (signature + version + image size + target count).
_HEADER_SIZE = 11

#: The minimum number of bytes a single element record consumes.
_ELEMENT_HEADER_SIZE = 8


class DfuError(Exception):
    """Base class for every ``.dfu`` loading problem."""


class DfuFormatError(DfuError):
    """The container is not a valid DfuSe image."""


class DfuValidationError(DfuError):
    """The container parsed, but the element chain does not add up."""


def _crc32_poly(data: bytes, invert_output: bool) -> int:
    """CRC-32, polynomial ``0x04C11DB7`` over a big-endian byte stream.

    ``invert_output`` picks between the two conventions found in the wild; both
    are exposed because the Flipper generator agrees with neither one (see
    ``crc32_dfu`` / ``crc32_stm32`` and the loader tests).
    """
    table: List[int] = []
    for index in range(256):
        crc = index << 24
        for _ in range(8):
            crc = (((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)) & 0xFFFFFFFF
        table.append(crc)
    value = 0xFFFFFFFF
    for byte in data:
        value = ((value << 8) & 0xFFFFFFFF) ^ table[((value >> 24) ^ byte) & 0xFF]
    return value ^ (0xFFFFFFFF if invert_output else 0)


def crc32_dfu(data: bytes) -> int:
    """DFU/zip flavour: init ``0xFFFFFFFF`` plus final inversion.

    ``crc32_dfu(b"123456789") == 0xFC891918``.
    """
    return _crc32_poly(data, True)


def crc32_stm32(data: bytes) -> int:
    """STM32 CRC-peripheral flavour: same polynomial, no final inversion.

    ``crc32_stm32(b"123456789") == 0x0376E6E7``.
    """
    return _crc32_poly(data, False)

@dataclass
class DfuSuffix:
    """Trailing 16-byte DFU suffix: device identity plus an integrity check."""

    bcd_device: int
    id_product: int
    id_vendor: int
    bcd_dfu: int
    length: int
    crc: int
    crc_ok: Optional[bool] = None
    crc_variant: Optional[str] = None


@dataclass
class DfuElement:
    """One contiguous block of bytes destined for one memory address."""

    index: int
    address: int
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def end(self) -> int:
        return self.address + len(self.data)

    @property
    def vector_words(self) -> Tuple[int, int]:
        """The block's first two words as ``(initial_sp, reset_vector)``."""
        if len(self.data) < 8:
            raise DfuValidationError("element too short to hold a vector table")
        return struct.unpack_from("<II", self.data, 0)


@dataclass
class DfuTarget:
    """A named DfuSe target holding one or more elements."""

    index: int
    name: str
    declared_size: int
    elements: Tuple[DfuElement, ...]

    @property
    def size(self) -> int:
        return sum(element.size for element in self.elements)


@dataclass
class DfuImage:
    """A parsed ``.dfu`` package."""

    version: int
    declared_image_size: int
    targets: Tuple[DfuTarget, ...]
    layout: str = ""
    payload_end: int = 0
    suffix: Optional[DfuSuffix] = None
    source: str = ""

    @property
    def element_count(self) -> int:
        return sum(len(target.elements) for target in self.targets)

    @property
    def total_bytes(self) -> int:
        return sum(target.size for target in self.targets)

    @property
    def target_bytes(self) -> int:
        """Bytes available for target records (everything after the header)."""
        return self.payload_end - 11

    def iter_elements(self):
        for target in self.targets:
            for element in target.elements:
                yield element

    def describe(self) -> str:
        lines = [
            "image %s" % (self.source or "<bytes>"),
            "  DfuSe version=%d declared_size=%d data_end=%d target_bytes=%d layout=%s"
            % (
                self.version,
                self.declared_image_size,
                self.payload_end,
                self.target_bytes,
                self.layout,
            ),
            "  targets=%d elements=%d data=%d bytes"
            % (len(self.targets), self.element_count, self.total_bytes),
        ]
        for target in self.targets:
            lines.append(
                "  target %d %r declared_size=%d elements=%d"
                % (target.index, target.name, target.declared_size, len(target.elements))
            )
            for element in target.elements:
                lines.append(
                    "    element %d: 0x%08X..0x%08X (%d bytes)"
                    % (element.index, element.address, element.end, element.size)
                )
        if self.suffix is not None:
            lines.append(
                "  suffix vid=0x%04X pid=0x%04X crc=0x%08X crc_ok=%s"
                % (
                    self.suffix.id_vendor,
                    self.suffix.id_product,
                    self.suffix.crc,
                    self.suffix.crc_ok,
                )
            )
        return "\n".join(lines)

def _in_plausible_region(address: int) -> bool:
    return any(start <= address < end for start, end in PLAUSIBLE_ELEMENT_REGIONS)


def _try_layout(blob: bytes, payload_end: int, layout: Tuple[str, int, int, int]):
    """Walk the target/element chain with one candidate layout.

    Returns ``(targets, layout_label)`` when the chain consumes exactly the
    declared payload, otherwise ``None`` — a chain that does not line up is
    rejected rather than trusted.
    """
    label, prefix_len, name_len, pad_len = layout
    offset = _HEADER_SIZE
    targets: List[DfuTarget] = []

    while offset < payload_end:
        if offset + prefix_len + name_len + 5 + pad_len > payload_end:
            return None
        offset += prefix_len

        name = blob[offset : offset + name_len].split(b"\x00", 1)[0]
        offset += name_len
        name_text = name.decode("ascii", errors="replace")

        declared_size = struct.unpack_from("<I", blob, offset)[0]
        offset += 4
        element_count = blob[offset]
        offset += 1 + pad_len

        elements: List[DfuElement] = []
        for element_index in range(element_count):
            if offset + _ELEMENT_HEADER_SIZE > payload_end:
                return None
            address, size = struct.unpack_from("<II", blob, offset)
            offset += _ELEMENT_HEADER_SIZE
            if not _in_plausible_region(address) or size == 0:
                return None
            if offset + size > payload_end:
                return None
            elements.append(DfuElement(element_index, address, blob[offset : offset + size]))
            offset += size

        targets.append(DfuTarget(len(targets), name_text, declared_size, tuple(elements)))

    if offset != payload_end or not targets:
        return None
    return targets, label


def _parse_suffix(blob: bytes) -> Optional[DfuSuffix]:
    """Parse the trailing 16-byte DFU suffix, if present.

    Layout: bcdDevice(2) idProduct(2) idVendor(2) bcdDFU(2) "UFD"(3) bLength(1)
    dwCRC(4) — the Flipper packages carry ``idVendor=0x0483`` and
    ``idProduct=0xDF11``, the ST DFU ids.
    """
    if len(blob) < DFU_SUFFIX_SIZE:
        return None
    suffix = blob[-DFU_SUFFIX_SIZE:]
    if suffix[8:11] != DFU_SUFFIX_MAGIC or suffix[11] != DFU_SUFFIX_SIZE:
        return None
    bcd_device, id_product, id_vendor, bcd_dfu = struct.unpack("<HHHH", suffix[:8])
    crc = struct.unpack("<I", suffix[12:16])[0]
    return DfuSuffix(bcd_device, id_product, id_vendor, bcd_dfu, suffix[11], crc)


def parse_bytes(blob: bytes, source: str = "", verify_crc: bool = False) -> DfuImage:
    """Parse a DfuSe image from raw bytes.

    ``verify_crc`` is opt-in: the pure-Python DFU CRC costs ~2 s on a 750 KiB
    image, and the Flipper generator's ``dwCRC`` does not agree with the DFU
    spec convention anyway (verified against the official 1.4.3 package), so
    integrity is established from the release manifest's SHA256 instead.
    """
    if len(blob) < _HEADER_SIZE + 1:
        raise DfuFormatError("file too short for a DfuSe header (%d bytes)" % len(blob))
    if blob[:5] != DFU_SIGNATURE:
        raise DfuFormatError("bad DfuSe signature %r" % (blob[:5],))

    version = blob[5]
    declared_image_size = struct.unpack_from("<I", blob, 6)[0]
    declared_targets = blob[10]

    suffix = _parse_suffix(blob)
    payload_end = len(blob) - DFU_SUFFIX_SIZE if suffix is not None else len(blob)
    if payload_end <= _HEADER_SIZE:
        raise DfuFormatError("no payload after the 11-byte DfuSe header")

    # dwImageSize counts everything up to the DFU suffix, the 11-byte header
    # included: the official 1.4.3 package declares 768,425, which is exactly
    # where its element data ends and the suffix begins.
    if declared_image_size and declared_image_size != payload_end:
        raise DfuValidationError(
            "declared image size %d != data end %d" % (declared_image_size, payload_end)
        )

    result = None
    for layout in (_LAYOUT_FLIPPER, _LAYOUT_CLASSIC):
        result = _try_layout(blob, payload_end, layout)
        if result is not None:
            break
    if result is None:
        raise DfuValidationError(
            "no consistent element chain (declared targets: %d)" % declared_targets
        )

    targets, label = result
    if declared_targets and declared_targets != len(targets):
        raise DfuValidationError(
            "declared %d target(s), parsed %d" % (declared_targets, len(targets))
        )

    if verify_crc and suffix is not None:
        for variant, value in (("dfu", crc32_dfu(blob[:-4])), ("stm32", crc32_stm32(blob[:-4]))):
            if value == suffix.crc:
                suffix.crc_ok = True
                suffix.crc_variant = variant
                break
        else:
            suffix.crc_ok = False

    return DfuImage(
        version=version,
        declared_image_size=declared_image_size,
        targets=tuple(targets),
        layout=label,
        payload_end=payload_end,
        suffix=suffix,
        source=source,
    )


def parse_file(path: str, verify_crc: bool = False) -> DfuImage:
    """Parse a ``.dfu`` file from disk."""
    with open(path, "rb") as handle:
        blob = handle.read()
    return parse_bytes(blob, source=path, verify_crc=verify_crc)


