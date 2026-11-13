"""The remote bench's shared core: refusal boundary, job layout, child-process runner, worker.

No board and no network: a job's child process is `python -c`, and the one integration test
runs a real Session against fakes. Also home of the helpers `test_server` and `test_client` use.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rustyfarian_bench import config, remote, runner
from rustyfarian_bench.config import Board, ConfigError, Scenario, Step
from rustyfarian_bench.remote import Job, Worker, create_job, recover, run_job

from .fakes import FakeSerialWorld, shrink_sleep, wait_until

SCENARIO = """\
examples = ["ex1", "ex2"]

[[step]]
say = "banner"
expect = "ready"
arm = false
"""

IMAGE = b"\xe9merged-image\x00\xff"
JOB_ID = "20260101-000000-abcdef"


def meta(**top: Any) -> dict[str, Any]:
    """A valid upload's metadata; keyword arguments replace top-level fields."""
    board = {"key": "c6", "chip": "esp32c6", "name": "C6 bench", "baud": 115200, "reset": "rts"}
    return {"board": board, "check": "connect", "example": "ex1"} | top


def board_meta(**over: Any) -> dict[str, Any]:
    board: dict[str, Any] = meta()["board"]
    return board | over


def child(
    verdict: str | None = "pass",
    detail: str = "ok",
    board: str = "c6",
    code: int | None = None,
) -> list[str]:
    """An argv standing in for the `run` child: prints a line, records one result row, exits.

    The exit code is the one `run` uses for the verdict (`remote.EXIT`, else 1) unless `code`
    overrides it; a negative `code` makes the child kill itself with that signal.
    """
    row = json.dumps({"verdict": verdict, "detail": detail})
    if code is None:
        code = remote.EXIT.get(verdict, 1) if verdict is not None else 1
    script = "import os, sys\nprint('child ran')\n"
    if verdict is not None:
        script += f"open('bench/{board}/results.jsonl', 'a').write({row!r} + '\\n')\n"
    if code < 0:
        script += f"sys.stdout.flush()\nos.kill(os.getpid(), {-code})\n"
    else:
        script += f"sys.exit({code})\n"
    return [sys.executable, "-c", script]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(job_dir: Path) -> Job:
    job = remote.read_job(job_dir.parent, job_dir.name)
    assert job is not None
    return job


def scenario(**over: Any) -> Scenario:
    board = over.pop("board", Board("c6", "esp32c6", "C6"))
    steps = over.pop("steps", (Step(say="banner", expect=("ready",)),))
    return Scenario("chk", board, ("ex1",), steps, Path("chk.toml"), **over)


# -- the refusal boundary ----------------------------------------------------------------------


def test_a_plain_unattended_scenario_has_no_violations() -> None:
    assert remote.remote_violations(scenario()) == []


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"setup": (("echo", "hi"),)}, "setup commands"),
        ({"teardown": (("echo", "bye"),)}, "teardown commands"),
        (
            {"steps": (Step(say="s", expect=("x",), probe=("tail", "{port}")),)},
            "step 1 runs a host command (probe)",
        ),
        (
            {"steps": (Step(say="ok"), Step(say="s", expect=("x",), run=("flash-it",)))},
            "step 2 runs a host command (run)",
        ),
        ({"board": Board("c6", "esp32c6", "C6", detect=False)}, "detect = false"),
        ({"steps": (Step(say="s", confirm="ok?"),)}, "no unattended step"),
    ],
    ids=["setup", "teardown", "probe", "run", "no-detect", "only-attended"],
)
def test_a_host_command_or_an_unrunnable_scenario_is_refused(
    over: dict[str, Any], reason: str
) -> None:
    reasons = remote.remote_violations(scenario(**over))

    assert any(reason in r for r in reasons), reasons


def test_every_reason_is_listed() -> None:
    reasons = remote.remote_violations(
        scenario(setup=(("a",),), teardown=(("b",),), steps=(Step(say="s", confirm="ok?"),))
    )

    assert len(reasons) == 3


