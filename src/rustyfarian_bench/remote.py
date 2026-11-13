"""Remote bench, shared by `serve` and `submit`: job files, the refusal boundary and the runner.

A job is a tiny consumer repo the server writes into `outbox/<id>/`: its own `bench/bench.toml`
(flash = `espflash write-bin` of the uploaded `image.bin`) plus the uploaded scenario. The
unchanged `run` command then runs as a child process in that directory, its output going to
`run.log`. Stdlib only: no web framework is imported here.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, device, probe_lines
from .config import ConfigError, Scenario
from .runner import steps_for

# Path components and argv words: a leading `-` would parse as a flag, `/` and `..` escape.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# ASCII digits only: `\d` would also match other Unicode digits.
JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
# A board's MAC as espflash prints it; it ends up in a TOML string, so it is matched strictly.
MAC = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")
# Verdicts and the exit code `submit --wait` (like `run`) ends with; anything else is 1.
EXIT = {"pass": 0, "unverified": 3, "incomplete": 3}
# Seconds a job waits for its board; nobody plugs one in at a shared bench.
DETECT_TIMEOUT = 30.0
# Chips espflash can flash; the board's chip ends up in an argv, so it is allowlisted.
CHIPS = frozenset(
    {
        "esp32",
        "esp32s2",
        "esp32s3",
        "esp32c2",
        "esp32c3",
        "esp32c5",
        "esp32c6",
        "esp32h2",
        "esp32p4",
    }
)
NAME_MAX = 80
MAX_BAUD = 4_000_000  # far above any ESP32 console rate

QUEUED, RUNNING, DONE = "queued", "running", "done"
RESTARTED = "server restarted before the job finished"


def remote_violations(scenario: Scenario) -> list[str]:
    """Why `scenario` cannot run remotely; empty when it can.

    The server runs whatever scenario it is sent, so this is its security boundary: host
    commands (`setup`, `teardown`, a step's `probe` or `run`) never run on the Pi. A step that
    only an attended run would reach is not run remotely, so its `probe` or `run` is allowed.
    """
    reasons = []
    if scenario.setup:
        reasons.append("it has setup commands")
    if scenario.teardown:
        reasons.append("it has teardown commands")
    unattended = steps_for(scenario, "unattended")
    for n, step in enumerate(scenario.steps, 1):
        if step in unattended and (step.probe or step.run):
            reasons.append(f"step {n} runs a host command ({'probe' if step.probe else 'run'})")
    if not scenario.board.detect:
        reasons.append(f"board {scenario.board.key} has detect = false")
    if not unattended:
        reasons.append("it has no unattended step")
    return reasons


@dataclass
class Job:
    id: str
    board: str
    chip: str
    check: str
    example: str
    created: str
    mode: str = "unattended"
    status: str = QUEUED
    started: str | None = None
    finished: str | None = None
    verdict: str | None = None
    detail: str = ""
    exit_code: int | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_job_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_job(job_dir: Path, job: Job) -> None:
    _atomic_write(job_dir / "job.json", json.dumps(asdict(job)))


def _load(job_dir: Path) -> Job | None:
    try:
        return Job(**json.loads((job_dir / "job.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def read_job(outbox: Path, job_id: str) -> Job | None:
    """The job `job_id`, or None for an id that is malformed, unknown or unreadable."""
    if not JOB_ID.fullmatch(job_id):
        return None
    return _load(outbox / job_id)


def _quoted(text: str) -> str:
    # JSON escapes only `"`, `\` and control characters, so it is also a valid TOML string.
    return json.dumps(text, ensure_ascii=False)


def bench_toml(board: Mapping[str, Any]) -> str:
    """The server-written `bench/bench.toml`; `board` holds key, chip, name, baud, reset, mac?."""
    chip = device.normalize_chip(board["chip"])
    flash = ["espflash", "write-bin", "--non-interactive", "--chip", chip]
    flash += ["--port", "{port}", "0x0", "image.bin"]
    mac = board.get("mac")
    pin = f"mac = {_quoted(mac)}\n" if mac else ""
    return (
        "[commands]\n"
        'build = ["true"]\n'  # never run (the child gets --no-build); config.load requires it
        f"flash = {json.dumps(flash)}\n"
        f"\n[boards.{board['key']}]\n"
        f"chip = {_quoted(chip)}\n"
        f"name = {_quoted(board['name'])}\n"
        f"baud = {int(board['baud'])}\n"
        f"reset = {_quoted(board['reset'])}\n"
        f"{pin}"
    )


def _check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or len(value) > NAME_MAX or not NAME.fullmatch(value):
        raise ConfigError(
            f"{what}: must match [A-Za-z0-9][A-Za-z0-9_-]* ({NAME_MAX} chars at most)"
        )
    return value


def _check_board(raw: Mapping[str, Any]) -> dict[str, Any]:
    key = _check_name(raw.get("key"), "board key")
    chip = raw.get("chip")
    if not isinstance(chip, str) or device.normalize_chip(chip) not in CHIPS:
        raise ConfigError(f"board chip: {chip!r} is not one of {sorted(CHIPS)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
        raise ConfigError(f"board name: expected 1 to {NAME_MAX} printable characters")
    if not name.isprintable():
        raise ConfigError("board name: expected printable characters")
    baud = raw.get("baud")
    if isinstance(baud, bool) or not isinstance(baud, int) or not 0 < baud <= MAX_BAUD:
        raise ConfigError(f"board baud: must be an integer from 1 to {MAX_BAUD}")
    reset = raw.get("reset")
    if reset not in config.RESET_MODES:
        raise ConfigError(f"board reset: must be one of {', '.join(config.RESET_MODES)}")
    checked = {"key": key, "chip": chip, "name": name, "baud": baud, "reset": reset}
    mac = raw.get("mac")
    if mac is not None:
        if not isinstance(mac, str) or not MAC.fullmatch(mac):
            raise ConfigError("board mac: expected the form aa:bb:cc:dd:ee:ff")
        checked["mac"] = mac.lower()
    return checked


def create_job(outbox: Path, meta: Mapping[str, Any], scenario_toml: str, image: bytes) -> Job:
    """Validate an upload and lay it out as `outbox/<id>/`; the job is queued, not started.

    `meta` holds `board` (key, chip, name, baud, reset, optional mac), `check` and `example`.
    Anything the runner would not take, or `remote_violations` refuses, raises ConfigError and
    leaves no directory behind.
    """
    board = _check_board(meta.get("board") or {})
    check = _check_name(meta.get("check"), "check")
    example = _check_name(meta.get("example"), "example")
    if not image:
        raise ConfigError("image: is empty")
    job = Job(
        id=new_job_id(),
        board=board["key"],
        chip=device.normalize_chip(board["chip"]),
        check=check,
        example=example,
        created=_now(),
    )
    job_dir = _make_job_dir(outbox, job)
    try:
        _populate(job_dir, board, check, example, scenario_toml, image)
        write_job(job_dir, job)
    except Exception as e:  # a refused or failed upload leaves no trace
        shutil.rmtree(job_dir, ignore_errors=True)
        if isinstance(e, UnicodeEncodeError):
            raise ConfigError("scenario: is not valid text") from None
        if isinstance(e, RecursionError):
            raise ConfigError("scenario: nested too deeply") from None
        if isinstance(e, ConfigError):  # the reply must not reveal where the outbox lives
            shown = str(e).replace(str(job_dir.resolve()), "<job>").replace(str(job_dir), "<job>")
            raise ConfigError(shown) from None
        raise
    return job


def _make_job_dir(outbox: Path, job: Job) -> Path:
    """Create `outbox/<id>/`; a same-second id collision gets a fresh id."""
    for _ in range(5):
        job_dir = outbox / job.id
        try:
            job_dir.mkdir(parents=True)
        except FileExistsError:
            job.id = new_job_id()
        else:
            return job_dir
    raise ConfigError("could not allocate a job id; try again")


def _populate(
    job_dir: Path, board: dict[str, Any], check: str, example: str, scenario: str, image: bytes
) -> None:
    (job_dir / "image.bin").write_bytes(image)
    (job_dir / "scenario.toml").write_text(scenario, encoding="utf-8")
    bench = job_dir / "bench"
    (bench / board["key"]).mkdir(parents=True)
    (bench / "bench.toml").write_text(bench_toml(board), encoding="utf-8")
    (bench / board["key"] / f"{check}.toml").write_text(scenario, encoding="utf-8")
    cfg = config.load(job_dir)
    if len(cfg.scenarios) != 1:
        raise ConfigError(f"expected exactly one scenario, found {len(cfg.scenarios)}")
    found = cfg.scenarios[0]
    if example not in found.examples:
        raise ConfigError(f"scenario {check}: does not list example {example!r}")
    if reasons := remote_violations(found):
        raise ConfigError(f"scenario {check} cannot run remotely: " + "; ".join(reasons))


def child_argv(example: str, detect_timeout: float | None = None) -> list[str]:
    """The `run` command a job executes; its cwd is the job directory, which is the repo."""
    run = ["run", example, "--unattended", "--no-build", "--no-twins"]
    if detect_timeout is not None:
        run += ["--detect-timeout", f"{detect_timeout:g}"]
    run.append("--unique-board")  # a shared bench never guesses between same-chip boards
    return [sys.executable, "-m", "rustyfarian_bench.cli", *run]


def _last_line(log: Path) -> str:
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in re.split(r"[\r\n]+", tail) if line.strip()]
    return lines[-1][:200] if lines else ""


def _rows(job_dir: Path, board: str) -> list[dict[str, Any]]:
    try:
        text = (job_dir / "bench" / board / "results.jsonl").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _finish(job_dir: Path, job: Job, verdict: str, detail: str, exit_code: int | None) -> None:
    job.status, job.finished = DONE, _now()
    job.verdict, job.detail, job.exit_code = verdict, detail, exit_code
    write_job(job_dir, job)


def run_job(
    job_dir: Path,
    timeout: float,
    argv: Sequence[str] | None = None,
    *,
    on_proc: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    detect_timeout: float | None = None,
) -> None:
    """Run the job's child process to its end and record the outcome; never raises.

    `detect_timeout` bounds the child's wait for its board; None waits for it.

    `on_proc` is told about the child once it runs, and with None when it is gone.
    """
    job = _load(job_dir)
    if job is None:
        return
    try:
        _run_job(job_dir, job, timeout, argv or child_argv(job.example, detect_timeout), on_proc)
    except Exception as e:  # the worker must go on with the next job
        with contextlib.suppress(Exception):
            _finish(job_dir, job, "error", f"{type(e).__name__}: {e}", None)


def _run_job(
    job_dir: Path,
    job: Job,
    timeout: float,
    argv: Sequence[str],
    on_proc: Callable[[subprocess.Popen[bytes] | None], None] | None,
) -> None:
    job.status, job.started = RUNNING, _now()
    write_job(job_dir, job)
    log = job_dir / "run.log"
    timed_out = False
    with log.open("wb") as out:
        proc = subprocess.Popen(
            list(argv),
            cwd=job_dir.resolve(),  # the child's repo: no --repo, so a relative outbox is fine
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own group, so a timeout or stop can end all of it
            env={**device.ESPFLASH_ENV, "PYTHONUNBUFFERED": "1"},
        )
        if on_proc:
            on_proc(proc)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Descendants of a child that has exited must not keep the board's port.
            probe_lines.end_group(proc)
            if on_proc:
                on_proc(None)
    rows = _rows(job_dir, job.board)
    code = proc.returncode
    last = _last_line(log)
    if timed_out:
        verdict, detail = "error", f"timed out after {timeout:g} s; last line: {last}"
    elif rows:
        verdict, detail = str(rows[-1].get("verdict", "error")), str(rows[-1].get("detail", ""))
        # One scenario and --no-twins: the job records one row, so its exit code must match it.
        if code != EXIT.get(verdict, 1):
            detail = f"run exited with code {code} after recording {verdict}; last line: {last}"
            verdict = "error"
    else:
        verdict, detail = "error", f"run exited with code {code} and recorded no result"
        detail += f"; last line: {last}"
    result = {"verdict": verdict, "detail": detail, "exit_code": code, "results": rows}
    _atomic_write(job_dir / "result.json", json.dumps(result))
    _finish(job_dir, job, verdict, detail, code)


def recover(outbox: Path) -> None:
    """At server start, close the jobs a previous server left queued or running."""
    if not outbox.is_dir():
        return
    for job_dir in sorted(outbox.iterdir()):
        job = _load(job_dir) if JOB_ID.fullmatch(job_dir.name) else None
        if job is not None and job.status != DONE:
            with contextlib.suppress(OSError):
                _finish(job_dir, job, "error", RESTARTED, None)


class Worker:
    """One thread running jobs in order: a board must not be probed while another job flashes it.

    `runner(job_dir)` runs one job; the default is `run_job`, whose child process the worker
    keeps track of so `stop` can end it. A runner that raises never stops the worker.
    """

    def __init__(
        self,
        runner: Callable[[Path], None] | None = None,
        *,
        timeout: float = 600.0,
        argv: Sequence[str] | None = None,
        detect_timeout: float | None = DETECT_TIMEOUT,
    ):
        self._runner = runner or self._run_tracked
        self._timeout = timeout
        self._argv = argv
        self._detect_timeout = detect_timeout
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="bench-worker", daemon=True)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._stopping = False

    def start(self) -> None:
        self._thread.start()

    def submit(self, job_dir: Path) -> None:
        self._queue.put(job_dir)

    def stop(self, wait: float = 10.0) -> None:
        """End the running child's process group and the thread; queued jobs stay queued."""
        with self._lock:
            self._stopping = True
            proc = self._proc
        self._queue.put(None)
        if proc is not None:
            probe_lines.end_group(proc)
        if self._thread.is_alive():
            self._thread.join(wait)

    def _run_tracked(self, job_dir: Path) -> None:
        run_job(
            job_dir,
            self._timeout,
            self._argv,
            on_proc=self._track,
            detect_timeout=self._detect_timeout,
        )

    def _track(self, proc: subprocess.Popen[bytes] | None) -> None:
        with self._lock:
            self._proc = proc
            stopping = self._stopping
        if proc is not None and stopping:  # stop() came before the child existed
            probe_lines.end_group(proc)

    def _loop(self) -> None:
        while (job_dir := self._queue.get()) is not None:
            if self._stopping:
                break
            try:
                self._runner(job_dir)
            except Exception as e:
                with contextlib.suppress(Exception):
                    job = _load(job_dir)
                    if job is not None and job.status != DONE:
                        _finish(job_dir, job, "error", f"{type(e).__name__}: {e}", None)
