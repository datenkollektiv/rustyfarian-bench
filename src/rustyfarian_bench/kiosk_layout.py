"""The kiosk screens as plain drawing primitives, for a 480x320 panel.

Pure and deterministic: no Pillow, no clock, no I/O. `layout` turns a status record and a `now`
into a tuple of frozen `Text`, `Rect` and `Line` primitives; the painter in `kiosk` only draws
them, and the loop redraws only when the tuple changes. For that reason nothing that ticks every
second is drawn outside the running view: verdict ages are bucketed to minutes, and the heartbeat
age shows only on the stale screen, also in minutes. Text that shares a row gets a `max_width`
column in pixels; the painter fits the text into it, since a character count cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .status import HISTORY, Status, Verdict, freshness

WIDTH = 480
HEIGHT = 320
MARGIN = 24
HISTORY_ROWS = HISTORY  # the writer and the parser keep exactly as many rows as are drawn
ROW_HEIGHT = 24
ROW_CHARS = 37  # a cap on "board example"; the pixel fit is the painter's, by `max_width`
TEXT_W = WIDTH - 2 * MARGIN
TIMER_W = 120  # the elapsed time at size 30, "MM:SS" up to "100:00"
AGE_W = 52  # a verdict age at size 16, "now", "59m" up to "999d"
GAP = 10
VERDICT_X = MARGIN + 22
HISTORY_NAME_X = MARGIN + 92

BACKGROUND = (0, 0, 0)
TITLE_COLOUR = (255, 255, 255)
SUBTITLE_COLOUR = (160, 160, 160)
ACCENT_COLOUR = (230, 110, 30)
TRACK_COLOUR = (50, 50, 50)
PASS_COLOUR = (60, 200, 90)
FAIL_COLOUR = (230, 60, 60)
ERROR_COLOUR = (240, 190, 40)
UNKNOWN_COLOUR = (120, 120, 120)
FAREWELL = "good-bye"

VERDICT_COLOURS = {"pass": PASS_COLOUR, "fail": FAIL_COLOUR, "error": ERROR_COLOUR}

Colour = tuple[int, int, int]


class View(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STALE = "stale"
    STOPPED = "stopped"
    MAINTENANCE = "maintenance"
    FAREWELL = "farewell"


@dataclass(frozen=True)
class Text:
    x: int
    y: int
    text: str
    size: int
    fill: Colour
    anchor: str = "lm"  # Pillow anchor: left, middle or right of the text, vertically centred
    max_width: int | None = None  # pixels the text may take; None: up to the panel edge


@dataclass(frozen=True)
class Rect:
    x0: int
    y0: int
    x1: int  # exclusive
    y1: int  # exclusive
    fill: Colour


@dataclass(frozen=True)
class Line:
    x0: int
    y0: int
    x1: int
    y1: int
    fill: Colour
    width: int = 1


Primitive = Text | Rect | Line


def truncate(text: str, n: int) -> str:
    """`text` cut to at most `n` characters, with an ellipsis where it was shortened."""
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…" if n > 1 else text[:1]


def classify(status: Status | None, now: datetime) -> View:
    """The view for a status record; None (no readable file) means `serve` is not running."""
    if status is None:
        return View.STOPPED
    if not freshness(status, now):
        return View.STALE
    if status.state == "maintenance":
        return View.MAINTENANCE
    if status.state == "running" and status.running is not None:
        return View.RUNNING
    return View.IDLE


def _elapsed(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _age(seconds: float) -> str:
    """An age in whole minutes, hours or days: it changes at most once a minute."""
    minutes = max(0, int(seconds)) // 60
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _background() -> Rect:
    return Rect(0, 0, WIDTH, HEIGHT, BACKGROUND)


def _history(history: tuple[Verdict, ...], now: datetime, top: int) -> list[Primitive]:
    if not history:
        return [Text(MARGIN, top + ROW_HEIGHT // 2, "no jobs yet", 16, SUBTITLE_COLOUR)]
    prims: list[Primitive] = []
    for i, row in enumerate(history[:HISTORY_ROWS]):
        y = top + i * ROW_HEIGHT
        mid = y + ROW_HEIGHT // 2
        colour = VERDICT_COLOURS.get(row.verdict, UNKNOWN_COLOUR)
        name = truncate(f"{row.board} {row.example}", ROW_CHARS)
        prims += [
            Rect(MARGIN, mid - 6, MARGIN + 12, mid + 6, colour),
            Text(VERDICT_X, mid, row.verdict.upper()[:5], 16, colour,
                 max_width=HISTORY_NAME_X - GAP - VERDICT_X),
            Text(HISTORY_NAME_X, mid, name, 16, TITLE_COLOUR,
                 max_width=WIDTH - MARGIN - AGE_W - GAP - HISTORY_NAME_X),
            Text(WIDTH - MARGIN, mid, _age((now - row.finished).total_seconds()), 16,
                 SUBTITLE_COLOUR, "rm", AGE_W),
        ]  # fmt: skip
    return prims


def _banner(title: str, subtitle: str, extra: str = "") -> tuple[Primitive, ...]:
    """The v1 look: title, accent line and subtitle, centred."""
    prims: list[Primitive] = [
        _background(),
        Text(WIDTH // 2, int(HEIGHT * 0.38), truncate(title, 24), 40, TITLE_COLOUR, "mm", TEXT_W),
        Line(2 * MARGIN, int(HEIGHT * 0.55), WIDTH - 2 * MARGIN, int(HEIGHT * 0.55),
             ACCENT_COLOUR, 3),
        Text(WIDTH // 2, int(HEIGHT * 0.70), truncate(subtitle, 36), 24, SUBTITLE_COLOUR, "mm",
             TEXT_W),
    ]  # fmt: skip
    if extra:
        prims.append(Text(WIDTH // 2, int(HEIGHT * 0.84), truncate(extra, 40), 18,
                          SUBTITLE_COLOUR, "mm", TEXT_W))  # fmt: skip
    return tuple(prims)


def _running_view(status: Status, now: datetime) -> tuple[Primitive, ...]:
    run = status.running
    assert run is not None
    prims: list[Primitive] = [
        _background(),
        Text(MARGIN, 32, truncate(run.board, 20), 34, TITLE_COLOUR,
             max_width=TEXT_W - TIMER_W - GAP),
        Text(WIDTH - MARGIN, 32, _elapsed((now - run.started).total_seconds()), 30,
             ACCENT_COLOUR, "rm", TIMER_W),
        Text(MARGIN, 68, truncate(run.example, 38), 20, SUBTITLE_COLOUR, max_width=TEXT_W),
    ]  # fmt: skip
    bar_x0, bar_x1 = MARGIN, WIDTH - MARGIN
    if run.step is not None and run.total is not None:
        prims.append(Text(MARGIN, 100, f"step {run.step}/{run.total}", 18, TITLE_COLOUR,
                          max_width=TEXT_W // 2 - GAP))  # fmt: skip
        prims.append(Rect(bar_x0, 116, bar_x1, 130, TRACK_COLOUR))
        done = (bar_x1 - bar_x0) * run.step // run.total
        prims.append(Rect(bar_x0, 116, bar_x0 + done, 130, ACCENT_COLOUR))
    else:
        prims.append(Text(MARGIN, 100, "starting", 18, SUBTITLE_COLOUR,
                          max_width=TEXT_W // 2 - GAP))  # fmt: skip
    if status.queued:
        prims.append(Text(WIDTH - MARGIN, 100, f"+{status.queued} queued", 18,
                          SUBTITLE_COLOUR, "rm", TEXT_W // 2 - GAP))  # fmt: skip
    prims.append(Line(MARGIN, 150, WIDTH - MARGIN, 150, ACCENT_COLOUR, 2))
    prims += _history(status.history, now, 164)
    return tuple(prims)


def _idle_view(
    status: Status, now: datetime, title: str, address: str, version: str
) -> tuple[Primitive, ...]:
    prims: list[Primitive] = [
        _background(),
        Text(WIDTH // 2, 34, truncate(title, 24), 34, TITLE_COLOUR, "mm", TEXT_W),
        Text(WIDTH // 2, 68, truncate(f"{version}  {address}".strip(), 44), 18,
             SUBTITLE_COLOUR, "mm", TEXT_W),
        Line(MARGIN, 90, WIDTH - MARGIN, 90, ACCENT_COLOUR, 3),
    ]  # fmt: skip
    if status.queued:
        prims.append(Text(WIDTH - MARGIN, 106, f"+{status.queued} queued", 16,
                          SUBTITLE_COLOUR, "rm", TEXT_W))  # fmt: skip
    prims += _history(status.history, now, 120)
    return tuple(prims)


def layout(
    status: Status | None, now: datetime, *, title: str, address: str, version: str
) -> tuple[Primitive, ...]:
    """The primitives for the screen `classify` picks; the same inputs give the same output."""
    view = classify(status, now)
    if view is View.RUNNING and status is not None:
        return _running_view(status, now)
    if view is View.IDLE and status is not None:
        return _idle_view(status, now, title, address, version)
    if view is View.STALE and status is not None:
        age = _age(abs((now - status.updated).total_seconds()))
        return _banner(title, "status is stale", f"last update {age} ago")
    if view is View.MAINTENANCE:
        return _banner(title, "maintenance")
    return _banner(title, "bench offline", "waiting for serve")


def farewell(title: str) -> tuple[Primitive, ...]:
    """The screen drawn when the kiosk stops: the v1 good-bye, with the title below."""
    return _banner(FAREWELL, title)


def dark() -> tuple[Primitive, ...]:
    """The screen drawn while the panel sleeps: one full-panel black background."""
    return (_background(),)