def test_a_probe_or_run_on_an_attended_only_step_is_allowed() -> None:
    # `--unattended` never reaches these steps, so no host command from them runs on the Pi.
    steps = (
        Step(say="banner", expect=("ready",)),
        Step(say="ask", confirm="ok?", probe=("tail", "{port}"), expect=("x",)),
        Step(say="press", attended=True, expect=("y",), run=("poke",)),
    )

    assert remote.remote_violations(scenario(steps=steps)) == []


# -- creating a job ----------------------------------------------------------------------------


def test_create_job_lays_out_a_runnable_repo(tmp_path: Path) -> None:
    job = create_job(tmp_path, meta(), SCENARIO, IMAGE)

    job_dir = tmp_path / job.id
    assert remote.JOB_ID.fullmatch(job.id)
    assert (job_dir / "image.bin").read_bytes() == IMAGE
    assert (job_dir / "scenario.toml").read_text() == SCENARIO
    assert (job_dir / "bench/c6/connect.toml").read_text() == SCENARIO
    assert read(job_dir) == job
    assert (job.status, job.board, job.chip, job.check, job.example) == (
        "queued",
        "c6",
        "esp32c6",
        "connect",
        "ex1",
    )
    cfg = config.load(job_dir)
    (found,) = cfg.scenarios
    assert (found.board.key, found.name, found.examples) == ("c6", "connect", ("ex1", "ex2"))
    assert (cfg.boards["c6"].baud, cfg.boards["c6"].reset) == (115200, "rts")


def test_the_generated_flash_command_writes_the_image_at_zero(tmp_path: Path) -> None:
    job = create_job(tmp_path, meta(), SCENARIO, IMAGE)

    cfg = config.load(tmp_path / job.id)

    flash = cfg.flash_for(cfg.boards["c6"])
    assert flash == (
        "espflash",
        "write-bin",
        "--non-interactive",
        "--chip",
        "esp32c6",
        "--port",
        "{port}",
        "0x0",
        "image.bin",
    )
    assert "monitor" not in flash
    assert "reset" not in flash


def test_the_chip_is_normalised(tmp_path: Path) -> None:
    job = create_job(tmp_path, meta(board=board_meta(chip="ESP32-C6")), SCENARIO, IMAGE)

    cfg = config.load(tmp_path / job.id)
    assert job.chip == "esp32c6"
    assert cfg.boards["c6"].chip == "esp32c6"
    assert cfg.flash[cfg.flash.index("--chip") + 1] == "esp32c6"


def test_a_board_name_with_quotes_cannot_break_out_of_the_toml(tmp_path: Path) -> None:
    name = 'He said "hi" \\ [commands]'
    job = create_job(tmp_path, meta(board=board_meta(name=name)), SCENARIO, IMAGE)

    cfg = config.load(tmp_path / job.id)

    assert cfg.boards["c6"].name == name
    assert cfg.build == ("true",)


def test_a_board_without_a_mac_has_no_mac_line_and_loads_without_one(tmp_path: Path) -> None:
    job = create_job(tmp_path, meta(), SCENARIO, IMAGE)

    toml = (tmp_path / job.id / "bench/bench.toml").read_text()
    assert not any(line.startswith("mac") for line in toml.splitlines())
    assert config.load(tmp_path / job.id).boards["c6"].mac is None


@pytest.mark.parametrize("sent", ["AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff", "Aa:bB:cC:dD:eE:fF"])
def test_a_mac_is_lowercased_into_bench_toml_and_the_loaded_board(
    tmp_path: Path, sent: str
) -> None:
    job = create_job(tmp_path, meta(board=board_meta(mac=sent)), SCENARIO, IMAGE)

    toml = (tmp_path / job.id / "bench/bench.toml").read_text()
    assert 'mac = "aa:bb:cc:dd:ee:ff"' in toml.splitlines()
    assert config.load(tmp_path / job.id).boards["c6"].mac == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "aa:bb:cc:dd:ee",
        "aa:bb:cc:dd:ee:ff:00",
        "aa-bb-cc-dd-ee-ff",
        "aabbccddeeff",
        "gg:bb:cc:dd:ee:ff",
        " aa:bb:cc:dd:ee:ff",
        "aa:bb:cc:dd:ee:ff\n",
        'aa:bb"\n[commands]',
        'aa:bb:cc:dd:ee:ff"\n[commands]\nbuild = ["x"]',
    ],
)
def test_a_malformed_mac_is_refused_and_leaves_no_directory(tmp_path: Path, bad: str) -> None:
    _assert_refused(tmp_path, "mac", meta=meta(board=board_meta(mac=bad)))


