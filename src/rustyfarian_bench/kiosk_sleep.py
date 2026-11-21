"""When the kiosk panel goes dark: pure idle bookkeeping, stdlib only.

The panel sleeps after `after` seconds without activity. Activity is a touch or any change of
what the status record shows (`activity_key`); the clock is monotonic, since the Pi has no RTC
and wall-clock finish times would jump at the first NTP sync. The key holds no ages, so the
per-minute ticks of the history rows never count as activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .kiosk_layout import View, classify
from .status import Status


@dataclass(frozen=True)
class Idle:
    key: tuple[object, ...]  # `activity_key` of the last status seen
    since: float  # monotonic seconds of the last activity


def activity_key(status: Status | None, now: datetime) -> tuple[object, ...]:
    """What the status shows, without ages; a change (also into STALE or STOPPED) is activity."""
    view = classify(status, now)
    if status is None:
        return (view,)
    run = status.running
    last = status.history[0] if status.history else None
    return (
        view,
        status.queued,
        (run.board, run.example, run.started) if run is not None else None,
        (last.verdict, last.board, last.example, last.finished) if last is not None else None,
    )


def busy(status: Status | None, now: datetime) -> bool:
    """True while a job runs or waits; a stale record is never busy."""
    view = classify(status, now)
    if view is View.RUNNING:
        return True
    return view is View.IDLE and status is not None and status.queued > 0


def track(prev: Idle | None, key: tuple[object, ...], mono: float, touched: bool) -> Idle:
    """Restart the idle clock on the first call, a changed key or a touch."""
    if prev is None or touched or prev.key != key:
        return Idle(key, mono)
    return prev


def asleep(idle: Idle, busy: bool, mono: float, after: float) -> bool:
    """True once `after` seconds passed without activity; 0 turns sleep off."""
    return after > 0 and not busy and mono - idle.since >= after
