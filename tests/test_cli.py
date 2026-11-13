from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import NoReturn

import pytest

import rustyfarian_bench
from rustyfarian_bench import cli, client, config, device, runner
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


def _capture_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[runner.Options]:
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {}, ())
    seen: list[runner.Options] = []
    real_init = runner.Session.__init__

    def spy(self: runner.Session, c: BenchConfig, opts: runner.Options) -> None:
        seen.append(opts)
        real_init(self, c, opts)

    monkeypatch.setattr(config, "load", lambda repo: cfg)
    monkeypatch.setattr(BenchConfig, "find", lambda self, target: ())
    monkeypatch.setattr(runner.Session, "__init__", spy)
    monkeypatch.setattr(runner.Session, "run", lambda self, scenarios, first_example: "pass")
    return seen


@pytest.mark.parametrize(
    ("flags", "mode", "assume_yes"),
    [
        ([], None, False),
        (["-y"], None, True),
        (["--unattended"], "unattended", True),
        (["--attended"], "attended", False),
        (["--attended", "-y"], "attended", True),
    ],
)
def test_run_mode_flags_reach_the_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    mode: str | None,
    assume_yes: bool,
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", *flags]) == 0

    (opts,) = seen
    assert opts.mode == mode
    assert opts.assume_yes is assume_yes


def test_unattended_and_attended_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--repo", str(tmp_path), "run", "s3", "--unattended", "--attended"])

    assert exc.value.code == 2
    assert seen == []


@pytest.mark.parametrize(("flags", "twins"), [([], True), (["--no-twins"], False)])
def test_no_twins_reaches_the_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], twins: bool
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", *flags]) == 0

    (opts,) = seen
    assert opts.twins is twins


def _no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `from . import server` import afresh, and never start a real server if it works."""

    def never(*a: object, **k: object) -> NoReturn:
        raise AssertionError("uvicorn must not start in a unit test")

    monkeypatch.setattr("uvicorn.run", never)
    monkeypatch.delitem(sys.modules, "rustyfarian_bench.server", raising=False)
    # `from . import server` takes the package attribute first, if an earlier test set it
    monkeypatch.delattr(rustyfarian_bench, "server", raising=False)


def test_serve_without_the_extra_hints_at_it_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_server(monkeypatch)
    # A None entry makes `import` raise ModuleNotFoundError, as if the extra were absent.
    for name in ("fastapi", "uvicorn"):
        monkeypatch.setitem(sys.modules, name, None)

    assert cli.main(["serve"]) == 2
    assert "rustyfarian-bench[serve]" in capsys.readouterr().out


def test_serve_does_not_hide_a_missing_module_of_our_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the serve extra's modules turn into the hint; any other import error is a bug.
    _no_server(monkeypatch)
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.server", None)

    with pytest.raises(ModuleNotFoundError):
        cli.main(["serve"])


def test_submit_maps_a_missing_tool_to_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(args: object) -> NoReturn:
        raise device.MissingTool("cannot run 'just'")

    monkeypatch.setattr(client, "cmd_submit", missing)

    argv = ["--repo", str(tmp_path), "submit", "s3", "ex", "--bench", "http://x"]
    assert cli.main(argv) == 2
    assert "cannot run 'just'" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flags", "expected"), [([], None), (["--detect-timeout", "12"], 12.0)], ids=["absent", "12"]
)
def test_detect_timeout_reaches_the_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: float | None
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", *flags]) == 0

    (opts,) = seen
    assert opts.detect_timeout == expected


@pytest.mark.parametrize(
    ("flags", "expected"), [([], False), (["--unique-board"], True)], ids=["default", "set"]
)
def test_unique_board_reaches_the_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: bool
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    assert cli.main(["--repo", str(tmp_path), "run", "s3", *flags]) == 0

    (opts,) = seen
    assert opts.unique_board is expected


@pytest.mark.parametrize("bad", ["0", "-1", "soon", "nan"])
def test_run_rejects_a_detect_timeout_that_is_not_a_positive_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    seen = _capture_options(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--repo", str(tmp_path), "run", "s3", "--detect-timeout", bad])

    assert exc.value.code == 2
    assert seen == []


@pytest.mark.parametrize(
    ("flags", "expected"), [([], 30.0), (["--detect-timeout", "5"], 5.0)], ids=["default", "5"]
)
def test_serve_passes_the_detect_timeout_to_the_server(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: float
) -> None:
    _no_server(monkeypatch)
    calls: list[tuple[object, ...]] = []
    fake = types.ModuleType("rustyfarian_bench.server")
    fake.serve = lambda *a: calls.append(a)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.server", fake)

    assert cli.main(["serve", *flags]) == 0

    [(host, port, outbox, _job_timeout, detect_timeout)] = calls
    assert (host, port, outbox) == ("127.0.0.1", 8000, Path("outbox"))
    assert detect_timeout == expected