def test_a_mac_that_is_not_text_is_refused(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "mac", meta=meta(board=board_meta(mac=5)))


def test_the_mac_pattern_matches_the_whole_string_in_either_case() -> None:
    assert remote.MAC.fullmatch("aa:bb:cc:dd:ee:ff")
    assert remote.MAC.fullmatch("AA:BB:CC:DD:EE:FF")
    assert not remote.MAC.fullmatch("aa:bb:cc:dd:ee:ff\n")


def _assert_refused(tmp_path: Path, match: str, **kw: Any) -> None:
    args: dict[str, Any] = {"meta": meta(), "scenario_toml": SCENARIO, "image": IMAGE} | kw
    with pytest.raises(ConfigError, match=match):
        create_job(tmp_path, args["meta"], args["scenario_toml"], args["image"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("scenario_toml", "match"),
    [
        ('setup = [["echo", "hi"]]\n' + SCENARIO, "it has setup commands"),
        ('teardown = [["echo", "bye"]]\n' + SCENARIO, "it has teardown commands"),
        (SCENARIO + 'probe = ["tail"]\n', r"step 1 runs a host command \(probe\)"),
        ('examples = ["ex1"]\n[[step]]\nsay = "s"\nconfirm = "ok?"\n', "no unattended step"),
        ('examples = ["other"]\n[[step]]\nsay = "s"\nexpect = "x"\n', "does not list example"),
        ("this is [not toml", "connect.toml"),
    ],
    ids=["setup", "teardown", "step-probe", "only-attended", "other-example", "malformed"],
)
def test_a_refused_scenario_leaves_no_directory(
    tmp_path: Path, scenario_toml: str, match: str
) -> None:
    _assert_refused(tmp_path, match, scenario_toml=scenario_toml)


def test_a_step_with_a_host_command_is_refused_with_its_reason(tmp_path: Path) -> None:
    text = 'examples = ["ex1"]\n[[step]]\nsay = "s"\nexpect = "x"\nrun = ["rm", "-rf", "/"]\n'

    _assert_refused(tmp_path, "cannot run remotely.*step 1 runs a host command", scenario_toml=text)


def test_an_unwritable_scenario_text_is_a_config_error(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "not valid text", scenario_toml="examples = ['\ud800']")


def test_an_empty_image_is_refused(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "image", image=b"")


@pytest.mark.parametrize("bad", ["../x", "a/b", "-x", "a b", "", "é", "x" * 81, "..", "a\n"])
@pytest.mark.parametrize("field", ["board.key", "check", "example"])
def test_an_unsafe_name_is_refused(tmp_path: Path, field: str, bad: str) -> None:
    m = meta(board=board_meta(key=bad)) if field == "board.key" else meta(**{field: bad})

    _assert_refused(tmp_path, "must match", meta=m)


@pytest.mark.parametrize("bad", ["esp32;rm", "avr", "", "esp32s3 --after", "../esp32"])
def test_a_chip_outside_the_allowlist_is_refused(tmp_path: Path, bad: str) -> None:
    _assert_refused(tmp_path, "chip", meta=meta(board=board_meta(chip=bad)))


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"baud": 0}, "baud"),
        ({"baud": remote.MAX_BAUD + 1}, "baud"),
        ({"baud": True}, "baud"),
        ({"baud": "115200"}, "baud"),
        ({"reset": "usb"}, "reset"),
        ({"name": ""}, "name"),
        ({"name": "a\nb"}, "name"),
        ({"name": "x" * 81}, "name"),
        ({"chip": 5}, "chip"),
    ],
)
def test_bad_board_fields_are_refused(tmp_path: Path, over: dict[str, Any], match: str) -> None:
    _assert_refused(tmp_path, match, meta=meta(board=board_meta(**over)))


