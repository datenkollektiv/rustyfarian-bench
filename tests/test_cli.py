from __future__ import annotations

from pathlib import Path

import pytest

from rustyfarian_bench import cli, config, runner
from rustyfarian_bench.config import BenchConfig


@pytest.mark.parametrize(
    ("outcome", "code"), [("pass", 0), ("fail", 1), ("error", 1), ("unverified", 3)]
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
