from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from rustyfarian_bench import cli, config, device, runner
from rustyfarian_bench.config import BenchConfig


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("pass", 0), ("fail", 1), ("error", 1), ("unverified", 3), ("incomplete", 3)],
)
def test_run_exit_code_follows_the_session_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, code: int
) -> None:
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {}, ())
    monkeypatch.setattr(config, "load", lambda repo: cfg)
    monkeypatch.setattr(BenchConfig, "find", lambda self, target: ())
    monkeypatch.setattr(runner.Session, "run", lambda self, scenarios, first_example: outcome)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", "-y"]) == code


def test_run_exit_code_is_2_for_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(repo: Path) -> BenchConfig:
        raise config.ConfigError("broken")

    monkeypatch.setattr(config, "load", bad)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", "-y"]) == 2


def test_list_prints_the_smoke_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    # `just smoke-install` runs `list` against this fixture; keep the two in step.
    repo = Path(__file__).parent / "fixtures" / "consumer"

    assert cli.main(["--repo", str(repo), "list"]) == 0
    assert "s3/smoke" in capsys.readouterr().out


def test_detect_without_espflash_warns_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(port: str) -> NoReturn:
        raise device.MissingTool("cannot run 'espflash'; install it with `cargo install espflash`")

    monkeypatch.setattr(device, "candidate_ports", lambda: ["/dev/cu.usbmodem1"])
    monkeypatch.setattr(device, "probe", missing)

    assert cli.main(["detect"]) == 1
    assert "cargo install espflash" in capsys.readouterr().out
