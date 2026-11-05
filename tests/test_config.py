from __future__ import annotations

from pathlib import Path

import pytest

from rustyfarian_bench.config import ConfigError, load

BENCH = """
[commands]
build = ["just", "build-example", "{example}"]
flash = ["just", "flash", "{example}", "{port}"]

[boards.s3]
chip = "esp32s3"
name = "CrowPanel"
"""

B3F = """
examples = ["hal_s3_b3f", "idf_s3_b3f"]

[[step]]
say = "banner"
expect = "ready"

[[step]]
say = "rotate"
duration = 5
reject = "pressed"
"""


def write(repo: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = repo / "bench" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def test_load_and_find(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, {"bench.toml": BENCH, "s3/b3f.toml": B3F}))
    assert cfg.build == ("just", "build-example", "{example}")
    (s,) = cfg.find("idf_s3_b3f")
    assert s.name == "b3f"
    assert s.board.chip == "esp32s3"
    assert s.steps[0].expect == ("ready",)
    assert cfg.find("s3/b3f") == (s,)
    assert cfg.find("s3") == (s,)


def test_unknown_target(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, {"bench.toml": BENCH, "s3/b3f.toml": B3F}))
    with pytest.raises(ConfigError):
        cfg.find("hal_c3_b3f")


def test_step_needs_exactly_one_kind(tmp_path: Path) -> None:
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpect = "a"\nduration = 1\n'
    with pytest.raises(ConfigError, match="exactly one"):
        load(write(tmp_path, {"bench.toml": BENCH, "s3/bad.toml": bad}))


def test_unknown_step_key(tmp_path: Path) -> None:
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpcet = "a"\n'
    with pytest.raises(ConfigError, match="unknown keys"):
        load(write(tmp_path, {"bench.toml": BENCH, "s3/bad.toml": bad}))
