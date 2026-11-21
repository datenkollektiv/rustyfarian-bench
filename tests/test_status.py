"""The kiosk status file: step parsing, outbox history, the writer and its failure modes."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rustyfarian_bench import remote, status, ui
from rustyfarian_bench.status import (
    Running,
    Status,
    StatusWriter,
    Verdict,
    freshness,
    history_from_outbox,
    parse_status,
    parse_step,
)

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
JOIN = 5.0


class FakeClock:
    """A settable `now` for the writer."""

    def __init__(self, start: datetime = T0):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def job_id(n: int) -> str:
    return f"20261121-1200{n:02d}-abcde{n % 10}"


def put_job(outbox: Path, n: int, jid: str | None = None, **fields: object) -> str:
    jid = jid or job_id(n)
    d = outbox / jid
    d.mkdir(parents=True, exist_ok=True)
    base: dict[str, object] = {
        "id": jid,
        "board": f"board-{n}",
        "chip": "esp32c3",
        "check": "hal",
        "example": f"ex_{n}",
        "created": "2026-11-21T12:00:00+00:00",
        "status": remote.DONE,
        "finished": f"2026-11-21T12:{n:02d}:00+00:00",
        "verdict": "pass",
        "submitter": "alice",
    }
    base.update(fields)
    (d / "job.json").write_text(json.dumps(base), encoding="utf-8")
    return jid


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    d = tmp_path / "outbox"
    d.mkdir()
    return d


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "status.json"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def writer(path: Path, outbox: Path, clock: FakeClock) -> Iterator[StatusWriter]:
    # `wait` returns True: the heartbeat thread, if started, ends at once and never sleeps.
    w = StatusWriter(path, outbox, version="0.8.0", now=clock, wait=lambda _s: True)
    yield w
    w.stop()


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --- parse_step ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("▶ [2/4] flash", (2, 4)),
        ("noise\n▶ [1/3] a\nmore\n▶ [3/3] b\n", (3, 3)),
        ("\x1b[1m\n▶ [2/9] bold\x1b[0m", (2, 9)),
        ("\x1b[1;31m▶ [1/2] coloured", (1, 2)),
        ("progress\r▶ [2/5] frame", (2, 5)),
        ("▶ [99/99] edge", (99, 99)),
        ("▶ [1/1] edge", (1, 1)),
    ],
)
def test_parse_step_accepts(tail: str, expected: tuple[int, int]) -> None:
    assert parse_step(tail) == expected


@pytest.mark.parametrize(
    "tail",
    [
        "",
        "no step here",
        "  │ ▶ [2/4] spoofed by the device",
        "  │ text\n  │ ▶ [3/4] still spoofed",
        "text ▶ [2/4] mid-line",
        "▶ [0/4] zero",
        "▶ [5/4] n above total",
        "▶ [1/100] total too big",
        "▶ [100/100] too big",
        "▶ [a/b] letters",
    ],
)
def test_parse_step_rejects(tail: str) -> None:
    assert parse_step(tail) is None


def test_parse_step_spoof_does_not_override_real_step() -> None:
    assert parse_step("▶ [2/4] real\n  │ ▶ [4/4] fake\n") == (2, 4)


def test_parse_step_skips_an_invalid_later_line() -> None:
    assert parse_step("▶ [2/4] real\n▶ [9/4] bogus\n") == (2, 4)


def test_parse_step_reads_what_ui_step_prints(capsys: pytest.CaptureFixture[str]) -> None:
    ui.step(3, 7, "flash the board")
    assert parse_step(capsys.readouterr().out) == (3, 7)


def test_parse_step_reads_ui_step_with_colour(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.step(2, 5, "watch")
    out = capsys.readouterr().out
    assert "\x1b[" in out
    assert parse_step(out) == (2, 5)


def test_parse_step_does_not_match_ui_device_line(capsys: pytest.CaptureFixture[str]) -> None:
    ui.device_line("▶ [2/4] pretend")
    assert parse_step(capsys.readouterr().out) is None


# --- history_from_outbox ------------------------------------------------------------------


def test_history_is_newest_first_and_limited(outbox: Path) -> None:
    for n in range(1, 9):
        put_job(outbox, n)
    rows = history_from_outbox(outbox)
    assert [r.example for r in rows] == ["ex_8", "ex_7", "ex_6", "ex_5", "ex_4"]
    assert rows[0].finished == datetime(2026, 11, 21, 12, 8, tzinfo=UTC)


def test_history_skips_unfinished_and_verdictless_jobs(outbox: Path) -> None:
    put_job(outbox, 1)
    put_job(outbox, 2, status=remote.QUEUED, verdict=None)
    put_job(outbox, 3, status=remote.RUNNING, verdict=None)
    put_job(outbox, 4, verdict=None)
    assert [r.example for r in history_from_outbox(outbox)] == ["ex_1"]


def test_history_ignores_foreign_entries_and_bad_json(outbox: Path) -> None:
    put_job(outbox, 1)
    (outbox / "not-a-job").mkdir()
    (outbox / "stray.txt").write_text("x", encoding="utf-8")
    bad = outbox / job_id(9)
    bad.mkdir()
    (bad / "job.json").write_text("{not json", encoding="utf-8")
    empty = outbox / job_id(8)
    empty.mkdir()
    assert [r.example for r in history_from_outbox(outbox)] == ["ex_1"]


def test_history_of_a_missing_outbox_is_empty(tmp_path: Path) -> None:
    assert history_from_outbox(tmp_path / "gone") == ()


def test_history_of_an_empty_outbox_is_empty(outbox: Path) -> None:
    assert history_from_outbox(outbox) == ()


def test_history_falls_back_when_finished_is_unusable(outbox: Path) -> None:
    put_job(outbox, 1, finished=None)
    (row,) = history_from_outbox(outbox)
    assert row.verdict == "pass"
    assert row.finished.tzinfo is not None


def test_history_limit_is_honoured(outbox: Path) -> None:
    for n in range(1, 5):
        put_job(outbox, n)
    assert len(history_from_outbox(outbox, limit=2)) == 2


def test_history_orders_same_second_jobs_by_finished(outbox: Path) -> None:
    for n in range(1, 9):
        put_job(outbox, n)
    # one second, random hex: the id order is the reverse of the finish order
    put_job(
        outbox, 20, "20261121-130000-aaaaaa", example="new", finished="2026-11-21T13:05:00+00:00"
    )
    put_job(
        outbox, 21, "20261121-130000-ffffff", example="old", finished="2026-11-21T13:01:00+00:00"
    )
    rows = history_from_outbox(outbox)
    assert [r.example for r in rows] == ["new", "old", "ex_8", "ex_7", "ex_6"]


def test_history_puts_a_recovered_job_with_an_old_id_first(outbox: Path) -> None:
    # recover() finishes a job a previous server left behind at restart time
    put_job(
        outbox,
        20,
        "20261121-100000-123456",
        example="recovered",
        verdict="error",
        finished="2026-11-21T14:00:00+00:00",
    )
    for n in range(1, 8):
        put_job(outbox, n)
    assert [r.example for r in history_from_outbox(outbox)][:2] == ["recovered", "ex_7"]


def test_history_breaks_a_finished_tie_by_id(outbox: Path) -> None:
    put_job(outbox, 1, finished="2026-11-21T13:00:00+00:00")
    put_job(outbox, 2, finished="2026-11-21T13:00:00+00:00")
    assert [r.example for r in history_from_outbox(outbox)] == ["ex_2", "ex_1"]


def test_history_limit_cuts_after_sorting(outbox: Path) -> None:
    put_job(outbox, 1, finished="2026-11-21T13:00:00+00:00")
    for n in range(2, 6):
        put_job(outbox, n)
    assert [r.example for r in history_from_outbox(outbox, limit=2)] == ["ex_1", "ex_5"]


# --- parse_status and freshness -----------------------------------------------------------


def sample() -> Status:
    return Status(
        "running",
        T0,
        "0.8.0",
        2,
        Running("b", "e", T0, 1, 3),
        (Verdict("pass", "b", "e", T0),),
    )


def test_status_round_trips() -> None:
    s = sample()
    assert parse_status(s.to_json()) == s


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema=2),
        lambda d: d.update(schema="1"),
        lambda d: d.update(schema=True),
        lambda d: d.update(state="exploding"),
        lambda d: d.update(queued="2"),
        lambda d: d.update(queued=-1),
        lambda d: d.update(updated="yesterday"),
        lambda d: d.update(updated="2026-11-21T12:00:00"),  # naive
        lambda d: d.update(history="none"),
        lambda d: d.update(history=[{"verdict": 1}]),
        lambda d: d.update(running={"board": "b"}),
        lambda d: d["running"].update(step=5, total=3),
        lambda d: d["running"].update(step=1, total=None),
    ],
)
def test_parse_status_rejects_bad_records(mutate: Callable[[dict[str, object]], object]) -> None:
    data = json.loads(sample().to_json())
    mutate(data)
    assert parse_status(json.dumps(data)) is None


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", "42", b"\xff\xfe"])
def test_parse_status_rejects_garbage(raw: str | bytes) -> None:
    assert parse_status(raw) is None


def test_parse_status_accepts_reserved_maintenance_state() -> None:
    s = Status("maintenance", T0, "0.8.0")
    assert parse_status(s.to_json()) == s


@pytest.mark.parametrize(
    ("age", "fresh"), [(0, True), (29, True), (30, True), (31, False), (-29, True), (-31, False)]
)
def test_freshness_boundaries(age: float, fresh: bool) -> None:
    s = Status("idle", T0, "0.8.0")
    assert freshness(s, T0 + timedelta(seconds=age)) is fresh


# --- the writer ---------------------------------------------------------------------------


def test_start_writes_idle_with_history(writer: StatusWriter, path: Path, outbox: Path) -> None:
    put_job(outbox, 1)
    writer.start()
    data = load(path)
    assert data["state"] == "idle"
    assert data["schema"] == 1
    assert data["version"] == "0.8.0"
    assert data["running"] is None
    assert len(data["history"]) == 1


def test_file_is_0644_under_a_strict_umask(writer: StatusWriter, path: Path) -> None:
    old = os.umask(0o077)
    try:
        writer.start()
        writer.job_queued(job_id(1))
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o644


def test_no_temp_file_is_left_behind(writer: StatusWriter, path: Path) -> None:
    writer.start()
    writer.job_queued(job_id(1))
    writer.refresh()
    assert sorted(p.name for p in path.parent.iterdir() if p.is_file()) == ["status.json"]


def test_concurrent_callers_leave_a_valid_file(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    writer.start()
    n = 8
    barrier = threading.Barrier(n, timeout=5)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait()
            for k in range(10):
                writer.job_queued(job_id(i * 10 + k))
                writer.refresh()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    parsed = parse_status(path.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed.queued == 80
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == []


def test_a_reader_never_sees_partial_json(writer: StatusWriter, path: Path) -> None:
    writer.start()
    done = threading.Event()
    bad: list[str] = []

    def read() -> None:
        while not done.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                bad.append("missing")
                continue
            if parse_status(raw) is None:
                bad.append(raw)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for k in range(300):
            writer.job_queued(job_id(k % 90))
            writer.refresh()
    finally:
        done.set()
        reader.join(JOIN)
    assert not reader.is_alive()
    assert bad == []


def test_a_write_error_is_swallowed_and_warned_once(
    writer: StatusWriter,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeClock,
) -> None:
    writer.start()

    def deny(*_a: object, **_k: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", deny)
    for k in range(3):
        writer.job_queued(job_id(k))  # must not raise
    err = capsys.readouterr().err
    assert err.count("PermissionError") == 1
    clock.advance(status.WARN_EVERY + 1)
    writer.job_queued(job_id(9))
    assert capsys.readouterr().err.count("PermissionError") == 1


def test_start_in_a_missing_directory_does_not_raise(
    tmp_path: Path, outbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = StatusWriter(
        tmp_path / "gone" / "status.json", outbox, version="0.8.0", wait=lambda _s: True
    )
    w.start()
    w.job_queued(job_id(1))
    w.stop()
    assert "FileNotFoundError" in capsys.readouterr().err


def test_stop_unlinks_and_is_idempotent(writer: StatusWriter, path: Path) -> None:
    writer.start()
    assert path.exists()
    writer.stop()
    assert not path.exists()
    writer.stop()
    assert not path.exists()
    assert not writer._thread.is_alive()


def test_stop_without_start_is_safe(writer: StatusWriter, path: Path) -> None:
    writer.stop()
    assert not path.exists()


def test_nothing_creates_the_file_after_stop(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    jid = put_job(outbox, 1)
    writer.start()
    writer.observe("start", outbox / jid)
    writer.stop()
    writer.observe("end", outbox / jid)
    writer.job_queued(job_id(2))
    writer.refresh()
    writer.start()
    assert not path.exists()


def test_observe_start_sets_running_with_a_start_time(
    writer: StatusWriter, path: Path, outbox: Path, clock: FakeClock
) -> None:
    jid = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    writer.start()
    clock.advance(5)
    writer.observe("start", outbox / jid)
    data = load(path)
    assert data["state"] == "running"
    assert data["running"] == {
        "board": "board-1",
        "example": "ex_1",
        "started": "2026-11-21T12:00:05+00:00",
        "step": None,
        "total": None,
    }


def test_observe_start_before_job_queued_keeps_queued_correct(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    jid = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    writer.start()
    writer.observe("start", outbox / jid)
    writer.job_queued(jid)  # arrives late: it is the running job, not a waiting one
    assert load(path)["queued"] == 0
    writer.job_queued(job_id(2))
    assert load(path)["queued"] == 1


def test_queued_counts_waiting_jobs_and_end_discards_the_job(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    a = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    writer.start()
    writer.job_queued(a)
    writer.job_queued(job_id(2))
    assert load(path)["queued"] == 2
    writer.observe("start", outbox / a)
    assert load(path)["queued"] == 1
    put_job(outbox, 1)  # the worker wrote the verdict
    writer.observe("end", outbox / a)
    data = load(path)
    assert data["state"] == "idle"
    assert data["running"] is None
    assert data["queued"] == 1
    assert data["history"][0]["example"] == "ex_1"


def test_end_keeps_at_most_five_verdicts(writer: StatusWriter, path: Path, outbox: Path) -> None:
    for n in range(1, 6):
        put_job(outbox, n)
    writer.start()
    extra = put_job(outbox, 6)
    writer.observe("start", outbox / extra)
    writer.observe("end", outbox / extra)
    rows = load(path)["history"]
    assert [r["example"] for r in rows] == ["ex_6", "ex_5", "ex_4", "ex_3", "ex_2"]


def test_refresh_picks_up_the_step_and_the_job_start(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    jid = put_job(
        outbox,
        1,
        status=remote.RUNNING,
        verdict=None,
        finished=None,
        started="2026-11-21T11:59:00+00:00",
    )
    (outbox / jid / "run.log").write_text("▶ [1/4] a\n  │ x\n▶ [2/4] b\n", encoding="utf-8")
    writer.start()
    writer.observe("start", outbox / jid)
    writer.refresh()
    running = load(path)["running"]
    assert running["step"] == 2 and running["total"] == 4
    assert running["started"] == "2026-11-21T11:59:00+00:00"


def test_refresh_keeps_the_cached_step_when_the_log_has_none(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    jid = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    log = outbox / jid / "run.log"
    log.write_text("▶ [3/4] c\n", encoding="utf-8")
    writer.start()
    writer.observe("start", outbox / jid)
    writer.refresh()
    log.write_text("garbage only\n", encoding="utf-8")
    writer.refresh()
    assert load(path)["running"]["step"] == 3


def test_a_spoofed_device_line_does_not_move_the_bar(
    writer: StatusWriter, path: Path, outbox: Path
) -> None:
    jid = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    (outbox / jid / "run.log").write_text("▶ [1/4] a\n  │ ▶ [4/4] fake\n", encoding="utf-8")
    writer.start()
    writer.observe("start", outbox / jid)
    writer.refresh()
    assert load(path)["running"]["step"] == 1


def test_heartbeat_refreshes_through_the_injected_wait(
    path: Path, outbox: Path, clock: FakeClock
) -> None:
    waits: list[float] = []
    ticked = threading.Event()

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) > 3:
            ticked.set()
            return True  # a loop bug fails here instead of hanging
        clock.advance(10)
        return False

    w = StatusWriter(path, outbox, version="0.8.0", now=clock, wait=wait)
    w.start()
    assert ticked.wait(JOIN)
    w._thread.join(JOIN)
    assert not w._thread.is_alive()
    assert waits == [status.HEARTBEAT] * 4
    assert load(path)["updated"] == "2026-11-21T12:00:30+00:00"
    w.stop()


def test_stop_ends_a_real_heartbeat_thread(path: Path, outbox: Path) -> None:
    w = StatusWriter(path, outbox, version="0.8.0", interval=3600)
    w.start()
    assert w._thread.is_alive()
    w.stop()
    w._thread.join(JOIN)
    assert not w._thread.is_alive()
    assert not path.exists()


def test_nothing_sensitive_reaches_the_file(writer: StatusWriter, path: Path, outbox: Path) -> None:
    done = put_job(outbox, 1, submitter="sekret", detail="sekret detail", check="sekret")
    live = put_job(
        outbox,
        2,
        status=remote.RUNNING,
        verdict=None,
        finished=None,
        submitter="sekret",
        detail="sekret",
    )
    (outbox / live / "run.log").write_text("sekret log text\n▶ [1/2] go\n", encoding="utf-8")
    writer.start()
    writer.job_queued(live)
    writer.observe("start", outbox / live)
    writer.refresh()
    during = path.read_text(encoding="utf-8")
    writer.observe("end", outbox / done)
    after = path.read_text(encoding="utf-8")
    for text in (during, after):
        assert "sekret" not in text
        assert live not in text
        assert "alice" not in text


def test_heartbeat_stays_well_inside_the_stale_threshold() -> None:
    assert status.STALE_AFTER >= 3 * status.HEARTBEAT


# --- failure recovery ----------------------------------------------------------------------


def test_a_failed_fchmod_closes_the_descriptor(
    monkeypatch: pytest.MonkeyPatch, writer: StatusWriter, path: Path
) -> None:
    opened: list[int] = []
    real_open = os.open

    def spy(file: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
        fd = real_open(file, flags, mode, *args, **kwargs)
        if Path(file).name == f".{path.name}.tmp":
            opened.append(fd)
        return fd

    def fail(*_: object) -> None:
        raise OSError(5, "fchmod failed")

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(os, "fchmod", fail)
    writer.start()
    writer.job_queued(job_id(1))
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == []
    monkeypatch.undo()
    writer.job_queued(job_id(2))
    assert load(path)["queued"] == 2


def _fail_replace_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace
    calls: list[int] = []

    def flaky(src: Any, dst: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "no space left")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)


def test_the_heartbeat_starts_when_the_first_write_fails(
    monkeypatch: pytest.MonkeyPatch, path: Path, outbox: Path, clock: FakeClock
) -> None:
    _fail_replace_once(monkeypatch)
    waits: list[float] = []
    ticked = threading.Event()

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) > 1:
            ticked.set()
            return True
        clock.advance(10)
        return False

    w = StatusWriter(path, outbox, version="0.8.0", now=clock, wait=wait)
    w.start()
    assert ticked.wait(JOIN)
    w._thread.join(JOIN)
    assert waits == [status.HEARTBEAT] * 2
    assert load(path)["updated"] == "2026-11-21T12:00:10+00:00"
    w.stop()


def test_a_real_heartbeat_runs_after_a_failed_first_write(
    monkeypatch: pytest.MonkeyPatch, path: Path, outbox: Path
) -> None:
    _fail_replace_once(monkeypatch)
    w = StatusWriter(path, outbox, version="0.8.0", interval=3600)
    w.start()
    assert w._thread.is_alive()
    w.stop()
    w._thread.join(JOIN)
    assert not w._thread.is_alive()


# --- following the run log ----------------------------------------------------------------


@pytest.fixture
def run_log(writer: StatusWriter, outbox: Path) -> Path:
    """A running job whose log the test appends to; the writer follows it."""
    jid = put_job(outbox, 1, status=remote.RUNNING, verdict=None, finished=None)
    log = outbox / jid / "run.log"
    log.write_bytes(b"")
    writer.start()
    writer.observe("start", outbox / jid)
    return log


def append(log: Path, data: bytes | str) -> None:
    with log.open("ab") as f:
        f.write(data.encode() if isinstance(data, str) else data)


def step_of(path: Path) -> tuple[int, int] | None:
    running = load(path)["running"]
    return None if running["step"] is None else (running["step"], running["total"])


def test_a_step_followed_by_much_output_is_seen(
    writer: StatusWriter, path: Path, run_log: Path
) -> None:
    append(run_log, "▶ [1/4] a\n")
    writer.refresh()
    assert step_of(path) == (1, 4)
    append(run_log, "▶ [3/4] c\n" + ("  │ device output\n" * 1000))  # ~18 KB after the marker
    writer.refresh()
    assert step_of(path) == (3, 4)


@pytest.mark.parametrize("cut", [1, 2, 4, 7, 11])
def test_a_step_split_across_two_appends_is_seen(
    writer: StatusWriter, path: Path, run_log: Path, cut: int
) -> None:
    append(run_log, "▶ [1/4] a\n")
    writer.refresh()
    marker = "▶ [3/4] go\n".encode()  # ▶ is three bytes: cuts 1 and 2 split it
    append(run_log, marker[:cut])
    writer.refresh()
    assert step_of(path) == (1, 4)
    append(run_log, marker[cut:])
    writer.refresh()
    assert step_of(path) == (3, 4)


def test_a_read_boundary_is_not_a_line_start(
    writer: StatusWriter, path: Path, run_log: Path
) -> None:
    append(run_log, "▶ [1/4] a\n  │ ")
    writer.refresh()
    append(run_log, "▶ [4/4] fake\n")
    writer.refresh()
    assert step_of(path) == (1, 4)


def test_a_new_job_reads_its_log_from_the_start(
    writer: StatusWriter, path: Path, outbox: Path, run_log: Path
) -> None:
    append(run_log, "▶ [3/4] c\n" + "x" * 500 + "\n")
    writer.refresh()
    writer.observe("end", run_log.parent)
    second = put_job(outbox, 2, status=remote.RUNNING, verdict=None, finished=None)
    (outbox / second / "run.log").write_text("▶ [1/2] a\n", encoding="utf-8")
    writer.observe("start", outbox / second)
    writer.refresh()
    assert step_of(path) == (1, 2)


def test_a_truncated_log_is_read_again_from_the_start(
    writer: StatusWriter, path: Path, run_log: Path
) -> None:
    append(run_log, "▶ [3/4] c\n" + "x" * 500 + "\n")
    writer.refresh()
    run_log.write_text("▶ [1/4] again\n", encoding="utf-8")
    writer.refresh()
    assert step_of(path) == (1, 4)


def test_a_line_without_end_stays_bounded(writer: StatusWriter, path: Path, run_log: Path) -> None:
    append(run_log, "▶ [1/4] a\n")
    for _ in range(20):
        append(run_log, b"x" * status.LINE_MAX)
        writer.refresh()
        assert writer._follow is not None
        assert len(writer._follow.carry) <= status.LINE_MAX
    append(run_log, "▶ [4/4] inside the long line\n▶ [2/4] ok\n")
    writer.refresh()
    assert step_of(path) == (2, 4)


def test_a_step_line_counts_once_it_ends(writer: StatusWriter, path: Path, run_log: Path) -> None:
    append(run_log, "▶ [2/4] b")
    writer.refresh()
    assert step_of(path) is None
    append(run_log, "\n")
    writer.refresh()
    assert step_of(path) == (2, 4)
