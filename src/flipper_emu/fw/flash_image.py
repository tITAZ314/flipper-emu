"""Assemble, validate and persist the emulated internal flash image.

The 1 MiB STM32WB55 flash is kept in a file (``flash.img`` by default) so that
whatever the firmware writes — settings, OTA updates, the wireless-stack area —
survives across runs, just like the real chip.  Anything the firmware cannot
write stays exactly as the package shipped it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from . import dfu, memmap

#: Byte value of erased flash.
ERASED_BYTE = 0xFF

#: Default file name of the persistent flash image.
DEFAULT_IMAGE_NAME = "flash.img"


class FlashLayoutError(Exception):
    """An image tried to place data outside the emulated flash region."""


@dataclass
class FlashImage:
    """A byte-for-byte image of the internal flash."""

    data: bytearray

    @classmethod
    def erased(cls) -> "FlashImage":
        """A fully erased (all ``0xFF``) 1 MiB flash."""
        return cls(bytearray([ERASED_BYTE]) * memmap.FLASH_SIZE)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "FlashImage":
        """Wrap an existing image, which must be exactly 1 MiB."""
        if len(blob) != memmap.FLASH_SIZE:
            raise FlashLayoutError(
                "flash image is %d bytes, expected %d" % (len(blob), memmap.FLASH_SIZE)
            )
        return cls(bytearray(blob))

    def offset_for(self, address: int) -> int:
        """Translate a CPU address into an offset inside the image."""
        if not memmap.FLASH_BASE <= address < memmap.FLASH_BASE + memmap.FLASH_SIZE:
            raise FlashLayoutError("0x%08X is outside the emulated flash" % address)
        return address - memmap.FLASH_BASE

    def program(self, address: int, payload: bytes) -> Tuple[int, int]:
        """Place ``payload`` at ``address``; returns the written ``(start, end)``."""
        start = self.offset_for(address)
        end = start + len(payload)
        if end > memmap.FLASH_SIZE:
            raise FlashLayoutError(
                "0x%08X + %d bytes overflows the flash" % (address, len(payload))
            )
        self.data[start:end] = payload
        return address, address + len(payload)

    def read(self, address: int, size: int) -> bytes:
        """Read ``size`` bytes starting at ``address``."""
        start = self.offset_for(address)
        return bytes(self.data[start : start + size])

    def vector_table(self) -> Tuple[int, int]:
        """``(initial_sp, reset_vector)`` from the application vector table."""
        return struct_unpack_words(self.data[0:8])

    @property
    def app_size(self) -> int:
        """Size of the populated application area, trailing ``0xFF`` stripped."""
        trimmed = bytes(self.data[: memmap.FLASH_APP_SIZE]).rstrip(b"\xFF")
        return len(trimmed)

    def sha256(self) -> str:
        return hashlib.sha256(bytes(self.data)).hexdigest()

    def describe(self) -> str:
        stack_pointer, reset_vector = self.vector_table()
        return (
            "flash image: %d bytes, app %d bytes, SP=0x%08X reset=0x%08X(thumb=%d), sha256=%s"
            % (
                len(self.data),
                self.app_size,
                stack_pointer,
                reset_vector,
                reset_vector & 1,
                self.sha256()[:16],
            )
        )

    def looks_bootable(self) -> bool:
        """True when the first words look like a Cortex-M4 vector table.

        Guards the persistent image against being replaced by junk (a failed
        platform load, an empty flash, or a truncated download).
        """
        stack_pointer, reset_vector = self.vector_table()
        return (
            0x20000000 < stack_pointer <= 0x20040000
            and memmap.FLASH_BASE <= reset_vector < memmap.FLASH_APP_END
            and (reset_vector & 1) == 1
        )

    def write_file(self, path: str) -> None:
        """Persist atomically so a crash cannot leave a half-written image."""
        temp_path = path + ".tmp"
        with open(temp_path, "wb") as handle:
            handle.write(bytes(self.data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)

    @classmethod
    def read_file(cls, path: str) -> "FlashImage":
        with open(path, "rb") as handle:
            return cls.from_bytes(handle.read())


def struct_unpack_words(blob: bytes) -> Tuple[int, int]:
    """Little-endian pair of 32-bit words (kept dependency-free and explicit)."""
    first = blob[0] | (blob[1] << 8) | (blob[2] << 16) | (blob[3] << 24)
    second = blob[4] | (blob[5] << 8) | (blob[6] << 16) | (blob[7] << 24)
    return first, second


def build_from_dfu(dfu_path: str, verify_crc: bool = False) -> Tuple[FlashImage, dfu.DfuImage]:
    """Build a flash image from a ``.dfu`` package."""
    image = dfu.parse_file(dfu_path, verify_crc=verify_crc)
    flash = FlashImage.erased()
    for element in image.iter_elements():
        flash.program(element.address, element.data)
    return flash, image


def build_from_bin(bin_path: str, address: int = memmap.FLASH_BASE) -> FlashImage:
    """Build a flash image from a raw ``.bin`` (the ``full_bin`` release artifact)."""
    with open(bin_path, "rb") as handle:
        blob = handle.read()
    flash = FlashImage.erased()
    flash.program(address, blob)
    return flash


def load_or_create(
    image_path: str,
    dfu_path: Optional[str] = None,
    verify_crc: bool = False,
    rebuild: bool = False,
) -> Tuple[FlashImage, Optional[dfu.DfuImage]]:
    """Return the persistent flash image, creating it from a package if needed.

    An existing image is *not* rebuilt by default: the firmware owns it and may
    have rewritten parts of it.  Pass ``rebuild=True`` to flash afresh.
    """
    if os.path.exists(image_path) and not rebuild:
        existing = FlashImage.read_file(image_path)
        # An existing image is normally the firmware's own (possibly rewritten
        # by an update), so it is reused -- unless it is not bootable at all, in
        # which case there is nothing of value to preserve and we reflash.
        if existing.looks_bootable() or dfu_path is None:
            return existing, None
    if dfu_path is None:
        raise FlashLayoutError(
            "no flash image at %s and no .dfu package given to create one" % image_path
        )
    flash, image = build_from_dfu(dfu_path, verify_crc=verify_crc)
    flash.write_file(image_path)
    return flash, image
