from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from typing import NoReturn

import pytest

import rustyfarian_bench
from rustyfarian_bench import cli, client, config, device, runner
from rustyfarian_bench.config import BenchConfig


@pytest.fixture(autouse=True)
def no_bench_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own shell must not decide whether `--bench` is required."""
    monkeypatch.delenv(client.URL_ENV, raising=False)


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
    fake.serve = lambda *a, **kw: calls.append(a)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.server", fake)

    assert cli.main(["serve", *flags]) == 0

    [(host, port, outbox, _job_timeout, detect_timeout)] = calls
    assert (host, port, outbox) == ("127.0.0.1", 8000, Path("outbox"))
    assert detect_timeout == expected


def _fake_serve(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    _no_server(monkeypatch)
    calls: list[dict[str, object]] = []
    fake = types.ModuleType("rustyfarian_bench.server")
    fake.serve = lambda *a, **kw: calls.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.server", fake)
    return calls


def test_serve_passes_the_token_file_and_the_tls_files_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_serve(monkeypatch)
    flags = ["--host", "0.0.0.0", "--token-file", "t.toml"]
    flags += ["--ssl-certfile", "c.pem", "--ssl-keyfile", "k.pem"]

    assert cli.main(["serve", *flags]) == 0

    [kw] = calls
    assert kw == {
        "token_file": Path("t.toml"),
        "ssl_certfile": Path("c.pem"),
        "ssl_keyfile": Path("k.pem"),
    }


def test_serve_without_the_flags_passes_none_for_each(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_serve(monkeypatch)

    assert cli.main(["serve"]) == 0

    [kw] = calls
    assert kw == {"token_file": None, "ssl_certfile": None, "ssl_keyfile": None}


def test_serve_refusing_to_start_is_exit_2_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_server(monkeypatch)
    fake = types.ModuleType("rustyfarian_bench.server")

    def refuse(*a: object, **kw: object) -> NoReturn:
        raise config.ConfigError("serving on 0.0.0.0 needs --token-file")

    fake.serve = refuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.server", fake)

    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "--token-file" in capsys.readouterr().out


def _capture_submit(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    seen: list[argparse.Namespace] = []

    def grab(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(client, "cmd_submit", grab)
    return seen


def test_submit_takes_the_bench_url_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_submit(monkeypatch)
    monkeypatch.setenv("RUSTYFARIAN_BENCH_URL", "https://bench.example.net:8443")

    assert cli.main(["--repo", str(tmp_path), "submit", "s3", "ex"]) == 0

    [args] = seen
    assert args.bench == "https://bench.example.net:8443"


def test_a_bench_flag_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_submit(monkeypatch)
    monkeypatch.setenv("RUSTYFARIAN_BENCH_URL", "https://bench.example.net:8443")

    argv = ["--repo", str(tmp_path), "submit", "s3", "ex", "--bench", "http://127.0.0.1:8000"]
    assert cli.main(argv) == 0

    [args] = seen
    assert args.bench == "http://127.0.0.1:8000"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_submit_without_a_bench_url_anywhere_names_both_ways(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    seen = _capture_submit(monkeypatch)
    if value is None:
        monkeypatch.delenv("RUSTYFARIAN_BENCH_URL", raising=False)
    else:
        monkeypatch.setenv("RUSTYFARIAN_BENCH_URL", value)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--repo", str(tmp_path), "submit", "s3", "ex"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--bench" in err
    assert "RUSTYFARIAN_BENCH_URL" in err
    assert seen == []


def test_serve_passes_a_status_file_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_serve(monkeypatch)

    assert cli.main(["serve", "--status-file", "run/status.json"]) == 0

    [kw] = calls
    assert kw["status_file"] == Path("run/status.json")


# -- kiosk -----------------------------------------------------------------------------------


def _no_kiosk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `from . import kiosk` import afresh: drop the module and the package attribute."""
    monkeypatch.delitem(sys.modules, "rustyfarian_bench.kiosk", raising=False)
    monkeypatch.delattr(rustyfarian_bench, "kiosk", raising=False)


_Call = tuple[tuple[object, ...], dict[str, object]]


def _fake_kiosk(monkeypatch: pytest.MonkeyPatch) -> list[_Call]:
    _no_kiosk(monkeypatch)
    calls: list[_Call] = []

    def run(*a: object, **kw: object) -> int:
        calls.append((a, kw))
        return 7

    fake = types.ModuleType("rustyfarian_bench.kiosk")
    fake.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.kiosk", fake)
    return calls


def test_kiosk_flags_reach_run_and_its_exit_code_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_kiosk(monkeypatch)
    _fake_touch_reader(monkeypatch)
    flags = ["--status-file", "s.json", "--title", "T", "--address", "bench.local:8000"]
    flags += ["--fb-match", "fb9", "--once", "--png-dir", "pngs"]

    assert cli.main(["kiosk", *flags]) == 7

    [((path,), kw)] = calls
    assert path == Path("s.json")
    assert (kw["title"], kw["address"], kw["fb_match"]) == ("T", "bench.local:8000", "fb9")
    assert (kw["once"], kw["png_dir"]) == (True, Path("pngs"))
    assert kw["version"] == rustyfarian_bench.__version__
    assert callable(kw["wait"])


