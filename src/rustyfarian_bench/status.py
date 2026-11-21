"""The kiosk status file: what `serve` shows the panel, and how the panel reads it back.

Stdlib only, so the reader and the writer load neither Pillow nor FastAPI. The record is a
small JSON file (schema 1) that deliberately leaves out job ids, submitters, chips, checks,
details and log text: it is world-readable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import remote

SCHEMA = 1
STATES = ("idle", "running", "maintenance")  # "maintenance" is reserved: v2 never writes it
HISTORY = 5
STALE_AFTER = 30.0  # seconds of heartbeat age, in either direction (NTP steps)
HEARTBEAT = 10.0  # keep STALE_AFTER >= 3 * HEARTBEAT, or the panel flaps to the stale screen
READ_CAP = 2 << 20  # bytes of run log read per heartbeat; the rest waits for the next one
LINE_MAX = 8192  # a longer unfinished log line is dropped; a step line is ~60 bytes
WARN_EVERY = 60.0  # seconds between two warnings about the same error

# A step line starts a line (or a `\r` frame), after an optional colour escape; a device line
# (`  │ ...`) does not match unless it embeds a `\r`, so device output can spoof the counter:
# the result is a bounded 1..99 pair that shows only on the panel and never touches a verdict.
_STEP = re.compile(r"(?:^|\r)(?:\x1b\[[0-9;]*m)*▶ \[([0-9]{1,2})/([0-9]{1,2})\]", re.MULTILINE)


@dataclass(frozen=True)
class Running:
    board: str
    example: str
    started: datetime
    step: int | None = None  # None until the first `▶ [n/total]` line
    total: int | None = None


@dataclass(frozen=True)
class Verdict:
    verdict: str
    board: str
    example: str
    finished: datetime


@dataclass(frozen=True)
class Status:
    state: str
    updated: datetime
    version: str
    queued: int = 0
    running: Running | None = None
    history: tuple[Verdict, ...] = ()  # newest first

    def to_json(self) -> str:
        run = self.running
        return json.dumps(
            {
                "schema": SCHEMA,
                "state": self.state,
                "updated": _iso(self.updated),
                "version": self.version,
                "queued": self.queued,
                "running": None
                if run is None
                else {
                    "board": run.board,
                    "example": run.example,
                    "started": _iso(run.started),
                    "step": run.step,
                    "total": run.total,
                },
                "history": [
                    {
                        "verdict": v.verdict,
                        "board": v.board,
                        "example": v.example,
                        "finished": _iso(v.finished),
                    }
                    for v in self.history
                ],
            }
        )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _running(raw: object) -> Running | None:
    """A valid `running` object, or None for anything else (the caller decides what that means)."""
    if not isinstance(raw, dict):
        return None
    board, example, started = raw.get("board"), raw.get("example"), _time(raw.get("started"))
    step, total = raw.get("step"), raw.get("total")
    if not isinstance(board, str) or not isinstance(example, str) or started is None:
        return None
    if step is None and total is None:
        return Running(board, example, started)
    n, m = _int(step), _int(total)
    if n is None or m is None or not 1 <= n <= m <= 99:
        return None
    return Running(board, example, started, n, m)


def _verdict(raw: object) -> Verdict | None:
    if not isinstance(raw, dict):
        return None
    verdict, board, example = raw.get("verdict"), raw.get("board"), raw.get("example")
    finished = _time(raw.get("finished"))
    if not (isinstance(verdict, str) and isinstance(board, str) and isinstance(example, str)):
        return None
    return None if finished is None else Verdict(verdict, board, example, finished)


def parse_status(raw: str | bytes) -> Status | None:
    """The record in `raw`, or None for bad JSON, a newer schema or any wrong type."""
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or _int(data.get("schema")) is None or data["schema"] > SCHEMA:
        return None
    state, version, updated = data.get("state"), data.get("version"), _time(data.get("updated"))
    queued, rows = _int(data.get("queued")), data.get("history")
    if state not in STATES or not isinstance(version, str) or updated is None:
        return None
    if queued is None or queued < 0 or not isinstance(rows, list):
        return None
    running = None
    if data.get("running") is not None:
        running = _running(data["running"])
        if running is None:
            return None
    history = tuple(_verdict(row) for row in rows[:HISTORY])
    if any(v is None for v in history):
        return None
    return Status(
        state, updated, version, queued, running, tuple(v for v in history if v is not None)
    )


def freshness(status: Status, now: datetime) -> bool:
    """True while the heartbeat is no more than 30 s away from `now`, in either direction."""
    return abs((now - status.updated).total_seconds()) <= STALE_AFTER


def parse_step(tail: str) -> tuple[int, int] | None:
    """The last valid `▶ [n/total]` step line in `tail`, with 1 <= n <= total <= 99."""
    found = None
    for match in _STEP.finditer(tail):
        n, total = int(match[1]), int(match[2])
        if 1 <= n <= total <= 99:
            found = (n, total)
    return found


class _StepFollower:
    """Reads what a job's run log gained since the last poll and finds its step lines in it.

    Only complete lines are parsed, so each parsed text begins at a real line start and the
    line-start anchor of a step line holds across reads; the unfinished rest is carried as bytes,
    which also keeps a cut `▶` whole. An unfinished line longer than `LINE_MAX` is dropped up to
    its end, never cut: its tail would begin mid-line.
    """

    def __init__(self, job_id: str, path: Path):
        self.job_id = job_id
        self.path = path
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self.carry = b""
        self.skip = False  # inside a dropped over-long line

    def poll(self) -> tuple[int, int] | None:
        """The last valid step in the lines completed since the last poll; None if there is none."""
        try:
            if self.path.stat().st_size < self.offset:
                self._reset()  # truncated or replaced: read again from the start
            with self.path.open("rb") as f:
                f.seek(self.offset)
                new = f.read(READ_CAP)
        except OSError:
            return None
        self.offset += len(new)
        data = self.carry + new
        if self.skip:
            end = _line_end(data)
            if end == 0:
                self.carry = b""
                return None
            data, self.skip = data[end:], False
        end = _line_end(data, last=True)
        done, self.carry = data[:end], data[end:]
        if len(self.carry) > LINE_MAX:
            self.carry, self.skip = b"", True
        return parse_step(done.decode("utf-8", errors="replace"))


def _line_end(data: bytes, *, last: bool = False) -> int:
    """The index just past the first (or last) `\n` or `\r` in `data`; 0 if there is none."""
    find = data.rfind if last else data.find
    ends = [i for i in (find(b"\n"), find(b"\r")) if i >= 0]
    if not ends:
        return 0
    return (max(ends) if last else min(ends)) + 1


def _verdict_of(job: remote.Job, fallback: datetime) -> Verdict | None:
    if job.status != remote.DONE or not isinstance(job.verdict, str):
        return None
    return Verdict(job.verdict, job.board, job.example, _time(job.finished) or fallback)


def history_from_outbox(outbox: Path, limit: int = HISTORY) -> tuple[Verdict, ...]:
    """The `limit` most recently finished jobs of the outbox, newest first; never raises.

    Every record is read: the id's second does not order jobs, its hex is random, and
    `remote.recover` finishes an old job at restart. Called once, when `serve` starts. A
    record without a usable finish time counts as finished now.
    """
    try:
        names = [e.name for e in outbox.iterdir() if remote.JOB_ID.fullmatch(e.name)]
    except OSError:
        return ()
    now = _now()
    rows: list[tuple[datetime, str, Verdict]] = []
    for name in names:
        job = remote.read_job(outbox, name)
        row = None if job is None else _verdict_of(job, now)
        if row is not None:
            rows.append((row.finished, name, row))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return tuple(row for _, _, row in rows[:limit])


def _open_0644(path: str, flags: int) -> int:
    """An `open` opener: the built-in `open` takes no mode for the file it creates."""
    return os.open(path, flags, 0o644)


class StatusWriter:
    """Keeps the status file in step with the worker; every public method is safe to call.

    The file is written at 0644 through a temp file and `os.replace`. A failure never reaches
    the caller: it is warned about on stderr, at most once a minute per distinct error.
    `now` and `wait` are test seams; `wait(seconds)` returns True once the writer must stop.
    """

    def __init__(
        self,
        path: Path,
        outbox: Path,
        *,
        version: str,
        interval: float = HEARTBEAT,
        now: Callable[[], datetime] = _now,
        wait: Callable[[float], bool] | None = None,
    ):
        self._path = path
        self._outbox = outbox
        self._version = version
        self._interval = interval
        self._now = now
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._lock = threading.Lock()
        self._closed = False
        self._queued: set[str] = set()
        self._current: str | None = None  # id of the running job
        self._running: Running | None = None
        self._history: tuple[Verdict, ...] = ()
        self._follow: _StepFollower | None = None  # the heartbeat thread's alone, so no lock
        self._warned: dict[str, datetime] = {}
        self._thread = threading.Thread(target=self._beat, name="bench-status", daemon=True)

    def start(self) -> None:
        with self._guard("start"):
            history = history_from_outbox(self._outbox)  # outside the lock
            with self._lock:
                if self._closed:
                    return
                self._history = history
                try:
                    self._write()
                finally:  # a failed first write is warned about; the heartbeat retries it
                    if self._thread.ident is None:
                        self._thread.start()

    def stop(self) -> None:
        """End the heartbeat and remove the file; later calls of any method do nothing."""
        with self._guard("stop"):
            with self._lock:
                if self._closed:
                    return
                self._closed = True
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join(2.0)
            self._path.unlink(missing_ok=True)

    def job_queued(self, job_id: str) -> None:
        with self._guard("job_queued"), self._lock:
            if self._closed:
                return
            self._queued.add(job_id)
            self._write()

    def observe(self, event: str, job_dir: Path) -> None:
        """The worker's `observer`: `"start"` or `"end"` of the job in `job_dir`."""
        with self._guard("observe"):
            job_id = job_dir.name
            job = remote.read_job(self._outbox, job_id)  # outside the lock
            moment = self._now()
            with self._lock:
                if self._closed:
                    return
                if event == "start":
                    if job is not None:
                        self._current = job_id
                        self._running = Running(job.board, job.example, moment)
                elif event == "end":
                    self._queued.discard(job_id)
                    if self._current == job_id:
                        self._current, self._running = None, None
                    row = None if job is None else _verdict_of(job, moment)
                    if row is not None:
                        self._history = (row, *self._history)[:HISTORY]
                self._write()

    def refresh(self) -> None:
        """Re-read the running job's start time and step, then rewrite the file (heartbeat)."""
        with self._guard("refresh"):
            with self._lock:
                job_id, running = self._current, self._running
            job = step = None
            if job_id is None:
                self._follow = None
            else:
                if self._follow is None or self._follow.job_id != job_id:
                    self._follow = _StepFollower(job_id, self._outbox / job_id / "run.log")
                job = remote.read_job(self._outbox, job_id)  # outside the lock
                step = self._follow.poll()
            with self._lock:
                if self._closed:
                    return
                if running is not None and self._current == job_id and job_id is not None:
                    started = (job and _time(job.started)) or running.started
                    n, total = step or (running.step, running.total)  # keep the cached step
                    self._running = Running(running.board, running.example, started, n, total)
                self._write()

    def _beat(self) -> None:
        while not self._closed and not self._wait(self._interval):
            self.refresh()

    def _write(self) -> None:
        """Write the record; the caller holds the lock."""
        status = Status(
            "running" if self._running else "idle",
            self._now(),
            self._version,
            len(self._queued - {self._current} if self._current else self._queued),
            self._running,
            self._history,
        )
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        try:
            # the file object owns the descriptor from the start, so every failure closes it
            with open(tmp, "w", encoding="utf-8", opener=_open_0644) as f:
                os.fchmod(f.fileno(), 0o644)  # the umask does not apply to fchmod
                f.write(status.to_json())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    @contextlib.contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        try:
            yield
        except Exception as e:
            self._warn(f"{what}: {type(e).__name__}: {e}")

    def _warn(self, message: str) -> None:
        with contextlib.suppress(Exception):
            moment = self._now()
            last = self._warned.get(message)
            if last is None or abs((moment - last).total_seconds()) >= WARN_EVERY:
                self._warned[message] = moment
                print(f"status file: {message}", file=sys.stderr)
