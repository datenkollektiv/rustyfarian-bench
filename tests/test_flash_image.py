"""The merged-image flash plan: what is written, what is erased, and what is never touched."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path

import pytest

from rustyfarian_bench import flash_image
from rustyfarian_bench.config import ConfigError
from rustyfarian_bench.flash_image import FlashPlan, Op

from .fakes import (
    FACTORY,
    NVS,
    NVS_CLASSIC,
    OTA_0,
    OTA_1,
    OTADATA,
    PHY_INIT,
    PartSpec,
    make_image,
    table_bytes,
    table_entry,
)

C3_PARTS = (NVS, PHY_INIT, FACTORY)
PREFIX = "image: partition table at 0x8000: "


def ops(plan: FlashPlan) -> list[tuple[str, int, int]]:
    return [(o.kind, o.offset, o.size) for o in plan.ops]


# -- parsing ------------------------------------------------------------------------------------


def test_an_image_without_a_table_has_none() -> None:
    assert flash_image.parse_table(b"\xe9merged-image\x00\xff") is None
    assert flash_image.parse_table(b"\xff" * 0x8020) is None


def test_an_image_too_short_for_a_table_has_none() -> None:
    assert flash_image.parse_table(b"\x00" * 0x8000 + b"\xaa\x50" + b"\x00" * 0x1D) is None


def test_the_table_is_read_with_labels_stripped_of_nul() -> None:
    parts = flash_image.parse_table(make_image(C3_PARTS))

    assert parts is not None
    assert [(p.type, p.subtype, p.offset, p.size, p.label) for p in parts] == [
        (1, 2, 0x9000, 0x6000, "nvs"),
        (1, 1, 0xF000, 0x1000, "phy_init"),
        (0, 0, 0x10000, 0x3F0000, "factory"),
    ]


def test_a_table_without_an_md5_entry_is_accepted() -> None:
    parts = flash_image.parse_table(make_image(C3_PARTS, md5=False))

    assert parts is not None
    assert len(parts) == 3


def _corrupt_md5() -> bytes:
    raw = bytearray(table_bytes(C3_PARTS))
    raw[3 * 32 + 16] ^= 0xFF
    return bytes(raw)


def _unknown_magic() -> bytes:
    good = table_bytes(C3_PARTS)
    return good[:32] + b"\x12\x34" + good[34:]


def _no_terminator() -> bytes:
    return b"".join(table_entry((1, 2, 0x9000 + 0x1000 * i, 0x1000, "d")) for i in range(0x60))


def _md5_without_terminator() -> bytes:
    entries = table_entry(FACTORY)
    return entries + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(entries).digest() + b"\x00" * 32


def _one(spec: PartSpec) -> bytes:
    return table_bytes([spec])


BAD_TABLES = {
    "unknown-magic": _unknown_magic(),
    "no-terminator": _no_terminator(),
    "md5-mismatch": _corrupt_md5(),
    "md5-not-followed-by-terminator": _md5_without_terminator(),
    "offset-in-bootloader-area": _one((1, 2, 0x8000, 0x1000, "nvs")),
    "offset-unaligned": _one((1, 2, 0x9800, 0x1000, "nvs")),
    "app-offset-unaligned": _one((0, 0, 0x11000, 0x10000, "factory")),
    "size-zero": _one((1, 2, 0x9000, 0, "nvs")),
    "app-size-unaligned": _one((0, 0, 0x10000, 0x10800, "factory")),
    "otadata-size-unaligned": _one((1, 0, 0xD000, 0x800, "otadata")),
    "beyond-4gib": _one((1, 2, 0xFFFFF000, 0x2000, "nvs")),
    "overlap": table_bytes([NVS, (1, 1, 0xA000, 0x1000, "phy_init")]),
    "non-ascii-label": _one((1, 2, 0x9000, 0x1000, "nüs")),
    "unprintable-label": _one((1, 2, 0x9000, 0x1000, "n\x01s")),
}


@pytest.mark.parametrize("raw", BAD_TABLES.values(), ids=BAD_TABLES.keys())
def test_a_malformed_table_is_refused_with_its_location(raw: bytes) -> None:
    image = make_image([], table=raw)

    with pytest.raises(ConfigError) as err:
        flash_image.parse_table(image)

    assert str(err.value).startswith(PREFIX)


def test_a_partition_ending_exactly_at_four_gib_is_valid() -> None:
    image = make_image([(1, 2, 0xFFFFF000, 0x1000, "nvs")])

    parts = flash_image.parse_table(image)

    assert parts is not None
    assert parts[0].offset + parts[0].size == 2**32


# -- planning -----------------------------------------------------------------------------------


def test_an_image_without_a_table_is_one_flat_write_at_zero() -> None:
    image = b"\xe9merged-image\x00\xff"

    plan = flash_image.plan(image)

    assert plan == FlashPlan("flat", (Op("write", 0, len(image), "image"),))


def test_the_real_esp_hal_c3_layout_rewrites_bootloader_and_app_but_not_nvs() -> None:
    app = b"\xe9" + b"A" * 0x90200 + b"\x01\x02\x03\x04\x05\x06\x07\x08" + b"\x00" * 0x77
    assert 0x10000 + len(app) == 0xA0280
    image = make_image(C3_PARTS, contents={0x10000: app})

    plan = flash_image.plan(image)

    assert plan.mode == "segments"
    assert ops(plan) == [("write", 0x0, 0x9000), ("write", 0x10000, 0x91000)]
    assert [o.label for o in plan.ops] == ["bootloader+table", "factory"]


def test_an_esp32_classic_layout_erases_otadata_and_writes_only_the_used_ota_slot() -> None:
    parts = (NVS_CLASSIC, OTADATA, PHY_INIT, OTA_0, OTA_1)
    app = b"\xe9" + b"A" * 0x2EFE + b"\x07"  # ends mid-sector, clear of the 64-byte tail
    image = make_image(parts, contents={0x1000: b"\xe9boot", 0x10000: app})

    plan = flash_image.plan(image)

    assert ops(plan) == [
        ("write", 0x0, 0x9000),
        ("erase", 0xD000, 0x2000),
        ("write", 0x10000, 0x3000),
    ]
    assert [o.label for o in plan.ops] == ["bootloader+table", "otadata", "ota_0"]
    assert all(o.label not in ("nvs", "phy_init", "ota_1") for o in plan.ops)


def test_an_app_that_is_all_ff_is_skipped() -> None:
    image = make_image([FACTORY], length=0x10000 + 0x2000)

    assert ops(flash_image.plan(image)) == [("write", 0x0, 0x9000)]


def test_an_app_whose_data_ends_mid_sector_is_rounded_up_to_the_sector() -> None:
    image = make_image([FACTORY], contents={0x10000: b"\xe9" + b"A" * 0x1000})  # 0x1001 bytes

    assert ops(flash_image.plan(image)) == [("write", 0x0, 0x9000), ("write", 0x10000, 0x2000)]


def test_trailing_ff_bytes_of_an_app_do_not_count() -> None:
    image = make_image([FACTORY], contents={0x10000: b"\xe9AB" + b"\xff" * 0x3000 + b"C"})
    assert ops(flash_image.plan(image))[-1] == ("write", 0x10000, 0x4000)

    image = make_image([FACTORY], contents={0x10000: b"\xe9AB" + b"\xff" * 0x3000})
    assert ops(flash_image.plan(image))[-1] == ("write", 0x10000, 0x1000)


def test_an_app_ending_just_below_a_sector_keeps_the_next_sector_for_a_0xff_digest() -> None:
    # The last non-0xFF byte sits 16 bytes below 0x11000; the digest may run on in 0xFF.
    image = make_image([FACTORY], contents={0x10000: b"\xe9" + b"A" * (0x1000 - 17)})

    assert ops(flash_image.plan(image))[-1] == ("write", 0x10000, 0x2000)


def test_typed_bootloader_and_table_entries_below_the_table_are_allowed_and_not_planned() -> None:
    bootloader: PartSpec = (2, 0, 0x0, 0x8000, "bootloader")
    table: PartSpec = (3, 0, 0x8000, 0x1000, "partition_table")
    image = make_image([bootloader, table, FACTORY], contents={0x10000: b"\xe9app"})

    assert ops(flash_image.plan(image)) == [("write", 0x0, 0x9000), ("write", 0x10000, 0x1000)]


def test_an_app_write_never_exceeds_its_partition() -> None:
    small: PartSpec = (0, 0, 0x10000, 0x10000, "factory")
    image = make_image([small], contents={0x10000: b"\xe9" + b"A" * 0x10000})

    assert ops(flash_image.plan(image))[-1] == ("write", 0x10000, 0x10000)


def test_an_image_ending_inside_an_app_partition_is_planned_from_what_is_there() -> None:
    image = make_image([FACTORY], contents={0x10000: b"\xe9abc"})
    assert len(image) < 0x10000 + 0x3F0000

    assert ops(flash_image.plan(image)) == [("write", 0x0, 0x9000), ("write", 0x10000, 0x1000)]


def test_only_otadata_among_data_partitions_is_erased() -> None:
    other: PartSpec = (1, 0x81, 0x20000, 0x1000, "fat")
    image = make_image([NVS_CLASSIC, OTADATA, PHY_INIT, other])

    assert ops(flash_image.plan(image)) == [("write", 0x0, 0x9000), ("erase", 0xD000, 0x2000)]


def test_ops_are_ordered_by_offset_whatever_the_table_order() -> None:
    image = make_image((OTA_0, OTADATA, NVS_CLASSIC), contents={0x10000: b"\xe9app"})

    offsets = [o.offset for o in flash_image.plan(image).ops]

    assert offsets == [0x0, 0xD000, 0x10000]


def test_planning_leaves_the_image_alone_and_is_repeatable() -> None:
    image = make_image(C3_PARTS, contents={0x10000: b"\xe9app"})
    before = bytes(image)

    assert flash_image.plan(image) == flash_image.plan(image)
    assert image == before


def test_a_malformed_table_fails_the_plan() -> None:
    with pytest.raises(ConfigError, match="partition table"):
        flash_image.plan(make_image([], table=BAD_TABLES["overlap"]))


# -- slices and the reported plan ---------------------------------------------------------------


def test_a_segment_is_the_image_slice_padded_with_ff() -> None:
    image = b"0123456789"

    assert flash_image.segment(image, Op("write", 2, 4, "x")) == b"2345"
    assert flash_image.segment(image, Op("write", 8, 5, "x")) == b"89\xff\xff\xff"
    assert flash_image.segment(image, Op("write", 20, 3, "x")) == b"\xff\xff\xff"


def test_describe_reports_hex_offsets_and_sizes() -> None:
    plan = FlashPlan(
        "segments",
        (Op("write", 0, 0x9000, "bootloader+table"), Op("erase", 0xD000, 0x2000, "otadata")),
    )

    assert flash_image.describe(plan) == {
        "mode": "segments",
        "ops": [
            {"kind": "write", "offset": "0x0", "size": "0x9000", "label": "bootloader+table"},
            {"kind": "erase", "offset": "0xd000", "size": "0x2000", "label": "otadata"},
        ],
    }


# -- espflash argv ------------------------------------------------------------------------------


def test_a_flat_write_is_todays_write_bin_argv() -> None:
    op = Op("write", 0, 10, "image")

    argv = flash_image.argv_for(op, chip="esp32c6", port="/dev/x", file="image.bin", last=True)

    assert argv == [
        "espflash",
        "write-bin",
        "--non-interactive",
        "--chip",
        "esp32c6",
        "--port",
        "/dev/x",
        "0x0",
        "image.bin",
    ]


def test_only_the_last_op_lets_the_chip_reset() -> None:
    op = Op("write", 0x10000, 0x1000, "factory")

    early = flash_image.argv_for(op, chip="esp32", port="/p", file="f.bin", last=False)
    last = flash_image.argv_for(op, chip="esp32", port="/p", file="f.bin", last=True)

    assert early[7:9] == ["--after", "no-reset"]
    assert early[9:] == ["0x10000", "f.bin"]
    assert "--after" not in last


def test_an_erase_names_the_region_and_no_file() -> None:
    op = Op("erase", 0xD000, 0x2000, "otadata")

    argv = flash_image.argv_for(op, chip="esp32", port="/p", file=None, last=False)

    assert argv == [
        "espflash",
        "erase-region",
        "--non-interactive",
        "--chip",
        "esp32",
        "--port",
        "/p",
        "--after",
        "no-reset",
        "0xd000",
        "0x2000",
    ]


# -- main ---------------------------------------------------------------------------------------


class Recorder:
    """A fake espflash: records argv and what each written file held while it was running."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.files: list[bytes | None] = []
        self.fail_at = fail_at

    def __call__(self, argv: list[str]) -> int:
        self.argvs.append(argv)
        self.files.append(Path(argv[-1]).read_bytes() if argv[1] == "write-bin" else None)
        return 5 if self.fail_at == len(self.argvs) else 0