def test_a_scenario_that_does_not_list_the_example_is_refused(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "does not list example", meta=meta(example="ex9"))


def test_a_refusal_does_not_reveal_the_outbox_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as e:
        create_job(tmp_path, meta(), "this is not toml", IMAGE)

    assert str(tmp_path) not in str(e.value)
    assert "<job>" in str(e.value)


def test_a_scenario_nested_too_deeply_is_refused_not_crashed(tmp_path: Path) -> None:
    _assert_refused(tmp_path, "nested too deeply", scenario_toml="a=" + "[" * 200_000)


def test_a_job_id_collision_gets_a_fresh_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(["20260101-000000-aaaaaa", "20260101-000000-aaaaaa", "20260101-000000-bbbbbb"])
    monkeypatch.setattr(remote, "new_job_id", lambda: next(ids))
    first = create_job(tmp_path, meta(), SCENARIO, IMAGE)

    second = create_job(tmp_path, meta(), SCENARIO, IMAGE)

    assert (first.id, second.id) == ("20260101-000000-aaaaaa", "20260101-000000-bbbbbb")
    assert (tmp_path / second.id / "job.json").is_file()


# -- reading a job -----------------------------------------------------------------------------


def test_read_job_is_none_for_a_bad_unknown_or_corrupt_id(tmp_path: Path) -> None:
    (tmp_path / JOB_ID).mkdir()
    (tmp_path / JOB_ID / "job.json").write_text("{not json")

    assert remote.read_job(tmp_path, "../etc") is None
    assert remote.read_job(tmp_path, "20260101-000000-ABCDEF") is None  # hex is lower case
    assert remote.read_job(tmp_path, "٢٠٢٦٠١٠١-000000-abcdef") is None  # non-ASCII digits
    assert remote.read_job(tmp_path, "20260101-000000-123456") is None  # unknown
    assert remote.read_job(tmp_path, JOB_ID) is None  # corrupt


def test_write_job_replaces_the_file_in_one_step(tmp_path: Path) -> None:
    job = create_job(tmp_path, meta(), SCENARIO, IMAGE)
    job_dir = tmp_path / job.id

    job.status = remote.RUNNING
    remote.write_job(job_dir, job)

    assert read(job_dir).status == "running"
    assert [p.name for p in job_dir.iterdir() if p.name.startswith(".")] == []


# -- running a job -----------------------------------------------------------------------------


def _queued(outbox: Path) -> Path:
    return outbox / create_job(outbox, meta(), SCENARIO, IMAGE).id


def test_a_pass_row_makes_the_job_pass(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("pass", "all good"))

    job = read(job_dir)
    assert (job.status, job.verdict, job.detail, job.exit_code) == ("done", "pass", "all good", 0)
    assert job.started and job.finished
    assert "child ran" in (job_dir / "run.log").read_text()
    result = json.loads((job_dir / "result.json").read_text())
    assert result["verdict"] == "pass"
    assert [r["verdict"] for r in result["results"]] == ["pass"]


def test_the_last_row_decides(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)
    rows = job_dir / "bench/c6/results.jsonl"
    rows.write_text(json.dumps({"verdict": "fail", "detail": "old"}) + "\nnot json\n")

    run_job(job_dir, 30, child("pass"))

    assert read(job_dir).verdict == "pass"


def test_no_row_is_an_error_naming_the_last_line(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child(None))

    job = read(job_dir)
    assert (job.status, job.verdict) == ("done", "error")
    assert "recorded no result" in job.detail
    assert "last line: child ran" in job.detail


def test_a_pass_row_with_a_failing_exit_code_is_an_error(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("pass", "all good", code=1))

    job = read(job_dir)
    assert (job.status, job.verdict, job.exit_code) == ("done", "error", 1)
    assert job.detail == "run exited with code 1 after recording pass; last line: child ran"
    result = json.loads((job_dir / "result.json").read_text())
    assert (result["verdict"], result["exit_code"]) == ("error", 1)
    assert [r["verdict"] for r in result["results"]] == ["pass"]


