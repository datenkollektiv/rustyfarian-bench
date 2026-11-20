"""The kiosk screens as drawing primitives: pure functions, no Pillow, no board."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from rustyfarian_bench import kiosk_layout as kl
from rustyfarian_bench.status import Running, Status, Verdict, parse_status

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
ARGS = {"title": "Bench", "address": "bench.local:8080", "version": "0.8.0"}


def _verdict(kind: str = "pass", minutes: int = 5, example: str = "blink") -> Verdict:
    return Verdict(kind, "c3-mini-1", example, NOW - timedelta(minutes=minutes))


def _running(
    step: int | None = None,
    total: int | None = None,
    *,
    started: datetime = NOW,
    board: str = "c3-mini-1",
    example: str = "hal_c3_connect_async",
) -> Running:
    return Running(board, example, started, step, total)


def _status(
    state: str = "idle",
    *,
    running: Running | None = None,
    history: tuple[Verdict, ...] = (),
    queued: int = 0,
    updated: datetime = NOW,
) -> Status:
    return Status(state, updated, "0.8.0", queued, running, history)


def _texts(prims: tuple[kl.Primitive, ...]) -> list[str]:
    return [p.text for p in prims if isinstance(p, kl.Text)]


def _layout(status: Status | None, now: datetime = NOW) -> tuple[kl.Primitive, ...]:
    return kl.layout(status, now, **ARGS)


def _bar_fill(prims: tuple[kl.Primitive, ...]) -> kl.Rect:
    return next(
        p for p in prims if isinstance(p, kl.Rect) and p.fill == kl.ACCENT_COLOUR and p.y0 == 116
    )


def test_idle_shows_title_version_address_and_history() -> None:
    prims = _layout(_status(history=(_verdict("fail"),)))
    texts = _texts(prims)
    assert "Bench" in texts
    assert "0.8.0  bench.local:8080" in texts
    assert "FAIL" in texts
    assert "c3-mini-1 blink" in texts
    assert "5m" in texts
    assert kl.classify(_status(), NOW) is kl.View.IDLE


def test_idle_without_history_says_so() -> None:
    assert "no jobs yet" in _texts(_layout(_status()))


def test_idle_shows_the_queue() -> None:
    assert "+2 queued" in _texts(_layout(_status(queued=2)))


def test_history_is_limited_to_five_rows() -> None:
    history = tuple(_verdict(example=f"ex{i}") for i in range(8))
    texts = _texts(_layout(_status(history=history)))
    assert "c3-mini-1 ex4" in texts
    assert "c3-mini-1 ex5" not in texts


def test_running_shows_board_example_elapsed_and_step() -> None:
    run = _running(2, 4, started=NOW - timedelta(seconds=75))
    prims = _layout(_status("running", running=run, queued=1, history=(_verdict(),)))
    texts = _texts(prims)
    assert kl.classify(_status("running", running=run), NOW) is kl.View.RUNNING
    assert {"c3-mini-1", "hal_c3_connect_async", "01:15", "step 2/4", "+1 queued"} <= set(texts)
    assert "PASS" in texts


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [(0, "00:00"), (59, "00:59"), (60, "01:00"), (3600, "60:00"), (7325, "122:05")],
)
def test_elapsed_formats_minutes_and_seconds(seconds: int, shown: str) -> None:
    run = _running(started=NOW - timedelta(seconds=seconds))
    assert shown in _texts(_layout(_status("running", running=run)))


def test_elapsed_is_never_negative() -> None:
    run = _running(started=NOW + timedelta(seconds=20))  # a clock that stepped back
    texts = _texts(_layout(_status("running", running=run)))
    assert "00:00" in texts
    assert not any(t.startswith("-") for t in texts)


def test_half_the_steps_give_a_half_width_bar() -> None:
    prims = _layout(_status("running", running=_running(2, 4)))
    track = next(p for p in prims if isinstance(p, kl.Rect) and p.fill == kl.TRACK_COLOUR)
    fill = _bar_fill(prims)
    assert (fill.x1 - fill.x0) * 2 == track.x1 - track.x0
    assert fill.x0 == track.x0


def test_no_bar_before_the_first_step() -> None:
    prims = _layout(_status("running", running=_running()))
    assert "starting" in _texts(prims)
    assert not any(isinstance(p, kl.Rect) and p.fill == kl.TRACK_COLOUR for p in prims)
    assert not any(t.startswith("step ") for t in _texts(prims))


def test_long_names_are_truncated() -> None:
    name = "x" * 200
    run = _running(board=name, example=name)
    verdict = Verdict("pass", name, name, NOW)
    prims = _layout(_status("running", running=run, history=(verdict,)))
    assert all(len(t) <= 40 for t in _texts(prims))
    assert any(t.endswith("…") for t in _texts(prims))


def test_truncate_edges() -> None:
    assert kl.truncate("abc", 3) == "abc"
    assert kl.truncate("abcd", 3) == "ab…"
    assert kl.truncate("abcd", 1) == "a"
    assert kl.truncate("abcd", 0) == ""


def test_wrong_typed_fields_degrade_to_the_offline_screen() -> None:
    raw = {"schema": 1, "state": "running", "updated": 12, "version": "0.8.0", "queued": "x"}
    parsed = parse_status(json.dumps(raw).encode())
    assert parsed is None
    prims = _layout(parsed)
    assert kl.classify(parsed, NOW) is kl.View.STOPPED
    assert "bench offline" in _texts(prims)


def test_running_state_without_a_running_record_is_idle() -> None:
    assert kl.classify(_status("running"), NOW) is kl.View.IDLE


def test_maintenance_is_drawn() -> None:
    status = _status("maintenance")
    assert kl.classify(status, NOW) is kl.View.MAINTENANCE
    assert "maintenance" in _texts(_layout(status))


def test_stale_heartbeat_is_drawn_in_either_direction() -> None:
    for updated in (NOW - timedelta(minutes=3), NOW + timedelta(minutes=3)):
        status = _status(updated=updated)
        assert kl.classify(status, NOW) is kl.View.STALE
        texts = _texts(_layout(status))
        assert "status is stale" in texts
        assert "last update 3m ago" in texts


def test_missing_file_is_the_stopped_screen() -> None:
    assert "bench offline" in _texts(_layout(None))


def test_farewell_says_good_bye() -> None:
    texts = _texts(kl.farewell("Bench"))
    assert texts[0] == "good-bye"
    assert "Bench" in texts


def test_layout_is_deterministic() -> None:
    status = _status("running", running=_running(1, 3), history=(_verdict(),))
    assert _layout(status) == _layout(status)


def test_verdict_colours_are_distinct() -> None:
    history = (_verdict("pass"), _verdict("fail"), _verdict("error"), _verdict("weird"))
    prims = _layout(_status(history=history))
    markers = [p.fill for p in prims if isinstance(p, kl.Rect) and p.x1 - p.x0 == 12]
    assert len(markers) == 4
    assert len(set(markers)) == 4


def test_idle_is_unchanged_within_a_minute() -> None:
    status = _status(history=(_verdict(minutes=5),), updated=NOW)
    first = _layout(status, NOW)
    # the heartbeat is still fresh and the verdict age has not rolled over
    assert _layout(status, NOW + timedelta(seconds=29)) == first


def _rows(prims: tuple[kl.Primitive, ...]) -> dict[int, list[kl.Text]]:
    rows: dict[int, list[kl.Text]] = {}
    for p in prims:
        if isinstance(p, kl.Text):
            rows.setdefault(p.y, []).append(p)
    return rows


def _left_edge(t: kl.Text) -> int:
    """Where a right-anchored text may start, given its column."""
    assert t.max_width is not None
    return t.x - t.max_width


@pytest.mark.parametrize("queued", [0, 3])
def test_texts_sharing_a_row_have_columns_that_do_not_meet(queued: int) -> None:
    history = tuple(_verdict(kind, minutes=i) for i, kind in enumerate(["pass", "error", "fail"]))
    for state, run in [("idle", None), ("running", _running(1, 3))]:
        prims = _layout(_status(state, running=run, history=history, queued=queued))
        for row in _rows(prims).values():
            if len(row) < 2:
                continue
            row.sort(key=lambda t: t.x)
            for left, right in pairwise(row):
                assert left.max_width is not None and right.max_width is not None
                left_end = left.x + left.max_width
                right_start = _left_edge(right) if right.anchor.startswith("r") else right.x
                assert left_end + kl.GAP <= right_start, (left, right)


def test_running_board_column_leaves_the_timer_its_width() -> None:
    prims = _layout(_status("running", running=_running()))
    board, timer = _rows(prims)[32]
    assert board.max_width is not None
    assert board.x + board.max_width <= timer.x - kl.TIMER_W - kl.GAP
    assert timer.max_width == kl.TIMER_W


def test_every_text_stays_inside_the_margins() -> None:
    history = (_verdict(),)
    statuses = [
        _status(history=history, queued=2),
        _status("running", running=_running(1, 3), history=history, queued=2),
        _status(updated=NOW - timedelta(hours=1)),
        None,
    ]
    for status in statuses:
        for t in (p for p in _layout(status) if isinstance(p, kl.Text)):
            if t.max_width is None:
                continue
            start = {"l": t.x, "m": t.x - t.max_width // 2, "r": t.x - t.max_width}[t.anchor[0]]
            assert start >= kl.MARGIN and start + t.max_width <= kl.WIDTH - kl.MARGIN, t
