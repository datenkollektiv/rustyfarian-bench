"""The kiosk loop with a fake framebuffer, a fake clock and a fake wait; no panel, no sleeping."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from rustyfarian_bench import kiosk, remote, status
from rustyfarian_bench import kiosk_layout as kl

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)
FB_WIDTH, FB_HEIGHT = 480, 320


class StopLoop(Exception):
    """The fake wait ran out of calls: the loop did not stop when it should have."""


class Clock:
    def __init__(self) -> None:
        self.moment = T0

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class FakeWait:
    """Stands in for Event.wait: advances the fake clock, runs hooks, ends after `stop_at` calls."""

    def __init__(
        self,
        clock: Clock,
        stop_at: int,
        *,
        hooks: dict[int, Callable[[], None]] | None = None,
        raise_after: int = 50,
    ) -> None:
        self.clock = clock
        self.stop_at = stop_at
        self.hooks = hooks or {}
        self.raise_after = raise_after
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> bool:
        self.calls.append(seconds)
        n = len(self.calls)
        if n > self.raise_after:
            raise StopLoop
        self.clock.advance(seconds)
        if n in self.hooks:
            self.hooks[n]()
        return n >= self.stop_at


class Recorder:
    """A render that records the primitives and returns a real black image."""

    def __init__(self) -> None:
        self.frames: list[tuple[kl.Primitive, ...]] = []

    def __call__(self, prims: tuple[kl.Primitive, ...], width: int, height: int) -> Image.Image:
        self.frames.append(prims)
        return Image.new("RGB", (width, height))

    def texts(self, index: int) -> list[str]:
        return [p.text for p in self.frames[index] if isinstance(p, kl.Text)]


class Fb:
    def __init__(self, sysfs: Path, dev_dir: Path, node: Path) -> None:
        self.sysfs = sysfs
        self.dev_dir = dev_dir
        self.node = node


def _make_fb(root: Path, *, name: str = "fb_ili9486", bpp: int = 16, with_node: bool = True) -> Fb:
    sysfs, dev_dir = root / "sys" / "graphics", root / "dev"
    fb = sysfs / "fb1"
    fb.mkdir(parents=True)
    dev_dir.mkdir()
    (fb / "name").write_text(name + "\n")
    (fb / "virtual_size").write_text(f"{FB_WIDTH},{FB_HEIGHT}\n")
    (fb / "bits_per_pixel").write_text(f"{bpp}\n")
    (fb / "stride").write_text(f"{FB_WIDTH * 2}\n")
    node = dev_dir / "fb1"
    if with_node:
        # the kiosk opens without O_CREAT, so the "device node" must exist beforehand
        os.close(os.open(node, os.O_WRONLY | os.O_CREAT, 0o644))
    return Fb(sysfs, dev_dir, node)


def _write_status(path: Path, **overrides: object) -> None:
    record: dict[str, object] = {
        "schema": 1,
        "state": "idle",
        "updated": T0.isoformat(),
        "version": "0.8.0",
        "queued": 0,
        "running": None,
        "history": [],
    }
    record.update(overrides)
    path.write_text(json.dumps(record))


def _run(
    tmp_path: Path,
    fb: Fb,
    clock: Clock,
    wait: FakeWait,
    render: kiosk.Render,
    *,
    path: Path | None = None,
    **kwargs: object,
) -> int:
    return kiosk.run(
        path or tmp_path / "status.json",
        title="Bench",
        address="bench.local",
        version="0.8.0",
        fb_match="ili9486",
        sysfs=fb.sysfs,
        dev_dir=fb.dev_dir,
        now=clock,
        wait=wait,
        render=render,
        **kwargs,  # type: ignore[arg-type]
    )


def test_first_tick_draws(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 1), rec) == 0
    # one frame for the first tick, one farewell
    assert len(rec.frames) == 2
    assert "Bench" in rec.texts(0)
    assert fb.node.stat().st_size == FB_WIDTH * FB_HEIGHT * 2


def test_unchanged_ticks_draw_once(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    # 10 s of fake time: still fresh, no verdict ages, so the idle screen is identical
    wait = FakeWait(clock, 5)
    assert _run(tmp_path, fb, clock, wait, rec, poll=2.0) == 0
    assert len(rec.frames) == 2  # the idle draw and the farewell
    assert wait.calls == [2.0] * 5


def test_running_redraws_once_per_second(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    running = {"board": "b", "example": "e", "started": T0.isoformat(), "step": 1, "total": 2}
    _write_status(tmp_path / "status.json", state="running", running=running)
    assert _run(tmp_path, fb, clock, FakeWait(clock, 3), rec) == 0
    # ticks at 0 s, 1 s, 2 s and 3 s of fake time are not all drawn: the loop ends at 3 s
    # after its third wait, so three tick frames and the farewell
    assert len(rec.frames) == 4
    assert [t for t in rec.texts(0) if ":" in t and t[0].isdigit()] == ["00:00"]
    assert "00:01" in rec.texts(1)
    assert "00:02" in rec.texts(2)


def test_garbage_file_draws_stopped_once(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    (tmp_path / "status.json").write_bytes(b"\x00not json{")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 4), rec) == 0
    assert len(rec.frames) == 2
    assert "bench offline" in rec.texts(0)


def test_vanished_file_draws_stopped_once(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    path = tmp_path / "status.json"
    _write_status(path)
    wait = FakeWait(clock, 5, hooks={1: path.unlink})
    assert _run(tmp_path, fb, clock, wait, rec) == 0
    # idle, then offline once, then the farewell
    assert len(rec.frames) == 3
    assert "bench offline" in rec.texts(1)


def test_missing_file_at_start_draws_stopped(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    assert _run(tmp_path, fb, clock, FakeWait(clock, 2), rec) == 0
    assert "bench offline" in rec.texts(0)


def test_framebuffer_oserror_is_retried(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path, with_node=False), Clock(), Recorder()
    _write_status(tmp_path / "status.json")

    def create_node() -> None:
        os.close(os.open(fb.node, os.O_WRONLY | os.O_CREAT, 0o644))

    wait = FakeWait(clock, 3, hooks={2: create_node})
    assert _run(tmp_path, fb, clock, wait, rec) == 0
    assert fb.node.stat().st_size == FB_WIDTH * FB_HEIGHT * 2
    # the failed ticks rendered too, then the draw that finally landed, then the farewell
    assert len(rec.frames) >= 3


def test_missing_node_is_not_created(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path, with_node=False), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 2), rec) == 0
    assert not fb.node.exists()


def test_once_fails_when_the_node_is_missing(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path, with_node=False), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 1), rec, once=True) == 1


def test_waits_for_the_framebuffer_and_ends_while_waiting(tmp_path: Path) -> None:
    sysfs, dev_dir = tmp_path / "sys", tmp_path / "dev"
    sysfs.mkdir()
    dev_dir.mkdir()
    clock, rec = Clock(), Recorder()
    wait = FakeWait(clock, 3)
    code = kiosk.run(
        tmp_path / "status.json",
        title="Bench",
        address="",
        version="0.8.0",
        fb_match="ili9486",
        sysfs=sysfs,
        dev_dir=dev_dir,
        now=clock,
        wait=wait,
        render=rec,
    )
    assert code == 0
    assert wait.calls == [kiosk.FB_POLL_SECONDS] * 3
    assert rec.frames == []  # no panel, so no farewell either


def test_once_without_a_framebuffer_fails(tmp_path: Path) -> None:
    sysfs, dev_dir = tmp_path / "sys", tmp_path / "dev"
    sysfs.mkdir()
    dev_dir.mkdir()
    clock = Clock()
    code = kiosk.run(
        tmp_path / "status.json",
        title="Bench",
        address="",
        version="0.8.0",
        fb_match="ili9486",
        sysfs=sysfs,
        dev_dir=dev_dir,
        once=True,
        now=clock,
        wait=FakeWait(clock, 1),
        render=Recorder(),
    )
    assert code == 1


def test_stop_draws_the_farewell_and_returns_zero(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 1), rec) == 0
    assert rec.texts(-1)[0] == "good-bye"


def test_stop_survives_a_vanished_panel(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    wait = FakeWait(clock, 2, hooks={2: fb.node.unlink})
    assert _run(tmp_path, fb, clock, wait, rec) == 0


def test_a_wait_that_never_stops_fails_instead_of_hanging(tmp_path: Path) -> None:
    fb, clock = _make_fb(tmp_path), Clock()
    _write_status(tmp_path / "status.json")
    wait = FakeWait(clock, 10**6, raise_after=5)
    with pytest.raises(StopLoop):
        _run(tmp_path, fb, clock, wait, Recorder())


def test_wrong_bit_depth_is_refused(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path, bpp=32), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    assert _run(tmp_path, fb, clock, FakeWait(clock, 1), rec) == 1
    assert rec.frames == []
    assert fb.node.stat().st_size == 0


def test_once_draws_one_frame_without_a_farewell_and_saves_the_png(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    _write_status(tmp_path / "status.json")
    png = tmp_path / "png"
    assert _run(tmp_path, fb, clock, FakeWait(clock, 1), rec, once=True, png_dir=png) == 0
    assert len(rec.frames) == 1
    assert (png / "kiosk.png").is_file()


class FakeTouch:
    """A scripted touch reader: `tap()` queues a touch that the next `touched()` drains."""

    def __init__(self, *, present: bool = True) -> None:
        self.present = present
        self.taps = 0
        self.polls = 0
        self.closed = False

    def tap(self) -> None:
        self.taps += 1

    def touched(self) -> bool:
        self.polls += 1
        tapped, self.taps = self.taps > 0, 0
        return tapped

    def close(self) -> None:
        self.closed = True


SLEEP_AFTER = 60
SLEEP_POLL = 10.0
# the tick at mono 60 s is the 7th; wait call n ends the tick at mono 10 * n s
DARK_TICK = 7


def _sleep_run(
    tmp_path: Path,
    fb: Fb,
    clock: Clock,
    wait: FakeWait,
    render: kiosk.Render,
    touch: FakeTouch,
    **kwargs: object,
) -> int:
    """Run the loop with a pinned status clock (the record stays fresh) and a monotonic one.

    `clock` only advances the monotonic reading; `now` stays at T0 so the heartbeat never goes
    stale on its own, which would count as activity.
    """
    options: dict[str, object] = {"poll": SLEEP_POLL, "sleep_after": SLEEP_AFTER}
    options.update(kwargs)
    return kiosk.run(
        tmp_path / "status.json",
        title="Bench",
        address="bench.local",
        version="0.8.0",
        fb_match="ili9486",
        sysfs=fb.sysfs,
        dev_dir=fb.dev_dir,
        now=lambda: T0,
        wait=wait,
        render=render,
        touch=touch,
        monotonic=lambda: (clock() - T0).total_seconds(),
        **options,  # type: ignore[arg-type]
    )


def _dark_count(rec: Recorder) -> int:
    return sum(1 for frame in rec.frames if frame == kl.dark())


def test_the_dark_frame_is_all_zero_bytes(tmp_path: Path) -> None:
    fb, clock, touch = _make_fb(tmp_path), Clock(), FakeTouch()
    _write_status(tmp_path / "status.json")
    seen: dict[str, bytes] = {}

    def snapshot() -> None:
        seen["dark"] = fb.node.read_bytes()

    wait = FakeWait(clock, 20, hooks={DARK_TICK: snapshot})
    assert _sleep_run(tmp_path, fb, clock, wait, kiosk.paint, touch) == 0
    assert not any(seen["dark"])
    assert len(seen["dark"]) == FB_WIDTH * FB_HEIGHT * 2


def test_dark_frame_comes_exactly_at_the_timeout(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    # stop after tick 6 (mono 50 s): still awake; then run to tick 7 (mono 60 s): dark
    assert _sleep_run(tmp_path, fb, clock, FakeWait(clock, DARK_TICK - 1), rec, touch) == 0
    assert _dark_count(rec) == 0
    clock2, rec2 = Clock(), Recorder()
    wait2 = FakeWait(clock2, 20)
    assert _sleep_run(tmp_path, fb, clock2, wait2, rec2, FakeTouch()) == 0
    assert _dark_count(rec2) == 1  # drawn once over 13 more dark ticks
    assert len(rec2.frames) == 3  # layout, dark, farewell


def test_a_touch_while_dark_redraws_once_and_nothing_else(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    wait = FakeWait(clock, 12, hooks={9: touch.tap})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    # layout, dark, the layout again on wake, farewell
    assert len(rec.frames) == 4
    assert rec.frames[1] == kl.dark()
    assert rec.frames[2] == rec.frames[0]
    assert _dark_count(rec) == 1


def test_a_touch_while_dark_leaves_the_panel_bytes_non_zero(tmp_path: Path) -> None:
    fb, clock, touch = _make_fb(tmp_path), Clock(), FakeTouch()
    _write_status(tmp_path / "status.json")
    seen: dict[str, bytes] = {}

    def snapshot() -> None:
        seen["awake"] = fb.node.read_bytes()

    # the tap lands before tick 10; wait 10 runs after it
    wait = FakeWait(clock, 11, hooks={9: touch.tap, 10: snapshot})
    assert _sleep_run(tmp_path, fb, clock, wait, kiosk.paint, touch) == 0
    assert any(seen["awake"])


def test_several_touches_wake_once(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")

    def burst() -> None:
        touch.tap()
        touch.tap()
        touch.tap()

    wait = FakeWait(clock, 12, hooks={9: burst, 10: touch.tap})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    assert len(rec.frames) == 4  # layout, dark, one wake redraw, farewell


def test_a_touch_while_awake_extends_the_clock_without_a_redraw(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    # the tap at tick 6 (mono 50 s) moves the timeout to mono 110 s, the 12th tick
    wait = FakeWait(clock, 11, hooks={5: touch.tap})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    assert len(rec.frames) == 2  # layout and farewell, no dark and no extra redraw

    clock2, rec2, touch2 = Clock(), Recorder(), FakeTouch()
    wait2 = FakeWait(clock2, 12, hooks={5: touch2.tap})
    assert _sleep_run(tmp_path, fb, clock2, wait2, rec2, touch2) == 0
    assert rec2.frames[1] == kl.dark()
    assert len(rec2.frames) == 3


def test_a_queued_job_wakes_the_panel_without_a_touch(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    path = tmp_path / "status.json"
    _write_status(path)
    wait = FakeWait(clock, 40, hooks={9: lambda: _write_status(path, queued=1)})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    # layout, dark, the queued layout on wake, farewell: busy keeps it awake to the end
    assert len(rec.frames) == 4
    assert rec.frames[1] == kl.dark()
    assert rec.frames[2] != kl.dark()
    assert touch.polls > 0


def test_a_running_job_never_sleeps(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    running = {"board": "b", "example": "e", "started": T0.isoformat(), "step": 1, "total": 2}
    _write_status(tmp_path / "status.json", state="running", running=running)
    # ten hours in ten-minute ticks
    wait = FakeWait(clock, 60, raise_after=100)
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch, poll=600.0) == 0
    assert _dark_count(rec) == 0


@pytest.mark.parametrize(("stop_at", "dark"), [(9, False), (10, True)])
def test_a_finished_job_sleeps_after_the_timeout(tmp_path: Path, stop_at: int, dark: bool) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    path = tmp_path / "status.json"
    running = {"board": "b", "example": "e", "started": T0.isoformat(), "step": 1, "total": 2}
    _write_status(path, state="running", running=running)
    verdict = {"verdict": "pass", "board": "b", "example": "e", "finished": T0.isoformat()}

    def finish() -> None:
        _write_status(path, history=[verdict])

    # the change is seen by tick 4 (mono 30 s); the timeout runs out at mono 90 s, tick 10
    wait = FakeWait(clock, stop_at, hooks={3: finish})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    assert _dark_count(rec) == (1 if dark else 0)


def test_sleep_after_zero_never_polls_the_touch_reader(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    wait = FakeWait(clock, 30)
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch, sleep_after=0) == 0
    assert touch.polls == 0
    assert len(rec.frames) == 2
    assert touch.closed


def test_a_missing_touch_device_keeps_the_panel_on(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    touch = FakeTouch(present=False)
    _write_status(tmp_path / "status.json")
    assert _sleep_run(tmp_path, fb, clock, FakeWait(clock, 30), rec, touch) == 0
    assert _dark_count(rec) == 0
    assert len(rec.frames) == 2


def test_a_failed_dark_draw_is_retried(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")

    def create_node() -> None:
        os.close(os.open(fb.node, os.O_WRONLY | os.O_CREAT, 0o644))

    # the node is gone for ticks 7 and 8, so the first two dark draws fail
    hooks = {DARK_TICK - 1: fb.node.unlink, DARK_TICK + 1: create_node}
    wait = FakeWait(clock, 12, hooks=hooks)
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    # layout, failed dark, failed dark, dark that landed, farewell: no layout in between
    assert len(rec.frames) == 5
    assert _dark_count(rec) == 3


def test_a_failed_dark_draw_is_not_taken_for_shown(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    wait = FakeWait(clock, 10, hooks={DARK_TICK - 1: fb.node.unlink})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    # the node never comes back: every dark tick tries again (ticks 7 to 10)
    assert _dark_count(rec) == 4


def test_once_never_sleeps(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    code = _sleep_run(tmp_path, fb, clock, FakeWait(clock, 1), rec, touch, once=True, sleep_after=1)
    assert code == 0
    assert len(rec.frames) == 1
    assert _dark_count(rec) == 0
    assert touch.polls == 0


def test_the_farewell_is_drawn_while_dark(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")
    assert _sleep_run(tmp_path, fb, clock, FakeWait(clock, 9), rec, touch) == 0
    assert rec.frames[-2] == kl.dark()
    assert rec.frames[-1] == kl.farewell("Bench")
    assert touch.closed


def test_find_framebuffer_matches_by_name(tmp_path: Path) -> None:
    fb = _make_fb(tmp_path, name="vc4drmfb")
    assert kiosk.find_framebuffer(fb.sysfs, "ili9486") is None
    assert kiosk.find_framebuffer(fb.sysfs, "drm") is not None


def test_find_framebuffer_skips_one_that_vanished(tmp_path: Path) -> None:
    fb = _make_fb(tmp_path)
    # fb0 listed by the glob, gone before its name is read (an overlay unloaded meanwhile)
    (fb.sysfs / "fb0").mkdir()
    assert kiosk.find_framebuffer(fb.sysfs, "ili9486") == fb.sysfs / "fb1"


def test_rgb565_packs_little_endian_and_pads_rows() -> None:
    image = Image.new("RGB", (2, 2), (255, 0, 0))
    assert kiosk.to_rgb565(image, 4) == b"\x00\xf8" * 4
    padded = kiosk.to_rgb565(image, 6)
    assert padded == (b"\x00\xf8\x00\xf8" + b"\x00\x00") * 2
    assert kiosk.to_rgb565(Image.new("RGB", (1, 1), (0, 255, 0)), 2) == b"\xe0\x07"
    assert kiosk.to_rgb565(Image.new("RGB", (1, 1), (0, 0, 255)), 2) == b"\x1f\x00"


@pytest.mark.parametrize("view", ["idle", "running", "stale", "stopped", "maintenance", "farewell"])
def test_painter_gives_a_panel_sized_rgb565_frame(view: str) -> None:
    running = status.Running("c3-mini-1", "blink", T0, 1, 3)
    history = (status.Verdict("pass", "c3-mini-1", "blink", T0 - timedelta(minutes=3)),)
    records = {
        "idle": status.Status("idle", T0, "0.8.0", 0, None, history),
        "running": status.Status("running", T0, "0.8.0", 1, running, history),
        "stale": status.Status("idle", T0 - timedelta(hours=1), "0.8.0"),
        "maintenance": status.Status("maintenance", T0, "0.8.0"),
    }
    args = {"title": "Bench", "address": "bench.local", "version": "0.8.0"}
    layout = kl.layout(records.get(view), T0, **args)
    prims = kl.farewell("Bench") if view == "farewell" else layout
    image = kiosk.paint(prims, FB_WIDTH, FB_HEIGHT)
    assert image.size == (FB_WIDTH, FB_HEIGHT)
    assert image.mode == "RGB"
    assert len(kiosk.to_rgb565(image, FB_WIDTH * 2)) == FB_WIDTH * FB_HEIGHT * 2


def test_painter_views_differ() -> None:
    args = {"title": "Bench", "address": "", "version": "0.8.0"}
    idle = kiosk.paint(kl.layout(status.Status("idle", T0, "0.8.0"), T0, **args), 480, 320)
    off = kiosk.paint(kl.layout(None, T0, **args), 480, 320)
    assert idle.tobytes() != off.tobytes()


def test_light_modules_import_without_pillow_or_fastapi() -> None:
    code = (
        "import sys\n"
        "import rustyfarian_bench.cli\n"
        "import rustyfarian_bench.status\n"
        "import rustyfarian_bench.kiosk_sleep\n"
        "import rustyfarian_bench.kiosk_touch\n"
        "bad = [m for m in ('PIL', 'fastapi') if m in sys.modules]\n"
        "sys.exit(','.join(bad) if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _ink(
    prims: tuple[kl.Primitive, ...],
) -> list[tuple[kl.Text, tuple[float, float, float, float]]]:
    """Each text with the box `paint` draws it in: the same `kiosk.fit` font and text."""
    draw = ImageDraw.Draw(Image.new("RGB", (kl.WIDTH, kl.HEIGHT)))
    boxes = []
    for t in prims:
        if isinstance(t, kl.Text):
            font, text = kiosk.fit(t)
            boxes.append((t, draw.textbbox((t.x, t.y), text, font=font, anchor=t.anchor)))
    return boxes


def _same_row(prims: tuple[kl.Primitive, ...], y: int) -> list[tuple[float, float, float, float]]:
    return [box for t, box in sorted(_ink(prims), key=lambda tb: tb[0].x) if t.y == y]


@pytest.mark.parametrize("board", ["W" * 20, "M" * 20, "W" * 10 + "m" * 10])
def test_a_wide_board_name_ends_before_the_timer(board: str) -> None:
    run = status.Running(board, "W" * 38, T0 - timedelta(minutes=99), 1, 3)
    prims = kl.layout(status.Status("running", T0, "0.8.0", 0, run), T0,
                      title="Bench", address="", version="0.8.0")  # fmt: skip
    (board_box, timer_box) = _same_row(prims, 32)
    assert board_box[2] < timer_box[0]
    (example_box,) = _same_row(prims, 68)
    assert example_box[0] >= kl.MARGIN and example_box[2] <= kl.WIDTH - kl.MARGIN


@pytest.mark.parametrize("state", ["idle", "running"])
@pytest.mark.parametrize("board", ["W" * remote.NAME_MAX, "W" * 20])
def test_a_long_history_name_ends_before_the_age(state: str, board: str) -> None:
    old = T0 - timedelta(days=99)  # the widest age, "99d"
    history = tuple(
        status.Verdict(kind, board, "M" * remote.NAME_MAX, old)
        for kind in ["pass", "fail", "error", "pass", "fail"]
    )
    run = status.Running("c3-mini-1", "blink", T0, 1, 3) if state == "running" else None
    prims = kl.layout(status.Status(state, T0, "0.8.0", 2, run, history), T0,
                      title="Bench", address="bench.local", version="0.8.0")  # fmt: skip
    rows = {t.y for t, _ in _ink(prims) if t.text == "99d"}
    assert len(rows) == len(history)
    for y in rows:
        verdict_box, name_box, age_box = _same_row(prims, y)
        assert verdict_box[2] < name_box[0]
        assert name_box[2] < age_box[0]
        assert age_box[2] <= kl.WIDTH


def test_fit_trims_text_that_does_not_fit_at_the_smallest_font() -> None:
    font, text = kiosk.fit(kl.Text(0, 0, "W" * 80, 16, kl.TITLE_COLOUR, max_width=100))
    assert text.endswith("…") and len(text) < 80
    assert font.getlength(text) <= 100


def test_fit_keeps_text_that_fits() -> None:
    _, text = kiosk.fit(kl.Text(0, 0, "blink", 16, kl.TITLE_COLOUR, max_width=200))
    assert text == "blink"


def test_losing_the_touch_device_while_dark_wakes_the_panel(tmp_path: Path) -> None:
    fb, clock, rec, touch = _make_fb(tmp_path), Clock(), Recorder(), FakeTouch()
    _write_status(tmp_path / "status.json")

    def lose() -> None:
        touch.present = False

    wait = FakeWait(clock, 12, hooks={DARK_TICK + 1: lose})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    # layout, dark, the layout again once nothing could wake it, farewell
    assert len(rec.frames) == 4
    assert rec.frames[1] == kl.dark()
    assert rec.frames[2] == rec.frames[0]
    assert _dark_count(rec) == 1


def test_a_returning_touch_device_restarts_the_idle_clock(tmp_path: Path) -> None:
    fb, clock, rec = _make_fb(tmp_path), Clock(), Recorder()
    touch = FakeTouch(present=False)
    _write_status(tmp_path / "status.json")

    def back() -> None:
        touch.present = True

    darks: list[int] = []

    # absent until mono 100 s, longer than SLEEP_AFTER: no dark until SLEEP_AFTER after its return
    wait = FakeWait(clock, 20, hooks={10: back, 15: lambda: darks.append(_dark_count(rec))})
    assert _sleep_run(tmp_path, fb, clock, wait, rec, touch) == 0
    assert darks == [0]  # still lit at mono 140 s
    assert _dark_count(rec) == 1
    assert rec.frames.index(kl.dark()) == 1  # the one layout first, dark only later
    assert len(rec.frames) == 3


def test_the_touch_reader_is_closed_when_no_framebuffer_turns_up(tmp_path: Path) -> None:
    sysfs, dev_dir = tmp_path / "sys", tmp_path / "dev"
    sysfs.mkdir()
    dev_dir.mkdir()
    for once in (False, True):
        clock, touch = Clock(), FakeTouch()
        kiosk.run(
            tmp_path / "status.json",
            title="Bench",
            address="",
            version="0.8.0",
            fb_match="ili9486",
            sysfs=sysfs,
            dev_dir=dev_dir,
            now=clock,
            wait=FakeWait(clock, 2),
            render=Recorder(),
            once=once,
            sleep_after=SLEEP_AFTER,
            touch=touch,
        )
        assert touch.closed