def test_a_pass_row_followed_by_a_signal_death_is_an_error_naming_the_code(
    tmp_path: Path,
) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("pass", code=-int(signal.SIGTERM)))

    job = read(job_dir)
    assert job.verdict == "error"
    assert job.detail == "run exited with code -15 after recording pass; last line: child ran"


def _with_grandchild(record_pass: bool) -> list[str]:
    """An argv whose child leaves a sleeping grandchild in its process group, then exits."""
    script = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('grandchild.pid', 'w').write(str(g.pid))\n"
    )
    if record_pass:
        row = json.dumps({"verdict": "pass", "detail": "ok"})
        script += f"open('bench/c6/results.jsonl', 'a').write({row!r} + '\\n')\n"
        script += "sys.exit(0)\n"
    else:
        script += "raise SystemExit(1)\n"
    return [sys.executable, "-c", script]


def _ended(pid: int) -> bool:
    """Gone, or a zombie: a container without a reaping init leaves the orphan as one."""
    if not alive(pid):
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return stat.stdout.strip().startswith("Z")


def _kill_leftover(pid_file: Path) -> None:
    """Never leak a 60 s sleeper from a failed test, whatever the pid file holds."""
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    if alive(pid):
        os.kill(pid, signal.SIGKILL)


def test_a_grandchild_is_ended_after_the_child_passes(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)
    pid_file = job_dir / "grandchild.pid"
    try:
        run_job(job_dir, 30, _with_grandchild(record_pass=True))

        pid = int(pid_file.read_text())
        assert wait_until(lambda: _ended(pid), 5.0)
        assert read(job_dir).verdict == "pass"
    finally:
        _kill_leftover(pid_file)


def test_a_grandchild_is_ended_after_the_child_crashes(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)
    pid_file = job_dir / "grandchild.pid"
    try:
        run_job(job_dir, 30, _with_grandchild(record_pass=False))

        pid = int(pid_file.read_text())
        assert wait_until(lambda: _ended(pid), 5.0)
        job = read(job_dir)
        assert job.verdict == "error"
        assert "recorded no result" in job.detail
    finally:
        _kill_leftover(pid_file)


def test_a_fail_row_with_exit_code_1_stays_a_fail(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("fail", "no banner", code=1))

    job = read(job_dir)
    assert (job.verdict, job.detail, job.exit_code) == ("fail", "no banner", 1)
    assert json.loads((job_dir / "result.json").read_text())["verdict"] == "fail"


def test_a_fail_row_with_an_unexpected_exit_code_is_an_error(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("fail", "no banner", code=9))

    job = read(job_dir)
    assert job.verdict == "error"
    assert "run exited with code 9 after recording fail" in job.detail


def test_an_unverified_row_with_exit_code_3_stays_unverified(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("unverified", "no one confirmed"))

    job = read(job_dir)
    assert (job.verdict, job.detail, job.exit_code) == ("unverified", "no one confirmed", 3)


def test_a_child_that_cannot_start_is_an_error_not_an_exception(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, [str(tmp_path / "no-such-program")])

    job = read(job_dir)
    assert (job.status, job.verdict) == ("done", "error")
    assert "FileNotFoundError" in job.detail


def test_run_job_ignores_a_directory_that_is_not_a_job(tmp_path: Path) -> None:
    run_job(tmp_path, 30, child())  # no job.json: nothing runs, nothing raises

    assert not (tmp_path / "run.log").exists()


def test_a_timeout_ends_the_whole_process_group(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('grandchild.pid', 'w').write(str(g.pid))\n"
        "print('waiting', flush=True)\n"
        "time.sleep(60)\n"
    )

    run_job(job_dir, 1.5, [sys.executable, "-c", code])

    job = read(job_dir)
    assert (job.status, job.verdict) == ("done", "error")
    assert job.detail.startswith("timed out after 1.5 s; last line: waiting")
    pid = int((job_dir / "grandchild.pid").read_text())
    assert wait_until(lambda: not alive(pid), 5.0), "the grandchild outlived the timeout"


