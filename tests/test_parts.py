"""`boards.load_parts`: the `parts.toml` the bench names its boards from."""

from __future__ import annotations

from pathlib import Path

import pytest

from rustyfarian_bench import boards
from rustyfarian_bench.config import ConfigError

GOOD = """\
[[parts]]
name = "bench-wroom32-01"
mac = "02:00:00:12:00:B8"
barcode = "A-0001"

[[parts]]
name = "bench-s3-02"
mac = "aa:bb:cc:dd:ee:02"
barcode = "A-0002"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "parts.toml"
    path.write_text(text, encoding="utf-8")
    return path


def entry(name: str = "bench-a", mac: str = "aa:bb:cc:dd:ee:01") -> str:
    return f'[[parts]]\nname = "{name}"\nmac = "{mac}"\n'


def test_a_valid_file_is_keyed_by_the_lowercase_mac(tmp_path: Path) -> None:
    parts = boards.load_parts(write(tmp_path, GOOD))

    assert set(parts) == {"02:00:00:12:00:b8", "aa:bb:cc:dd:ee:02"}
    assert parts["02:00:00:12:00:b8"] == boards.Part("bench-wroom32-01", "02:00:00:12:00:b8")


def test_the_barcode_is_not_kept(tmp_path: Path) -> None:
    [part, _] = boards.load_parts(write(tmp_path, GOOD)).values()

    assert not hasattr(part, "barcode")


def test_other_keys_are_ignored(tmp_path: Path) -> None:
    parts = boards.load_parts(write(tmp_path, entry() + 'shelf = "3"\n'))

    assert list(parts) == ["aa:bb:cc:dd:ee:01"]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "at least one"),
        ("title = 'x'\n", "at least one"),
        ("parts = []\n", "at least one"),
        ("parts = [1]\n", "expected a table"),
        ('[[parts]]\nmac = "aa:bb:cc:dd:ee:01"\n', "name"),
        ('[[parts]]\nname = "a"\n', "mac"),
        (entry(mac="aa:bb:cc:dd:ee"), "mac"),
        (entry(mac="aabbccddeeff"), "mac"),
        (entry(mac="gg:bb:cc:dd:ee:01"), "mac"),
        (entry(name="two words"), "name"),
        (entry(name="-lead"), "name"),
        (entry(name="x" * 81), "name"),
        (entry() + entry(name="bench-b"), "listed twice"),
        (entry() + entry("bench-b", "AA:BB:CC:DD:EE:01"), "listed twice"),
        ("[[parts", "not valid TOML"),
    ],
    ids=[
        "empty",
        "no-parts",
        "empty-list",
        "not-a-table",
        "no-name",
        "no-mac",
        "short-mac",
        "no-colons",
        "not-hex",
        "space-in-name",
        "leading-dash",
        "name-too-long",
        "duplicate-mac",
        "duplicate-mac-other-case",
        "malformed",
    ],
)
def test_a_bad_file_is_a_config_error(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        boards.load_parts(write(tmp_path, text))


def test_a_duplicate_name_is_refused(tmp_path: Path) -> None:
    text = entry() + entry(mac="aa:bb:cc:dd:ee:02")

    with pytest.raises(ConfigError, match="name 'bench-a' is listed twice"):
        boards.load_parts(write(tmp_path, text))


def test_names_differing_in_case_are_different_names(tmp_path: Path) -> None:
    text = entry("Bench-a") + entry("bench-a", "aa:bb:cc:dd:ee:02")

    assert len(boards.load_parts(write(tmp_path, text))) == 2


def test_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        boards.load_parts(tmp_path / "absent.toml")


def test_a_file_over_the_size_cap_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    text = entry() + "#" * boards.MAX_PARTS_FILE + "\n"
    with pytest.raises(ConfigError, match="more than"):
        boards.load_parts(write(tmp_path, text))


def test_a_file_nested_too_deeply_is_a_config_error_not_a_crash(tmp_path: Path) -> None:
    text = entry() + "deep = " + "[" * 100_000 + "]" * 100_000 + "\n"
    with pytest.raises(ConfigError):
        boards.load_parts(write(tmp_path, text))


def test_a_file_that_is_not_utf8_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "parts.toml"
    path.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(ConfigError, match="cannot be read"):
        boards.load_parts(path)


def test_an_error_names_the_entry_and_field_but_not_the_value(tmp_path: Path) -> None:
    text = entry() + entry("bench-b", "SECRET-NOT-A-MAC")

    with pytest.raises(ConfigError) as err:
        boards.load_parts(write(tmp_path, text))

    assert "parts #2" in str(err.value)
    assert "mac" in str(err.value)
    assert "SECRET-NOT-A-MAC" not in str(err.value)
