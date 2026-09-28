# -*- coding: utf-8 -*-
"""Build the microSD image the firmware's first-start slideshow reads.

Why this is host-side Python and not part of the emulator: the card model
(`SdCardSpi.cs`) only moves 512-byte sectors; everything that decides *what* is on
the card belongs here, where it can be unit-tested without Renode.

Three pieces, each checked against the firmware's own source at tag 1.4.3:

1. **PNG -> panel bitmap.** The upstream packer is `scripts/slideshow.py` +
   `scripts/flipper/assets/icon.py`; the pixel step there is "PIL: convert to
   mode 1, invert, save as XBM", i.e. *dark* source pixels become *set* bits,
   8 pixels per byte, least significant bit leftmost, each row padded to a whole
   byte. `tests/test_sdcard_build.py` pins that polarity against a hand-built PNG
   of known pixels.

2. **The `.slideshow` file** (`applications/services/desktop/helpers/slideshow.c`):

   ```
   struct SlideshowFileHeader {  // packed, 8 bytes
       uint32_t magic;           // 0x72676468, rejected if different
       uint8_t version;          // must be <= 1
       uint8_t width; uint8_t height; uint8_t frame_count;
   };
   struct SlideshowFrameHeader { uint16_t size; };   // then `size` bytes
   ```

   Each frame payload is an "icon bitmap": a leading 0x00 means the bytes that
   follow are raw XBM, 0x01 means heatshrink-compressed data with a little-endian
   length. This builder emits the raw form - it is what upstream itself emits
   whenever compression does not pay off, and it keeps the file readable here.

3. **A FAT16 volume with VFAT long names.** In 1.4.3 there is no internal flash
   volume: `/int/...` is rewritten to `/ext/.int/...` on the card
   (`STORAGE_INTERNAL_DIR_NAME ".int"`, `storage_process_alias()`), so the file
   the Desktop looks for lives at `/.int/.slideshow` in the volume root. FatFS
   here is built with LFN (`ff_wtoupper` and `LfnOfs` are in the ELF), and its
   `create_name()` strips leading dots when it derives the short name, so the
   entries are written exactly as FatFS itself would: long name `.int` over short
   name `INT`, and `.slideshow` over `SLIDESHO`.

Usage::

    py -3.9 -m flipper_emu.platform.sdcard_build --frames artifacts/first_start-dev \
        --out artifacts/sdcard.img

The image is written 512-byte sector by sector and stays *writable* by the
emulator, because the firmware deletes `.slideshow` when the slideshow exits -
rebuild it before each verification run.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class Frame:
    """One slideshow frame: 1 means a dark (set) pixel, packing does the rest."""

    width: int
    height: int
    pixels: List[List[int]] = field(default_factory=list)

    @property
    def dark_pixels(self) -> int:
        return sum(sum(row) for row in self.pixels)

    def to_xbm(self) -> bytes:
        """XBM layout: (width + 7) // 8 bytes per row, LSB = leftmost pixel."""
        stride = (self.width + 7) // 8
        data = bytearray(stride * self.height)
        for y, row in enumerate(self.pixels):
            for x, pixel in enumerate(row):
                if pixel:
                    data[y * stride + (x >> 3)] |= 1 << (x & 7)
        return bytes(data)


def _unfilter(raw: bytes, width: int, height: int, stride: int) -> List[bytearray]:
    """Undo the PNG row filters (the spec defines 0-4 for non-interlaced files)."""
    rows: List[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(stride):
                line[index] = (line[index] + (line[index - 1] if index else 0)) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - 1] if index else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - 1] if index else 0
                up = previous[index]
                upleft = previous[index - 1] if index else 0
                estimate = left + up - upleft
                pa = abs(estimate - left)
                pb = abs(estimate - up)
                pc = abs(estimate - upleft)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = upleft
                line[index] = (line[index] + predictor) & 0xFF
        elif filter_type != 0:
            raise ValueError("unsupported PNG filter %d" % filter_type)
        rows.append(line)
        previous = line
    return rows