def test_the_child_runs_in_the_job_directory_even_for_a_relative_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    job = create_job(Path("outbox"), meta(), SCENARIO, IMAGE)
    job_dir = Path("outbox") / job.id

    run_job(job_dir, 30, [sys.executable, "-c", "import os; print(os.getcwd())"])

    printed = (job_dir / "run.log").read_text().strip()
    assert Path(printed).resolve() == (tmp_path / "outbox" / job.id).resolve()


def test_the_real_child_command_has_no_repo_flag_and_no_twins() -> None:
    argv = remote.child_argv("ex1")

    assert argv[:3] == [sys.executable, "-m", "rustyfarian_bench.cli"]
    assert argv[3:] == [
        "run",
        "ex1",
        "--unattended",
        "--no-build",
        "--no-twins",
        "--unique-board",
    ]


@pytest.mark.parametrize(
    ("timeout", "tail"),
    [(None, []), (30.0, ["--detect-timeout", "30"]), (2.5, ["--detect-timeout", "2.5"])],
)
def test_the_child_command_gets_the_detect_timeout_only_when_set(
    timeout: float | None, tail: list[str]
) -> None:
    argv = remote.child_argv("ex1", timeout)

    head = ["run", "ex1", "--unattended", "--no-build", "--no-twins"]
    assert argv[3:] == [*head, *tail, "--unique-board"]
    assert argv[-1] == "--unique-board"


def test_run_job_hands_the_detect_timeout_to_the_child_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, float | None]] = []

    def fake_argv(example: str, detect_timeout: float | None = None) -> list[str]:
        asked.append((example, detect_timeout))
        return child()

    monkeypatch.setattr(remote, "child_argv", fake_argv)
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, detect_timeout=12.0)

    assert asked == [("ex1", 12.0)]
    assert read(job_dir).verdict == "pass"


def test_a_given_argv_is_used_as_it_is_whatever_the_detect_timeout(tmp_path: Path) -> None:
    job_dir = _queued(tmp_path)

    run_job(job_dir, 30, child("pass", "mine"), detect_timeout=12.0)

    assert read(job_dir).detail == "mine"


# -- recovery and the worker -------------------------------------------------------------------


def test_recover_closes_unfinished_jobs_and_leaves_the_rest(tmp_path: Path) -> None:
    queued, running, done = (_queued(tmp_path) for _ in range(3))
    for d, status in ((running, "running"), (done, "done")):
        job = read(d)
        job.status = status
        job.verdict = "pass" if status == "done" else None
        remote.write_job(d, job)
    (tmp_path / "not-a-job").mkdir()
    (tmp_path / "stray.txt").write_text("x")

    recover(tmp_path)

    for d in (queued, running):
        job = read(d)
        assert (job.status, job.verdict, job.detail) == ("done", "error", remote.RESTARTED)
    assert (read(done).verdict, read(done).detail) == ("pass", "")


def test_recover_without_an_outbox_does_nothing(tmp_path: Path) -> None:
    recover(tmp_path / "missing")


def _worker_runs(outbox: Path, runner_fn: Callable[[Path], None], count: int) -> list[Path]:
    worker = Worker(runner_fn)
    dirs = [_queued(outbox) for _ in range(count)]
    worker.start()
    try:
        for d in dirs:
            worker.submit(d)
        assert wait_until(lambda: all(read(d).status == "done" for d in dirs), 5.0)
    finally:
        worker.stop()
    return dirs


def test_the_worker_runs_jobs_one_at_a_time_in_order(tmp_path: Path) -> None:
    order: list[str] = []
    running = 0
    most = 0

    def fake(job_dir: Path) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        order.append(job_dir.name)
        run_job(job_dir, 30, child())
        running -= 1

    dirs = _worker_runs(tmp_path, fake, 3)

    assert order == [d.name for d in dirs]
    assert most == 1