def test_kiosk_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KIOSK_ENV:
        monkeypatch.delenv(var, raising=False)
    calls = _fake_kiosk(monkeypatch)
    readers = _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk"]) == 7

    [((path,), kw)] = calls
    assert path == Path("/run/rustyfarian-bench/status.json")
    assert (kw["once"], kw["png_dir"]) == (False, None)
    assert kw["sleep_after"] == 900
    assert kw["touch"] is readers[0]
    assert readers[0].args == (Path("/sys/class/input"), Path("/dev"), "ADS7846")


KIOSK_ENV = (
    "BENCH_TITLE",
    "BENCH_ADDRESS",
    "BENCH_FB_MATCH",
    "BENCH_SLEEP_AFTER",
    "BENCH_TOUCH_MATCH",
    "BENCH_SYSFS_INPUT",
    "BENCH_DEV_DIR",
)


class _FakeReader:
    def __init__(self, *args: object) -> None:
        self.args = args


def _fake_touch_reader(monkeypatch: pytest.MonkeyPatch) -> list[_FakeReader]:
    """Replace `TouchReader` so no test reads the machine's own /sys/class/input."""
    from rustyfarian_bench import kiosk_touch

    readers: list[_FakeReader] = []

    def build(*args: object) -> _FakeReader:
        readers.append(_FakeReader(*args))
        return readers[-1]

    monkeypatch.setattr(kiosk_touch, "TouchReader", build)
    return readers


def test_kiosk_sleep_after_reaches_run_with_a_touch_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BENCH_SLEEP_AFTER", raising=False)
    calls = _fake_kiosk(monkeypatch)
    readers = _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk", "--sleep-after", "30"]) == 7

    [(_, kw)] = calls
    assert kw["sleep_after"] == 30
    assert len(readers) == 1
    assert kw["touch"] is readers[0]


def test_kiosk_sleep_after_zero_is_accepted_and_builds_no_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_kiosk(monkeypatch)
    readers = _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk", "--sleep-after", "0"]) == 7

    [(_, kw)] = calls
    assert kw["sleep_after"] == 0
    assert kw["touch"] is None
    assert readers == []


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", ""])
def test_kiosk_sleep_after_rejects_bad_values_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = _fake_kiosk(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["kiosk", "--sleep-after", value])

    assert exit_info.value.code == 2
    assert calls == []


def test_kiosk_environment_sets_the_sleep_and_touch_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BENCH_SLEEP_AFTER", "120")
    monkeypatch.setenv("BENCH_TOUCH_MATCH", "Goodix")
    monkeypatch.setenv("BENCH_SYSFS_INPUT", str(tmp_path / "input"))
    monkeypatch.setenv("BENCH_DEV_DIR", str(tmp_path / "dev"))
    calls = _fake_kiosk(monkeypatch)
    readers = _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk"]) == 7

    [(_, kw)] = calls
    assert kw["sleep_after"] == 120
    assert readers[0].args == (tmp_path / "input", tmp_path / "dev", "Goodix")


def test_kiosk_flag_beats_the_sleep_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_SLEEP_AFTER", "120")
    calls = _fake_kiosk(monkeypatch)
    _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk", "--sleep-after", "0"]) == 7

    assert calls[0][1]["sleep_after"] == 0


def test_kiosk_restores_the_sigterm_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    calls = _fake_kiosk(monkeypatch)
    _fake_touch_reader(monkeypatch)
    before = signal.getsignal(signal.SIGTERM)

    cli.main(["kiosk"])

    assert calls
    assert signal.getsignal(signal.SIGTERM) is before


def test_kiosk_without_pillow_hints_at_the_extra_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_kiosk(monkeypatch)
    monkeypatch.setitem(sys.modules, "PIL", None)  # as if the extra were absent

    assert cli.main(["kiosk"]) == 2
    assert "rustyfarian-bench[kiosk]" in capsys.readouterr().out


def test_kiosk_does_not_hide_a_missing_module_of_our_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_kiosk(monkeypatch)
    monkeypatch.setitem(sys.modules, "rustyfarian_bench.kiosk", None)

    with pytest.raises(ModuleNotFoundError):
        cli.main(["kiosk"])


def test_kiosk_once_builds_no_touch_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCH_SLEEP_AFTER", raising=False)
    calls = _fake_kiosk(monkeypatch)
    readers = _fake_touch_reader(monkeypatch)

    assert cli.main(["kiosk", "--once"]) == 7

    [(_, kw)] = calls
    assert kw["touch"] is None
    assert readers == []