def read_png(path: str) -> Frame:
    """Read a grayscale PNG (bit depth 1..8, not interlaced) into a Frame."""
    with open(path, "rb") as handle:
        blob = handle.read()
    if blob[:8] != PNG_MAGIC:
        raise ValueError("%s is not a PNG" % path)

    offset = 8
    width = height = depth = color = interlace = 0
    compressed = bytearray()
    while offset + 8 <= len(blob):
        length = struct.unpack_from(">I", blob, offset)[0]
        kind = blob[offset + 4 : offset + 8]
        payload = blob[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break

    if color != 0:
        raise ValueError("%s: only grayscale is supported (colour type %d)" % (path, color))
    if interlace:
        raise ValueError("%s: interlaced PNGs are not supported" % path)
    if depth not in (1, 2, 4, 8):
        raise ValueError("%s: unsupported bit depth %d" % (path, depth))

    stride = (width * depth + 7) // 8
    rows = _unfilter(zlib.decompress(bytes(compressed)), width, height, stride)
    full_scale = (1 << depth) - 1

    pixels: List[List[int]] = []
    for line in rows:
        row: List[int] = []
        for x in range(width):
            if depth == 8:
                value = line[x]
                row.append(1 if value < 128 else 0)
            else:
                per_byte = 8 // depth
                shift = 8 - depth * (x % per_byte + 1)
                value = (line[x // per_byte] >> shift) & full_scale
                # A 1-bit grayscale PNG stores black as 0, so "value < half
                # scale" is ink - the same thing PIL's invert+XBM path produces.
                row.append(1 if value * 2 < full_scale else 0)
        pixels.append(row)
    return Frame(width, height, pixels)


# --------------------------------------------------------------------------
# The .slideshow file
# --------------------------------------------------------------------------

SLIDESHOW_MAGIC = 0x72676468
SLIDESHOW_VERSION = 1

#: Frame payload tag meaning "the bytes after this one are raw XBM" (the
#: alternative, 0x01, is heatshrink-compressed data with a uint16 length).
RAW_FRAME_TAG = 0x00


def pack_slideshow(frames: Sequence[Frame]) -> bytes:
    """Serialise frames exactly as the firmware's `slideshow_load()` reads them."""
    if not frames:
        raise ValueError("a slideshow needs at least one frame")
    if len({frame.width for frame in frames}) != 1 or len({frame.height for frame in frames}) != 1:
        raise ValueError("all frames must share one size (upstream rejects mixed sizes too)")

    blob = bytearray(
        struct.pack(
            "<IBBBB",
            SLIDESHOW_MAGIC,
            SLIDESHOW_VERSION,
            frames[0].width,
            frames[0].height,
            len(frames),
        )
    )
    for frame in frames:
        payload = bytes([RAW_FRAME_TAG]) + frame.to_xbm()
        blob.extend(struct.pack("<H", len(payload)))
        blob.extend(payload)
    return bytes(blob)


# --------------------------------------------------------------------------
# FAT16 with VFAT long names
# --------------------------------------------------------------------------

SECTOR_SIZE = 512
ATTR_DIRECTORY = 0x10
ATTR_LONG_NAME = 0x0F

@dataclass
class VolumeFile:
    """One entry to place in the image; directories carry children."""

    name: str
    data: bytes = b""
    is_dir: bool = False
    children: List["VolumeFile"] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.name.lstrip(" .") != self.name or len(self.name) > 12 or "." in self.name[1:]


def lfn_checksum(short_name: bytes) -> int:
    """The VFAT checksum over the 11-byte short name (must match, or FatFS ignores the LFN)."""
    total = 0
    for byte in short_name:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def short_name(long_name: str) -> bytes:
    """The 11-byte short name FatFS's `create_name()` derives from `long_name`.

    This is a direct port of the LFN-enabled branch of `create_name()` in
    `lib/fatfs/ff.c` (the one that runs when `_USE_LFN != 0`, which is how the
    1.4.3 firmware is built: `lib/fatfs/ffconf_template.h` sets `_USE_LFN 3`).
    Getting this wrong is invisible until a lookup fails, so the details matter:

    * trailing spaces and dots are snipped off first;
    * leading spaces and dots are stripped (this also sets `NS_LOSS`, so FatFS
      will *not* fall back to comparing short names);
    * `di` marks the first character after the *last* dot - the extension start.
      Because a leading dot is stripped but still counted, a name like
      `.slideshow` has `di <= si`: the very first character switches the builder
      into the extension section, so the body stays empty and `slideshow` is
      truncated into the three-character extension, giving `b"        SLI"`.
      `.int` similarly becomes `b"        INT"`;
    * characters illegal in a short name (`+,;=[]`) become `_`.
    """

    def keep(char: str) -> str:
        return "_" if char in "+,;=[]" else char.upper()

    name = long_name.rstrip(" .")  # ff.c: snip off trailing spaces and dots
    if not name:
        return b" " * 11

    si = 0
    while si < len(name) and name[si] in " .":  # ff.c: strip leading spaces and dots
        si += 1

    di = len(name)  # ff.c: find the extension (di <= si means "no extension")
    while di and name[di - 1] != ".":
        di -= 1

    out = [" "] * 11
    i, ni = 0, 8
    while si < len(name):
        char = name[si]
        si += 1
        if char == " " or (char == "." and si != di):
            continue
        if i >= ni or si == di:
            if ni == 11:  # extension longer than three characters
                break
            if si > di:  # no extension at all: the body is full
                break
            si = di
            i, ni = 8, 11
            continue
        out[i] = keep(char)
        i += 1

    return "".join(out).encode("ascii")


def lfn_entries(long_name: str, checksum: int) -> List[bytes]:
    """VFAT long-name entries, stored first-to-last the way the spec requires."""
    encoded = long_name.encode("utf-16-le")
    units = [
        encoded[index : index + 2].ljust(2, b"\x00") for index in range(0, len(encoded), 2)
    ]
    units.append(b"\x00\x00")
    while len(units) % 13:
        units.append(b"\xff\xff")

    entries = []
    total = len(units) // 13
    for index in range(total):
        chunk = units[index * 13 : (index + 1) * 13]
        order = total - index  # physically first entry carries the highest order
        entry = bytearray(32)
        entry[0] = order | (0x40 if index == 0 else 0)
        entry[1:11] = b"".join(chunk[0:5])
        entry[11] = ATTR_LONG_NAME
        entry[12] = 0
        entry[13] = checksum
        entry[14:26] = b"".join(chunk[5:11])
        entry[26:28] = b"\x00\x00"
        entry[28:32] = b"".join(chunk[11:13])
        entries.append(bytes(entry))
    return entries


class FatImage:
    """A minimally complete FAT16 formatter: enough for FatFS to mount this volume.

    Geometry is fixed and deliberately boring (512-byte sectors, one sector per
    cluster, two FATs, a 512-entry root): the smallest thing a stock FatFS with
    LFN accepts. Everything FatFS actually validates is here - jump instruction,
    BPB fields, media byte, boot signature, both FAT copies, and the `FAT16   `
    type string.
    """

    def __init__(self, total_sectors: int = 16384, volume_label: str = "FLIPPER"):
        self.total_sectors = total_sectors
        self.volume_label = volume_label.upper()[:11].ljust(11)
        self.reserved_sectors = 1
        self.fat_count = 2
        self.root_entries = 512
        self.root_sectors = (self.root_entries * 32) // SECTOR_SIZE
        self.fat_sectors = self._fat_sectors()
        self.data_start = (
            self.reserved_sectors + self.fat_count * self.fat_sectors + self.root_sectors
        )
        self.cluster_count = self.total_sectors - self.data_start
        if self.cluster_count < 16:
            raise ValueError("volume is too small: %d clusters" % self.cluster_count)
        if self.cluster_count > 0xFFF4:
            raise ValueError("too many clusters for FAT16: %d" % self.cluster_count)

    def _fat_sectors(self) -> int:
        """FAT size in sectors; FAT16 needs one 16-bit entry per cluster (+2)."""
        fat = 1
        while True:
            data_sectors = (
                self.total_sectors
                - self.reserved_sectors
                - self.fat_count * fat
                - self.root_sectors
            )
            needed = ((data_sectors + 2) * 2 + SECTOR_SIZE - 1) // SECTOR_SIZE
            if needed <= fat:
                return fat
            fat = needed
            if fat >= self.total_sectors:
                raise ValueError("volume is too small to hold its own FAT")

    def _entry_blob(self, node: VolumeFile, first_cluster: int) -> bytes:
        """Long-name entries, then the short entry they describe."""
        short = short_name(node.name)
        blob = bytearray()
        blob.extend(b"".join(lfn_entries(node.name, lfn_checksum(short))))
        entry = bytearray(32)
        entry[0:11] = short
        entry[11] = ATTR_DIRECTORY if node.is_dir else 0x00
        entry[12] = 0x00  # case lives in the long name
        struct.pack_into("<H", entry, 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", entry, 26, first_cluster & 0xFFFF)
        struct.pack_into("<I", entry, 28, 0 if node.is_dir else len(node.data))
        blob.extend(entry)
        return bytes(blob)

    def _clusters_for(self, node: VolumeFile) -> int:
        if not node.is_dir:
            return (len(node.data) + SECTOR_SIZE - 1) // SECTOR_SIZE
        used = 2  # "." and ".."
        for child in node.children:
            short = short_name(child.name)
            used += 1 + len(lfn_entries(child.name, lfn_checksum(short)))
        return max(1, (used + 15) // 16)  # 16 entries fit in one 512-byte cluster

    def build(self, entries: Sequence[VolumeFile]) -> bytes:
        """Lay the volume out: boot sector, both FATs, root dir, then clusters."""
        image = bytearray(self.total_sectors * SECTOR_SIZE)
        fat = [0] * (self.cluster_count + 2)
        plan: Dict[int, int] = {}
        next_cluster = 2

        def allocate(count: int) -> int:
            nonlocal next_cluster
            if count <= 0:
                return 0
            first = next_cluster
            for index in range(count):
                if next_cluster > self.cluster_count + 1:
                    raise ValueError("image too small for the requested contents")
                fat[next_cluster] = 0xFFFF if index == count - 1 else next_cluster + 1
                next_cluster += 1
            return first

        def plan_tree(nodes: Sequence[VolumeFile]) -> None:
            for node in nodes:
                plan[id(node)] = allocate(self._clusters_for(node))
                if node.is_dir:
                    plan_tree(node.children)

        plan_tree(entries)

        def chain(first: int) -> List[int]:
            clusters: List[int] = []
            cluster = first
            while cluster:
                clusters.append(cluster)
                cluster = 0 if fat[cluster] >= 0xFFF8 else fat[cluster]
            return clusters

        def write_clusters(clusters: Sequence[int], blob: bytes) -> None:
            padded = blob + b"\x00" * (len(clusters) * SECTOR_SIZE - len(blob))
            for index, cluster in enumerate(clusters):
                start = (self.data_start + cluster - 2) * SECTOR_SIZE
                image[start : start + SECTOR_SIZE] = padded[
                    index * SECTOR_SIZE : (index + 1) * SECTOR_SIZE
                ]

        def write_directory(node: VolumeFile, parent_cluster: int) -> None:
            clusters = chain(plan[id(node)])
            blob = bytearray()
            dot = bytearray(b" " * 32)
            dot[0] = ord(".")
            dot[11] = ATTR_DIRECTORY
            struct.pack_into("<H", dot, 26, clusters[0])
            dotdot = bytearray(b" " * 32)
            dotdot[0:2] = b".."
            dotdot[11] = ATTR_DIRECTORY
            struct.pack_into("<H", dotdot, 26, parent_cluster)
            blob.extend(dot)
            blob.extend(dotdot)
            for child in node.children:
                blob.extend(self._entry_blob(child, plan[id(child)]))
            write_clusters(clusters, bytes(blob))

        def walk(nodes: Sequence[VolumeFile], parent_cluster: int) -> None:
            for node in nodes:
                if node.is_dir:
                    write_directory(node, parent_cluster)
                    walk(node.children, plan[id(node)])
                else:
                    write_clusters(chain(plan[id(node)]), node.data)

        image[0:SECTOR_SIZE] = self._boot_sector()

        fat_bytes = bytearray(self.fat_sectors * SECTOR_SIZE)
        for index, value in enumerate(fat):
            struct.pack_into("<H", fat_bytes, index * 2, value)
        for copy in range(self.fat_count):
            start = (self.reserved_sectors + copy * self.fat_sectors) * SECTOR_SIZE
            image[start : start + len(fat_bytes)] = fat_bytes

        root = bytearray(self.root_sectors * SECTOR_SIZE)
        offset = 0
        for node in entries:
            blob = self._entry_blob(node, plan[id(node)])
            root[offset : offset + len(blob)] = blob
            offset += len(blob)
        if offset > len(root):
            raise ValueError("root directory overflow (%d bytes)" % offset)
        start = (self.reserved_sectors + self.fat_count * self.fat_sectors) * SECTOR_SIZE
        image[start : start + len(root)] = root

        walk(entries, 0)
        return bytes(image)

    def _boot_sector(self) -> bytes:
        sector = bytearray(SECTOR_SIZE)
        sector[0:3] = b"\xEB\x3C\x90"
        sector[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", sector, 11, SECTOR_SIZE)
        sector[13] = 1
        struct.pack_into("<H", sector, 14, self.reserved_sectors)
        sector[16] = self.fat_count
        struct.pack_into("<H", sector, 17, self.root_entries)
        struct.pack_into(
            "<H", sector, 19, self.total_sectors if self.total_sectors < 0x10000 else 0
        )
        sector[21] = 0xF8
        struct.pack_into("<H", sector, 22, self.fat_sectors)
        struct.pack_into("<H", sector, 24, 63)
        struct.pack_into("<H", sector, 26, 255)
        struct.pack_into("<I", sector, 28, 0)
        struct.pack_into("<I", sector, 32, self.total_sectors)
        sector[36] = 0x80
        sector[38] = 0x29
        struct.pack_into("<I", sector, 39, 0x1A2B3C4D)
        sector[43:54] = self.volume_label.encode("ascii")
        sector[54:62] = b"FAT16   "
        sector[510:512] = b"\x55\xAA"
        return bytes(sector)


# --------------------------------------------------------------------------
# Reading the image back (this is how the slideshow's deletion gets verified)
# --------------------------------------------------------------------------


@dataclass
class FatEntry:
    """One parsed directory entry."""

    name: str
    is_dir: bool
    first_cluster: int
    size: int


def _parse_entries(data: bytes) -> List[FatEntry]:
    """Walk 32-byte entries, folding VFAT long-name entries into their short entry."""
    entries: List[FatEntry] = []
    long_name: List[str] = []
    for offset in range(0, len(data) - 31, 32):
        entry = data[offset : offset + 32]
        if entry[0] == 0x00:
            break
        if entry[0] == 0xE5:
            long_name = []
            continue
        attributes = entry[11]
        if attributes == ATTR_LONG_NAME:
            chars = entry[1:11] + entry[14:26] + entry[28:32]
            text = chars.decode("utf-16-le", errors="replace")
            text = text.split("\x00")[0].split("\uffff")[0]
            # Long names are stored tail-first, so each chunk prepends the last one.
            long_name.insert(0, text)
            continue
        if attributes & 0x08 and not attributes & ATTR_DIRECTORY:
            long_name = []
            continue  # volume label
        if long_name:
            name = "".join(long_name)
        else:
            # Short names are space padded, but some writers use NUL, so strip both.
            body = entry[0:8].decode("ascii", errors="replace").rstrip(" \x00")
            extension = entry[8:11].decode("ascii", errors="replace").rstrip(" \x00")
            name = body + ("." + extension if extension else "")
        long_name = []
        entries.append(
            FatEntry(
                name=name,
                is_dir=bool(attributes & ATTR_DIRECTORY),
                # Both halves of the cluster number are little-endian words (the
                # high half was added by FAT32 and stays zero here); reading them
                # big-endian turns cluster 2 into 512, hence the explicit unpack.
                first_cluster=struct.unpack_from("<H", entry, 20)[0] << 16
                | struct.unpack_from("<H", entry, 26)[0],
                size=struct.unpack_from("<I", entry, 28)[0],
            )
        )
    return entries


def read_fat16(blob: bytes) -> Dict[str, bytes]:
    """Every file in a FAT16 volume as ``{"/.int/.slideshow": bytes}``, LFN aware."""
    bytes_per_sector = struct.unpack_from("<H", blob, 11)[0]
    sectors_per_cluster = blob[13]
    reserved = struct.unpack_from("<H", blob, 14)[0]
    fat_count = blob[16]
    root_entries = struct.unpack_from("<H", blob, 17)[0]
    fat_sectors = struct.unpack_from("<H", blob, 22)[0]
    root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
    root_start = (reserved + fat_count * fat_sectors) * bytes_per_sector
    data_start = root_start + root_dir_sectors * bytes_per_sector
    cluster_bytes = sectors_per_cluster * bytes_per_sector

    def fat_value(cluster: int) -> int:
        return struct.unpack_from("<H", blob, reserved * bytes_per_sector + cluster * 2)[0]

    def read_chain(first: int, size: Optional[int] = None) -> bytes:
        data = bytearray()
        cluster = first
        while cluster and cluster < 0xFFF8:
            start = data_start + (cluster - 2) * cluster_bytes
            data.extend(blob[start : start + cluster_bytes])
            if size is not None and len(data) >= size:
                break
            cluster = fat_value(cluster)
        return bytes(data[:size]) if size is not None else bytes(data)

    files: Dict[str, bytes] = {}

    def walk(data: bytes, prefix: str) -> None:
        for entry in _parse_entries(data):
            if entry.name in (".", ".."):
                continue
            path = prefix + entry.name
            if entry.is_dir:
                walk(read_chain(entry.first_cluster), path + "/")
            else:
                files[path] = read_chain(entry.first_cluster, entry.size)

    walk(blob[root_start : root_start + root_dir_sectors * bytes_per_sector], "")
    return files


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def collect_frames(folder: str) -> List[Frame]:
    """Read frame_00.png, frame_01.png, ... in order (the packer's own naming)."""
    frames: List[Frame] = []
    index = 0
    while True:
        path = os.path.join(folder, "frame_%02d.png" % index)
        if not os.path.exists(path):
            break
        frames.append(read_png(path))
        index += 1
    if not frames:
        raise FileNotFoundError("no frame_NN.png files in %s" % folder)
    return frames


def build_card_image(
    frames_folder: str, out_path: str, size_mib: int = 8, file_name: str = ".slideshow"
) -> Dict[str, object]:
    """Write a FAT16 image holding ``/.int/<file_name>`` built from PNG frames."""
    frames = collect_frames(frames_folder)
    slideshow = pack_slideshow(frames)
    directory = VolumeFile(
        name=".int", is_dir=True, children=[VolumeFile(name=file_name, data=slideshow)]
    )
    image = FatImage(total_sectors=size_mib * 2048, volume_label="FLIPPER").build([directory])
    with open(out_path, "wb") as handle:
        handle.write(image)

    # Runs work on a copy. The firmware writes to the card it is given (it deletes
    # /.int/.slideshow after the first-start slideshow, saves settings, and so on), and
    # a run also keeps the image open for the whole session - which locks out the next
    # run. Copying here means the built image stays pristine and each run starts from
    # identical content.
    run_path = os.path.join(os.path.dirname(out_path) or ".", "sdcard-run.img")
    with open(run_path, "wb") as handle:
        handle.write(image)
    return {
        "path": out_path,
        "frames": len(frames),
        "frame_size": "%dx%d" % (frames[0].width, frames[0].height),
        "slideshow_bytes": len(slideshow),
        "image_bytes": len(image),
        "dark_pixels": sum(frame.dark_pixels for frame in frames),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build or inspect the emulated SD card image")
    parser.add_argument("--frames", default="artifacts/first_start-dev", help="frame_NN.png folder")
    parser.add_argument("--out", default="artifacts/sdcard.img", help="image file to write")
    parser.add_argument("--size-mib", type=int, default=8, help="volume size (default 8 MiB)")
    parser.add_argument("--name", default=".slideshow", help="file name inside /.int")
    parser.add_argument("--list", dest="list_image", help="list an existing image instead of building")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list_image:
        with open(args.list_image, "rb") as handle:
            files = read_fat16(handle.read())
        if not files:
            print("%s: no files" % args.list_image)
        for name in sorted(files):
            print("%-28s %d bytes" % (name, len(files[name])))
        return 0

    summary = build_card_image(args.frames, args.out, args.size_mib, args.name)
    print("image      %s (%d bytes)" % (summary["path"], summary["image_bytes"]))
    print(
        "frames     %d at %s, %d dark pixels in total"
        % (summary["frames"], summary["frame_size"], summary["dark_pixels"])
    )
    print("slideshow  %d bytes -> /.int/%s" % (summary["slideshow_bytes"], args.name))
    with open(args.out, "rb") as handle:
        contents = read_fat16(handle.read())
    print("contents   %s" % ", ".join(sorted(contents)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