def test_the_worker_survives_a_runner_that_raises(tmp_path: Path) -> None:
    calls: list[str] = []

    def flaky(job_dir: Path) -> None:
        calls.append(job_dir.name)
        if len(calls) == 1:
            raise RuntimeError("boom")
        run_job(job_dir, 30, child())

    first, second = _worker_runs(tmp_path, flaky, 2)

    assert (read(first).verdict, read(first).detail) == ("error", "RuntimeError: boom")
    assert read(second).verdict == "pass"


def test_stopping_the_worker_kills_the_running_child(tmp_path: Path) -> None:
    code = "import os, time; print('pid', os.getpid(), flush=True); time.sleep(60)"
    job_dir = _queued(tmp_path)
    log = job_dir / "run.log"
    worker = Worker(timeout=60, argv=[sys.executable, "-c", code])
    worker.start()
    worker.submit(job_dir)
    assert wait_until(lambda: log.exists() and "pid" in log.read_text(), 10.0)
    pid = int(log.read_text().split()[1])

    worker.stop()

    assert not alive(pid), "the child kept running after stop()"
    assert read(job_dir).status == "done"


@pytest.mark.parametrize(("kw", "expected"), [({}, 30.0), ({"detect_timeout": 4.0}, 4.0)])
def test_the_worker_threads_the_detect_timeout_into_the_child_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], expected: float
) -> None:
    asked: list[float | None] = []

    def fake_argv(example: str, detect_timeout: float | None = None) -> list[str]:
        asked.append(detect_timeout)
        return child()

    monkeypatch.setattr(remote, "child_argv", fake_argv)
    job_dir = _queued(tmp_path)
    worker = Worker(timeout=30, **kw)
    worker.start()
    try:
        worker.submit(job_dir)
        assert wait_until(lambda: read(job_dir).status == "done", 10.0)
    finally:
        worker.stop()

    assert asked == [expected]


def test_a_child_that_found_no_board_ends_its_job_and_the_next_job_runs(tmp_path: Path) -> None:
    detail = "no C6 bench (esp32c6) found within 30 s"
    argvs = iter([child("error", detail), child("pass", "all good")])
    first, second = _worker_runs(tmp_path, lambda job_dir: run_job(job_dir, 30, next(argvs)), 2)

    job = read(first)
    assert (job.status, job.verdict, job.detail) == ("done", "error", detail)
    assert (read(second).status, read(second).verdict) == ("done", "pass")


# -- the whole path, with a real Session -------------------------------------------------------


def test_a_session_runs_write_bin_of_the_uploaded_image_at_zero_with_the_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = create_job(tmp_path, meta(), SCENARIO, IMAGE)
    job_dir = tmp_path / job.id
    cfg = config.load(job_dir)
    calls: list[tuple[list[str], Any]] = []

    def espflash(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw.get("cwd")))
        out = ""
        if argv[:2] == ["espflash", "board-info"]:
            out = "Chip type: esp32c6 (revision v0.1)\nMAC address: aa:bb:cc:dd:ee:ff\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", espflash)
    world = FakeSerialWorld([b"boot\nready\n"])
    world.install(monkeypatch)
    shrink_sleep(monkeypatch)
    opts = runner.Options(
        repo=job_dir,
        port="/dev/ttyACM0",
        build=False,
        assume_yes=True,
        mode="unattended",
        twins=False,
    )

    outcome = runner.Session(cfg, opts).run(cfg.scenarios, first_example="ex1")

    assert outcome == "pass"
    writes = [(argv, cwd) for argv, cwd in calls if argv[:2] == ["espflash", "write-bin"]]
    assert writes == [
        (
            [
                "espflash",
                "write-bin",
                "--non-interactive",
                "--chip",
                "esp32c6",
                "--port",
                "/dev/ttyACM0",
                "0x0",
                "image.bin",
            ],
            job_dir,
        )
    ]
    assert all(argv[0] == "espflash" for argv, _ in calls)
    assert not any(a in ("monitor", "reset") for argv, _ in calls for a in argv)
    assert (job_dir / "image.bin").read_bytes() == IMAGE  # what write-bin reads, from its cwd
    rows = remote._rows(job_dir, "c6")
    assert [(r["example"], r["verdict"]) for r in rows] == [("ex1", "pass")]
    assert len(world.opened) == 1