def write_image(directory: Path, image: bytes) -> Path:
    path = directory / "image.bin"
    path.write_bytes(image)
    return path


def leftovers(directory: Path) -> list[Path]:
    return list(directory.glob(".flash-*"))


def test_a_flat_image_is_written_from_the_given_path_as_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_image(tmp_path, b"\xe9flat")
    rec = Recorder()

    code = flash_image.main(["--chip", "esp32c6", "--port", "/dev/x", str(path)], run=rec)

    assert code == 0
    assert rec.argvs == [
        [
            "espflash",
            "write-bin",
            "--non-interactive",
            "--chip",
            "esp32c6",
            "--port",
            "/dev/x",
            "0x0",
            str(path),
        ]
    ]
    assert capsys.readouterr().out == "$ " + shlex.join(rec.argvs[0]) + "\n"


def test_a_flat_write_makes_no_slice_directory(tmp_path: Path) -> None:
    path = write_image(tmp_path, b"\xe9flat")
    made: list[Path] = []

    def run(argv: list[str]) -> int:
        made.extend(leftovers(tmp_path))
        return 0

    assert flash_image.main(["--chip", "esp32c3", "--port", "/dev/x", str(path)], run=run) == 0
    assert made == []


def test_a_segmented_image_runs_each_op_in_order_with_its_slice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = b"\xe9" + b"A" * 0x1EFE + b"\x07"  # ends mid-sector, clear of the 64-byte tail
    image = make_image((NVS_CLASSIC, OTADATA, PHY_INIT, OTA_0, OTA_1), contents={0x10000: app})
    path = write_image(tmp_path, image)
    rec = Recorder()

    code = flash_image.main(["--chip", "esp32", "--port", "/dev/x", str(path)], run=rec)

    assert code == 0
    assert [a[1] for a in rec.argvs] == ["write-bin", "erase-region", "write-bin"]
    assert ["--after" in a for a in rec.argvs] == [True, True, False]
    assert rec.argvs[1][-2:] == ["0xd000", "0x2000"]
    # the image ends with the app, mid-sector; the slice is padded with 0xFF to the sector
    assert rec.files == [image[:0x9000], None, image[0x10000:0x12000].ljust(0x2000, b"\xff")]
    writes = [Path(a[-1]).name for a in rec.argvs if a[1] == "write-bin"]
    assert writes == ["0x0.bin", "0x10000.bin"]
    assert capsys.readouterr().out.splitlines() == ["$ " + shlex.join(a) for a in rec.argvs]


