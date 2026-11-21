"""When the kiosk panel goes dark: pure idle bookkeeping, no Pillow, no board."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rustyfarian_bench import kiosk_layout as kl
from rustyfarian_bench.kiosk_sleep import Idle, activity_key, asleep, busy, track
from rustyfarian_bench.status import STALE_AFTER, Running, Status, Verdict

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
AFTER = 900.0


def _verdict(example: str = "blink", finished: datetime = NOW) -> Verdict:
    return Verdict("pass", "c3-mini-1", example, finished)


def _running(started: datetime = NOW) -> Running:
    return Running("c3-mini-1", "hal_c3_connect_async", started)


def _status(
    state: str = "idle",
    *,
    running: Running | None = None,
    history: tuple[Verdict, ...] = (),
    queued: int = 0,
    updated: datetime = NOW,
) -> Status:
    return Status(state, updated, "0.8.0", queued, running, history)


def _stale() -> Status:
    return _status("running", running=_running(), updated=NOW - timedelta(seconds=STALE_AFTER + 1))


# asleep: the boundary


def test_asleep_boundary_is_inclusive() -> None:
    idle = Idle((), 100.0)
    assert not asleep(idle, False, 100.0 + 899, AFTER)
    assert asleep(idle, False, 100.0 + 900, AFTER)
    assert asleep(idle, False, 100.0 + 901, AFTER)


def test_after_zero_never_sleeps() -> None:
    assert not asleep(Idle((), 0.0), False, 1e9, 0)


def test_busy_never_sleeps() -> None:
    assert not asleep(Idle((), 0.0), True, 10 * 3600, AFTER)


# busy


def test_running_is_busy() -> None:
    assert busy(_status("running", running=_running()), NOW)


def test_queued_job_with_idle_state_is_busy() -> None:
    assert busy(_status(queued=1), NOW)


def test_idle_without_queue_is_not_busy() -> None:
    assert not busy(_status(history=(_verdict(),)), NOW)


def test_running_without_a_job_is_idle_not_busy() -> None:
    assert not busy(_status("running", running=None), NOW)


def test_running_without_a_job_but_queued_is_busy() -> None:
    assert busy(_status("running", running=None, queued=2), NOW)


def test_stale_running_record_is_not_busy() -> None:
    assert not busy(_stale(), NOW)


def test_missing_status_is_not_busy() -> None:
    assert not busy(None, NOW)


# activity_key


def test_key_is_stable_across_age_ticks() -> None:
    status = _status(history=(_verdict(),), updated=NOW)
    later = NOW + timedelta(seconds=20)
    assert activity_key(status, NOW) == activity_key(status, later)


def test_key_ignores_the_heartbeat_timestamp() -> None:
    a = _status(history=(_verdict(),), updated=NOW)
    b = _status(history=(_verdict(),), updated=NOW + timedelta(seconds=5))
    assert activity_key(a, NOW) == activity_key(b, NOW)


def test_key_works_without_history() -> None:
    assert activity_key(_status(), NOW) == activity_key(_status(), NOW)


def test_key_changes_with_a_new_verdict() -> None:
    old = _status(history=(_verdict("blink", NOW - timedelta(hours=1)),))
    new = _status(history=(_verdict("blink", NOW), *old.history))
    assert activity_key(old, NOW) != activity_key(new, NOW)


def test_old_history_entry_does_not_hide_a_new_start() -> None:
    history = (_verdict(finished=NOW - timedelta(hours=2)),)
    idle = _status(history=history)
    started = _status("running", running=_running(), history=history)
    assert activity_key(idle, NOW) != activity_key(started, NOW)


def test_key_changes_with_the_queue() -> None:
    assert activity_key(_status(queued=0), NOW) != activity_key(_status(queued=1), NOW)


def test_key_changes_when_a_new_job_starts() -> None:
    a = _status("running", running=_running(NOW))
    b = _status("running", running=_running(NOW + timedelta(seconds=1)))
    assert activity_key(a, NOW) != activity_key(b, NOW)


def test_key_without_status_is_stable() -> None:
    assert activity_key(None, NOW) == activity_key(None, NOW)


def test_going_stale_changes_the_key() -> None:
    status = _status(history=(_verdict(),))
    later = NOW + timedelta(seconds=STALE_AFTER + 1)
    assert activity_key(status, NOW) != activity_key(status, later)


def test_serve_stopping_changes_the_key() -> None:
    status = _status(history=(_verdict(),))
    assert activity_key(status, NOW) != activity_key(None, NOW)


# track


def test_first_call_starts_the_clock() -> None:
    assert track(None, ("k",), 50.0, False) == Idle(("k",), 50.0)


def test_unchanged_key_keeps_the_clock() -> None:
    prev = Idle(("k",), 50.0)
    assert track(prev, ("k",), 500.0, False) is prev


def test_changed_key_restarts_the_clock() -> None:
    assert track(Idle(("k",), 50.0), ("other",), 500.0, False) == Idle(("other",), 500.0)


def test_touch_restarts_the_clock() -> None:
    assert track(Idle(("k",), 50.0), ("k",), 500.0, True) == Idle(("k",), 500.0)


def test_stale_wakes_once_then_sleeps_again() -> None:
    status = _status(history=(_verdict(),))
    fresh = activity_key(status, NOW)
    idle = track(None, fresh, 0.0, False)
    assert asleep(idle, False, AFTER, AFTER)
    stale_now = NOW + timedelta(seconds=STALE_AFTER + 1)
    idle = track(idle, activity_key(status, stale_now), AFTER + 1, False)
    assert not asleep(idle, busy(status, stale_now), AFTER + 1, AFTER)
    later = AFTER + 1
    idle = track(idle, activity_key(status, stale_now), later + AFTER - 1, False)
    assert not asleep(idle, False, later + AFTER - 1, AFTER)
    assert asleep(idle, False, later + AFTER, AFTER)


def test_serve_stopping_wakes_once() -> None:
    status = _status(history=(_verdict(),))
    idle = track(None, activity_key(status, NOW), 0.0, False)
    idle = track(idle, activity_key(None, NOW), AFTER + 5, False)
    assert idle.since == AFTER + 5
    assert not asleep(idle, False, AFTER + 6, AFTER)


# dark


def test_dark_is_one_full_panel_black_rect() -> None:
    assert kl.dark() == (kl.Rect(0, 0, kl.WIDTH, kl.HEIGHT, (0, 0, 0)),)
