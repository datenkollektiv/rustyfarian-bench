from __future__ import annotations

from collections import deque

from rustyfarian_bench.config import Step
from rustyfarian_bench.steps import run_step


class FakeLines:
    """Lines already queued, then silence."""

    def __init__(self, *lines: str):
        self.queue = deque(lines)
        self.drained = 0

    def get(self, timeout: float) -> str | None:
        return self.queue.popleft() if self.queue else None

    def drain(self) -> None:
        self.drained += 1
        self.queue.clear()


def never(_: str) -> bool:
    raise AssertionError("unexpected prompt")


PRESSES = [
    "t=10 ms  pressed  (count=1)",
    "t=90 ms  released",
    "t=300 ms  pressed  (count=2)",
    "t=380 ms  released",
    "t=600 ms  pressed  (count=3)",
    "t=680 ms  released",
]


def test_expect_sequence_passes() -> None:
    step = Step(say="press 3x", expect=(r"count=1", "released", r"count=3", "released"))
    assert run_step(step, FakeLines("banner", *PRESSES), never).ok


def test_expect_consumes_backlog() -> None:
    step = Step(say="banner", expect=("B3F button ready",), timeout=0.3)
    assert run_step(step, FakeLines("B3F button ready on GPIO 41"), never).ok


def test_expect_times_out() -> None:
    step = Step(say="press", expect=("count=3",), timeout=0.3)
    res = run_step(step, FakeLines(*PRESSES[:2]), never)
    assert not res.ok
    assert "timeout" in res.detail


def test_reject_fails_expect_step() -> None:
    step = Step(say="press", expect=("count=3",), reject="PANIC", timeout=1)
    res = run_step(step, FakeLines("PANIC: boom", *PRESSES), never)
    assert not res.ok
    assert "PANIC" in res.detail


def test_settle_catches_extra_press() -> None:
    step = Step(say="press 3x", expect=("count=3",), reject=r"count=[4-9]", settle=0.3)
    res = run_step(step, FakeLines(*PRESSES, "t=900 ms  pressed  (count=4)"), never)
    assert not res.ok
    assert "settling" in res.detail


def test_duration_drains_backlog_then_passes() -> None:
    lines = FakeLines(*PRESSES)
    step = Step(say="rotate", duration=0.3, reject="pressed")
    assert run_step(step, lines, never).ok
    assert lines.drained == 1


def test_optional_miss_counts_as_pass() -> None:
    step = Step(say="banner", expect=("banner",), timeout=0.2, optional=True)
    res = run_step(step, FakeLines(), never)
    assert not res.ok
    assert res.counts_as_pass


def test_confirm_uses_operator_answer() -> None:
    step = Step(say="listen", confirm="Did it play?")
    assert run_step(step, FakeLines(), lambda q: True).ok
    assert not run_step(step, FakeLines(), lambda q: False).ok
