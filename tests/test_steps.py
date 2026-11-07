from __future__ import annotations

import time

from rustyfarian_bench.config import Step
from rustyfarian_bench.steps import StepResult, run_step

from .fakes import FakeLines


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
    yes = run_step(step, FakeLines(), lambda q: True)
    no = run_step(step, FakeLines(), lambda q: False)
    assert yes.ok and yes.detail == "confirmed by operator"
    assert not no.ok and no.detail == "denied by operator"


def test_expected_line_is_matched_before_reject_applies() -> None:
    step = Step(
        say="press once",
        expect=("pressed", "released"),
        reject="pressed|released|PANIC",
    )
    assert run_step(step, FakeLines("pressed", "released"), never).ok


def test_unexpected_line_is_rejected_even_if_it_looks_like_a_later_expect() -> None:
    step = Step(
        say="press once",
        expect=("pressed", "released"),
        reject="pressed|released|PANIC",
    )
    res = run_step(step, FakeLines("released", "pressed"), never)
    assert not res.ok
    assert "rejected" in res.detail


def test_extra_press_in_settle_is_rejected() -> None:
    step = Step(
        say="press once",
        expect=("pressed", "released"),
        reject="pressed|released|PANIC",
        settle=0.05,
    )
    res = run_step(step, FakeLines("pressed", "released", "pressed"), never)
    assert not res.ok
    assert "settling" in res.detail


def test_confirm_not_asked_is_unverified_not_pass() -> None:
    step = Step(say="listen", confirm="Did it play?")
    res = run_step(step, FakeLines(), lambda q: None)
    assert not res.ok
    assert res.unverified
    assert not res.counts_as_pass
    assert res.detail == "not asked (--yes)"


def test_optional_confirm_not_asked_still_unverified() -> None:
    step = Step(say="listen", confirm="Did it play?", optional=True)
    res = run_step(step, FakeLines(), lambda q: None)
    assert res.unverified
    assert res.optional  # recorded as unverified, but cannot hold the verdict back
    assert not res.failed


def test_failed_property() -> None:
    assert StepResult("s", False, "d").failed
    assert not StepResult("s", True, "d").failed
    assert not StepResult("s", False, "d", optional=True).failed
    assert not StepResult("s", False, "d", unverified=True).failed
    assert not StepResult("s", True, "d").unverified


def test_duration_fails_when_link_lost() -> None:
    lines = FakeLines()
    lines.lost = True
    step = Step(say="rotate", duration=0.3, reject="pressed")
    res = run_step(step, lines, never)
    assert not res.ok
    assert res.detail == "serial connection lost"
    assert lines.lost_since_args  # asked, with a monotonic start time
    assert all(t0 <= time.monotonic() for t0 in lines.lost_since_args)


def test_duration_loss_mid_window_fails() -> None:
    lines = FakeLines()
    calls = 0

    def lose_on_third_get() -> None:
        nonlocal calls
        calls += 1
        lines.lost = calls >= 3

    lines.on_get = lose_on_third_get
    step = Step(say="rotate", duration=1.0, reject="pressed")
    res = run_step(step, lines, never)
    assert not res.ok
    assert res.detail == "serial connection lost"


def test_duration_without_loss_still_passes() -> None:
    lines = FakeLines()
    step = Step(say="rotate", duration=0.3, reject="pressed")
    assert run_step(step, lines, never).ok


def test_settle_fails_when_link_lost_while_settling() -> None:
    lines = FakeLines(*PRESSES)

    def lose_once_backlog_is_consumed() -> None:
        lines.lost = not lines.queue

    lines.on_get = lose_once_backlog_is_consumed
    step = Step(say="press 3x", expect=("count=3",), reject="count=4", settle=0.5, timeout=1)
    res = run_step(step, lines, never)
    assert not res.ok
    assert res.detail == "serial connection lost while settling"


def test_expect_timeout_after_loss_says_so() -> None:
    lines = FakeLines()
    lines.lost = True
    step = Step(say="press", expect=("count=3",), timeout=0.3)
    res = run_step(step, lines, never)
    assert not res.ok
    assert "serial connection lost" in res.detail


def test_expect_timeout_without_loss_does_not_mention_it() -> None:
    step = Step(say="press", expect=("count=3",), timeout=0.3)
    res = run_step(step, FakeLines(), never)
    assert "timeout" in res.detail
    assert "lost" not in res.detail


def test_expect_matched_despite_loss_still_passes() -> None:
    lines = FakeLines("count=3")
    lines.lost = True
    assert run_step(Step(say="press", expect=("count=3",), timeout=1), lines, never).ok


def test_watch_without_reject_says_what_it_did() -> None:
    step = Step(say="watch", duration=0.01)
    res = run_step(step, FakeLines(), never)
    assert res.ok and res.detail == "watched for 0.01 s"
