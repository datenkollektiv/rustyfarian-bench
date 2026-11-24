"""Write a merged image by its partition table, so a remote flash keeps NVS and boot state.

`python -m rustyfarian_bench.flash_image --chip C --port P IMAGE`. A flat write of a merged image
(`espflash write-bin 0x0`) erases every sector it covers, wiping NVS (Wi-Fi credentials, pairing)
and otadata. When the image carries a partition table at 0x8000 this writes only the bootloader and
table, then each app partition's used range, skips the data partitions and erases otadata (so the
bootloader picks factory or ota_0, the flashed slot, not a stale OTA slot). An image without a
table is written flat, as before.

espflash 4.x merged images have file offset == flash address, so ranges are slices of the file.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import shlex
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import device
from .config import ConfigError

TABLE_OFFSET = 0x8000
TABLE_MAX = 0xC00
TABLE_END = 0x9000  # end of the table sector: the first write is [0, TABLE_END)
SECTOR = 0x1000
APP_ALIGN = 0x10000
APP = 0
DATA = 1
# Newer ESP-IDF tables may list the bootloader and the table themselves, below 0x9000; the first
# write covers them, so they are allowed there and never planned.
BOOTLOADER = 2
PARTITION_TABLE = 3
OTADATA = 0  # data subtype
# Kept past an app's last non-0xFF byte: the app ends with a checksum byte and a SHA-256 digest,
# which may end in 0xFF themselves, and a sector the write stops short of keeps stale bytes.
APP_TAIL = 64

# The chip id in an image header (u16 LE at +12) and where that family's bootloader image sits.
CHIP_IDS: dict[int, str] = {
    0: "esp32",
    2: "esp32s2",
    5: "esp32c3",
    9: "esp32s3",
    12: "esp32c2",
    13: "esp32c6",
    16: "esp32h2",
    18: "esp32p4",
    23: "esp32c5",
}
_AT = {"esp32": 0x1000, "esp32s2": 0x1000, "esp32c5": 0x2000, "esp32p4": 0x2000}
BOOTLOADER_AT: dict[str, int] = {chip: _AT.get(chip, 0) for chip in CHIP_IDS.values()}
_HEADER = 14  # magic, segment count, flash mode and size, entry point, wp pin, spi drive, chip id
_IMAGE_MAGIC = 0xE9

_ENTRY = 32
_MAGIC = b"\xaa\x50"
_MD5 = b"\xeb\xeb"
_END = b"\xff\xff"
_PREFIX = "image: partition table at 0x8000: "


@dataclass(frozen=True)
class Partition:
    type: int
    subtype: int
    offset: int
    size: int
    label: str


@dataclass(frozen=True)
class Op:
    kind: Literal["write", "erase"]
    offset: int
    size: int
    label: str


@dataclass(frozen=True)
class FlashPlan:
    mode: Literal["flat", "segments"]
    ops: tuple[Op, ...]


def chip_of(image: bytes) -> str:
    """The chip a merged image is for, from its bootloader header: pure, no I/O.

    A hit counts only at the offset that chip's family uses, so a stray 0xE9 elsewhere is no
    match; none or several hits mean the file is not a merged image we can place.
    """
    hits = []
    for off in (0x0, 0x1000, 0x2000):
        if len(image) < off + _HEADER or image[off] != _IMAGE_MAGIC:
            continue
        chip = CHIP_IDS.get(struct.unpack_from("<H", image, off + 12)[0])
        if chip is not None and BOOTLOADER_AT[chip] == off:
            hits.append(chip)
    if not hits:
        raise ConfigError(
            "image: no ESP bootloader header at 0x0, 0x1000 or 0x2000; is it a merged image?"
        )
    if len(hits) > 1:
        raise ConfigError("image: ambiguous bootloader header; is it a merged image?")
    return hits[0]


def merged_chip(image: bytes) -> str:
    """`chip_of` for a flash job, which also needs a partition table at 0x8000.

    A bare app image of a 0x0-family chip carries the same header at 0x0; written flat, it
    would replace the bootloader and table. Raises ConfigError for either.
    """
    chip = chip_of(image)
    if parse_table(image) is None:
        raise ConfigError(
            "image: no partition table at 0x8000; flash takes a merged image "
            "(espflash save-image --merge)"
        )
    return chip


def _bad(text: str) -> ConfigError:
    return ConfigError(_PREFIX + text)


def _entry(raw: bytes) -> Partition:
    kind, subtype, offset, size = struct.unpack_from("<BBII", raw, 2)
    name = raw[12:28].split(b"\0", 1)[0]
    if not name.isascii() or not name.decode("ascii").isprintable():
        raise _bad("a label is not printable ASCII")
    label = name.decode("ascii")
    where = f"{label!r} at {offset:#x}"
    below_table = kind in (BOOTLOADER, PARTITION_TABLE) and offset + size <= TABLE_END
    if (offset < TABLE_END and not below_table) or offset % SECTOR:
        raise _bad(f"{where} is not a sector-aligned offset past the table")
    if kind == APP and offset % APP_ALIGN:
        raise _bad(f"{where}: an app must start on a 64 KiB boundary")
    sectored = kind == APP or (kind == DATA and subtype == OTADATA)
    if size == 0 or (sectored and size % SECTOR):
        raise _bad(f"{where} has size {size:#x}")
    if offset + size > 2**32:
        raise _bad(f"{where} runs past 4 GiB")
    return Partition(kind, subtype, offset, size, label)


def parse_table(image: bytes) -> tuple[Partition, ...] | None:
    """The partitions of a merged image, or None when it has no table at 0x8000."""
    if len(image) < TABLE_OFFSET + _ENTRY or image[TABLE_OFFSET : TABLE_OFFSET + 2] != _MAGIC:
        return None
    parts: list[Partition] = []
    for i in range(TABLE_MAX // _ENTRY):
        start = TABLE_OFFSET + i * _ENTRY
        raw = image[start : start + _ENTRY]
        if raw[:2] == _MAGIC and len(raw) == _ENTRY:
            parts.append(_entry(raw))
        elif raw[:2] == _END:
            break
        elif raw[:2] == _MD5 and len(raw) == _ENTRY:
            if hashlib.md5(image[TABLE_OFFSET:start]).digest() != raw[16:]:
                raise _bad("the MD5 entry does not match")
            if image[start + _ENTRY : start + _ENTRY + 2] != _END:
                raise _bad("the MD5 entry is not followed by the end marker")
            break
        else:
            raise _bad(f"entry {i} has an unknown magic")
    else:
        raise _bad("no end marker within 0xc00 bytes")
    ordered = sorted(parts, key=lambda p: p.offset)
    for a, b in itertools.pairwise(ordered):
        if a.offset + a.size > b.offset:
            raise _bad(f"{a.label!r} and {b.label!r} overlap")
    return tuple(parts)


def _round_up(n: int) -> int:
    return -(-n // SECTOR) * SECTOR


def plan(image: bytes) -> FlashPlan:
    """What to flash: pure, no I/O. Flat unless the image has a partition table."""
    parts = parse_table(image)
    if parts is None:
        return FlashPlan("flat", (Op("write", 0, len(image), "image"),))
    ops = [Op("write", 0, TABLE_END, "bootloader+table")]
    for p in parts:
        if p.type == APP:
            used = image[p.offset : min(p.offset + p.size, len(image))].rstrip(b"\xff")
            if used:  # an all-0xFF slot (an empty ota_1) holds no app to write
                size = min(_round_up(len(used) + APP_TAIL), p.size)
                ops.append(Op("write", p.offset, size, p.label))
        elif p.type == DATA and p.subtype == OTADATA:
            ops.append(Op("erase", p.offset, p.size, p.label))
    return FlashPlan("segments", tuple(sorted(ops, key=lambda o: o.offset)))


def segment(image: bytes, op: Op) -> bytes:
    """The bytes of a write op, padded with 0xFF to its size."""
    return image[op.offset : op.offset + op.size].ljust(op.size, b"\xff")


def describe(plan: FlashPlan) -> dict[str, Any]:
    """The plan as JSON-able data, for job.json."""
    ops = [
        {"kind": o.kind, "offset": hex(o.offset), "size": hex(o.size), "label": o.label}
        for o in plan.ops
    ]
    return {"mode": plan.mode, "ops": ops}


def argv_for(op: Op, *, chip: str, port: str, file: str | None, last: bool) -> list[str]:
    """The espflash command for one op; only the last one lets the chip reset."""
    verb = "write-bin" if op.kind == "write" else "erase-region"
    argv = ["espflash", verb, "--non-interactive", "--chip", chip, "--port", port]
    if not last:
        argv += ["--after", "no-reset"]
    if op.kind == "write":
        if file is None:
            raise ValueError("a write op needs a file")
        return [*argv, hex(op.offset), file]
    return [*argv, hex(op.offset), hex(op.size)]


def _espflash(argv: list[str]) -> int:
    try:
        return subprocess.run(argv).returncode
    except (FileNotFoundError, PermissionError) as e:
        print(device.missing_tool(argv[0], e), file=sys.stderr)
        return 127


def main(
    argv: Sequence[str] | None = None, *, run: Callable[[list[str]], int] | None = None
) -> int:
    ap = argparse.ArgumentParser(prog="flash_image", description=__doc__)
    ap.add_argument("--chip", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("image", type=Path)
    args = ap.parse_args(argv)
    run = run or _espflash
    try:
        data = args.image.read_bytes()
    except OSError as e:
        print(f"flash_image: cannot read {args.image}: {e.strerror or e}", file=sys.stderr)
        return 2
    try:
        flash = plan(data)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    slices: contextlib.AbstractContextManager[str | None] = (
        tempfile.TemporaryDirectory(dir=args.image.parent, prefix=".flash-")
        if flash.mode == "segments"
        else contextlib.nullcontext()  # a flat write reads the image itself
    )
    with slices as tmp:
        for n, op in enumerate(flash.ops):
            file: str | None = None
            if op.kind == "write":
                file = str(args.image)
                if tmp is not None:
                    path = Path(tmp) / f"{op.offset:#x}.bin"
                    path.write_bytes(segment(data, op))
                    file = str(path)
            cmd = argv_for(
                op, chip=args.chip, port=args.port, file=file, last=n == len(flash.ops) - 1
            )
            print("$ " + shlex.join(cmd), flush=True)
            if code := run(cmd):
                return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