def test_the_slices_live_beside_the_image_and_are_gone_afterwards(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    path = write_image(job, make_image(C3_PARTS, contents={0x10000: b"\xe9app"}))
    where: list[tuple[Path, str]] = []

    def run(argv: list[str]) -> int:
        where.append((Path(argv[-1]).parent.parent, Path(argv[-1]).parent.name))
        return 0

    assert flash_image.main(["--chip", "esp32c3", "--port", "/p", str(path)], run=run) == 0

    assert len(where) == 2
    assert all(parent == job and name.startswith(".flash-") for parent, name in where)
    assert leftovers(job) == []


def test_the_first_failing_op_stops_the_run_and_its_code_is_returned(tmp_path: Path) -> None:
    image = make_image((NVS_CLASSIC, OTADATA, OTA_0), contents={0x10000: b"\xe9app"})
    path = write_image(tmp_path, image)
    rec = Recorder(fail_at=2)

    code = flash_image.main(["--chip", "esp32", "--port", "/p", str(path)], run=rec)

    assert code == 5
    assert len(rec.argvs) == 2
    assert leftovers(tmp_path) == []


def test_a_missing_image_is_2_on_stderr_and_nothing_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = Recorder()

    code = flash_image.main(["--chip", "x", "--port", "/p", str(tmp_path / "nope.bin")], run=rec)

    assert code == 2
    assert rec.argvs == []
    captured = capsys.readouterr()
    assert captured.err != ""
    assert captured.out == ""


def test_a_malformed_table_is_2_on_stderr_and_nothing_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_image(tmp_path, make_image([], table=BAD_TABLES["overlap"]))
    rec = Recorder()

    code = flash_image.main(["--chip", "esp32", "--port", "/p", str(path)], run=rec)

    assert code == 2
    assert rec.argvs == []
    assert "partition table at 0x8000" in capsys.readouterr().err
    assert leftovers(tmp_path) == []


def test_a_missing_espflash_is_127_with_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_image(tmp_path, b"\xe9flat")

    def gone(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("espflash")

    monkeypatch.setattr(subprocess, "run", gone)

    code = flash_image.main(["--chip", "esp32", "--port", "/p", str(path)])

    assert code == 127
    assert "espflash" in capsys.readouterr().err
