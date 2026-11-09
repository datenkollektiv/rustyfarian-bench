from __future__ import annotations

import re
import time

import pytest

from rustyfarian_bench.config import Step
from rustyfarian_bench.steps import StepResult, run_step

from .fakes import FakeLines, FakeProbeLines


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


def test_duration_override_watches_the_chosen_length_and_notes_the_scenario_one() -> None:
    step = Step(say="rotate", duration=0.3, reject="pressed")
    res = run_step(step, FakeLines(), never, duration=0.1)
    assert res.ok
    assert "0.1 s" in res.detail
    assert res.detail.endswith("(scenario: 0.3 s)")


@pytest.mark.parametrize("override", [None, 0.3])
def test_duration_override_equal_to_the_scenario_has_no_suffix(override: float | None) -> None:
    step = Step(say="rotate", duration=0.3, reject="pressed")
    res = run_step(step, FakeLines(), never, duration=override)
    assert res.ok
    assert "scenario" not in res.detail


def test_duration_override_suffix_is_on_failures_too() -> None:
    step = Step(say="rotate", duration=0.3, reject="pressed")
    lines = FakeLines()
    lines.on_get = lambda: lines.queue.append("t=1 pressed")  # arrives after the drain
    res = run_step(step, lines, never, duration=0.1)
    assert not res.ok
    assert res.detail.endswith("(scenario: 0.3 s)")


def test_duration_override_is_ignored_by_expect_steps() -> None:
    step = Step(say="press", expect=("count=3",), timeout=0.3)
    res = run_step(step, FakeLines(*PRESSES), never, duration=0.1)
    assert res.ok
    assert "scenario" not in res.detail


def test_attended_watch_ends_early_when_the_operator_stops_it() -> None:
    step = Step(say="watch", duration=30, reject="PANIC", attended=True)
    t0 = time.monotonic()
    res = run_step(step, FakeLines(), never, stop=lambda: True)
    assert res.ok
    assert res.detail.startswith("ended by operator after 0 s of 30 s")
    assert time.monotonic() - t0 < 5


def test_a_reject_before_the_stop_still_fails_the_watch() -> None:
    step = Step(say="watch", duration=30, reject="PANIC", attended=True)
    lines = FakeLines()
    polls = iter([False, True])

    def feed() -> None:
        if lines.on_get is not None:
            lines.queue.append("PANIC: boom")
            lines.on_get = None

    lines.on_get = feed
    res = run_step(step, lines, never, stop=lambda: next(polls, True))
    assert not res.ok
    assert "PANIC: boom" in res.detail


def test_a_reject_queued_behind_the_stop_still_fails_the_watch() -> None:
    step = Step(say="watch", duration=30, reject="PANIC", attended=True)
    lines = FakeLines()

    def feed() -> None:  # after the step's drain: both lines arrive before Enter
        if lines.on_get is not None:
            lines.queue.extend(["normal", "PANIC: boom"])
            lines.on_get = None

    lines.on_get = feed
    res = run_step(step, lines, never, stop=lambda: True)
    assert not res.ok
    assert "PANIC: boom" in res.detail


def test_an_early_stop_notes_a_changed_scenario_length() -> None:
    step = Step(say="watch", duration=60, attended=True)
    res = run_step(step, FakeLines(), never, duration=30, stop=lambda: True)
    assert res.ok and res.detail.endswith("(scenario: 60 s)")


def test_named_groups_are_captured_and_noted() -> None:
    step = Step(say="pole", expect=(r"dev=\d+\s+(?P<pole>SOUTH|NORTH)",), timeout=0.3)
    res = run_step(step, FakeLines("dev=1  ---", "dev=120  NORTH"), never)
    assert res.ok
    assert res.captured == {"pole": "NORTH"}
    assert res.detail.endswith("(pole=NORTH)")


def test_each_expect_records_the_line_it_matched() -> None:
    step = Step(say="press", expect=("count=1", "count=3"), timeout=0.3)
    res = run_step(step, FakeLines(*PRESSES), never)
    assert res.evidence == ("t=10 ms  pressed  (count=1)", "t=600 ms  pressed  (count=3)")


def test_a_settle_failure_keeps_the_evidence_it_had() -> None:
    step = Step(say="hold", expect=("count=3",), reject=r"count=[4-9]", settle=0.3)
    res = run_step(step, FakeLines(*PRESSES, "t=900 ms  pressed  (count=4)"), never)
    assert not res.ok
    assert res.evidence == ("t=600 ms  pressed  (count=3)",)


def test_serial_lines_name_the_loss_as_a_serial_connection() -> None:
    assert FakeLines.lost_text == "serial connection lost"


def test_a_lost_probe_reads_the_sources_own_text_in_a_duration_step() -> None:
    lines = FakeProbeLines()
    lines.lost = True
    res = run_step(Step(say="watch", duration=0.3), lines, never)
    assert not res.ok
    assert res.detail == "probe command exited"


def test_a_lost_probe_reads_the_sources_own_text_while_settling() -> None:
    lines = FakeProbeLines("count=3")

    def lose_once_backlog_is_consumed() -> None:
        lines.lost = not lines.queue

    lines.on_get = lose_once_backlog_is_consumed
    step = Step(say="watch", expect=("count=3",), reject="count=4", settle=0.5, timeout=1)
    res = run_step(step, lines, never)
    assert not res.ok
    assert res.detail == "probe command exited while settling"


def test_a_lost_probe_reads_the_sources_own_text_after_an_expect_timeout() -> None:
    lines = FakeProbeLines()
    lines.lost = True
    res = run_step(Step(say="watch", expect=("count=3",), timeout=0.3), lines, never)
    assert not res.ok
    assert "timeout" in res.detail
    assert "(probe command exited)" in res.detail
    assert "serial" not in res.detail


def test_duration_judges_queued_lines_before_reporting_a_lost_link() -> None:
    lines = FakeLines("ok", "ERROR")
    lines.lost = True
    step = Step(say="probe", duration=0.3, reject="ERROR", reset=True)
    res = run_step(step, lines, never)
    assert not res.ok
    assert res.detail == "rejected line: 'ERROR'"


# An attended probe: the operator ends its duration with Enter, like an attended serial watch.


def test_an_attended_probe_duration_ends_early_on_enter() -> None:
    step = Step(say="unplug", probe=("watch",), duration=5, attended=True)
    res = run_step(step, FakeProbeLines(), never, stop=lambda: True)
    assert res.ok
    assert "ended by operator after" in res.detail


def test_a_probe_line_seen_before_the_stop_still_fails() -> None:
    step = Step(say="unplug", probe=("watch",), duration=5, attended=True, reject="refused")
    res = run_step(step, FakeProbeLines("refused"), never, stop=lambda: True)
    assert not res.ok
    assert "refused" in res.detail


# run: a step that starts a host command skips the duration drain, so the board's reaction to
# the action is judged.


def test_a_duration_step_drops_the_backlog_first() -> None:
    lines = FakeLines("old")
    res = run_step(Step(say="quiet", duration=0.2), lines, never)
    assert res.ok
    assert lines.drained == 1


def test_a_duration_step_with_run_keeps_the_backlog() -> None:
    lines = FakeLines("PANIC before the action", echo=False)
    step = Step(say="react", run=("pub",), duration=0.2, reject="PANIC")
    res = run_step(step, lines, never)
    assert lines.drained == 0
    assert not res.ok
    assert "PANIC" in res.detail


# GuardedLines: a probe step's lines, with the serial lines watched for a pattern meanwhile.

PANIC = re.compile("PANIC|panicked")


def test_guarded_lines_leave_the_serial_backlog_to_the_caller_to_drain() -> None:
    from rustyfarian_bench.steps import GuardedLines, SerialRejected

    serial = FakeLines("PANIC from the probe's first moments")
    guarded = GuardedLines(FakeProbeLines("online"), serial, PANIC)
    assert serial.drained == 0
    assert list(serial.queue) == ["PANIC from the probe's first moments"]
    with pytest.raises(SerialRejected):
        guarded.check()


def test_guarded_lines_pass_the_probe_lines_through() -> None:
    from rustyfarian_bench.steps import GuardedLines

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines("one", "two"), serial, PANIC)
    assert guarded.get(0.1) == "one"
    assert guarded.get(0.1) == "two"
    assert guarded.get(0.1) is None


def test_guarded_lines_ignore_serial_lines_that_do_not_match() -> None:
    from rustyfarian_bench.steps import GuardedLines

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines("online"), serial, PANIC)
    serial.queue.append("boot ok")
    assert guarded.get(0.1) == "online"
    assert list(serial.queue) == []  # looked at, judged fine, gone


def test_guarded_lines_raise_with_the_offending_serial_line() -> None:
    from rustyfarian_bench.steps import GuardedLines, SerialRejected

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines("online"), serial, PANIC)
    serial.queue.append("thread panicked at main.rs")
    with pytest.raises(SerialRejected) as caught:
        guarded.get(0.1)
    assert caught.value.line == "thread panicked at main.rs"


def test_guarded_lines_raise_out_of_an_expect_step() -> None:
    from rustyfarian_bench.steps import GuardedLines, SerialRejected

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines(), serial, PANIC)
    serial.queue.append("PANIC: boom")
    step = Step(say="watch", probe=("watch",), expect=("online",), timeout=1)
    with pytest.raises(SerialRejected) as caught:
        run_step(step, guarded, never)
    assert caught.value.line == "PANIC: boom"


def test_guarded_lines_raise_out_of_a_duration_step() -> None:
    from rustyfarian_bench.steps import GuardedLines, SerialRejected

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines(), serial, PANIC)
    serial.queue.append("PANIC: boom")
    step = Step(say="watch", probe=("watch",), duration=1)
    with pytest.raises(SerialRejected):
        run_step(step, guarded, never)


def test_guarded_lines_raise_while_the_step_settles() -> None:
    from rustyfarian_bench.steps import GuardedLines, SerialRejected

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines("online"), serial, PANIC)
    seen: list[int] = []

    def late_panic() -> None:  # the probe matches first; the board panics during settle
        if seen:
            serial.queue.append("PANIC: late")
        seen.append(1)

    serial.on_get = late_panic
    step = Step(say="watch", probe=("watch",), expect=("online",), settle=1, timeout=1)
    with pytest.raises(SerialRejected) as caught:
        run_step(step, guarded, never)
    assert caught.value.line == "PANIC: late"


def test_guarded_lines_are_a_no_op_while_the_serial_side_is_quiet() -> None:
    from rustyfarian_bench.steps import GuardedLines

    serial = FakeLines()
    guarded = GuardedLines(FakeProbeLines("online"), serial, PANIC)
    step = Step(say="watch", probe=("watch",), expect=("online",), timeout=1)
    assert run_step(step, guarded, never).ok


def test_guarded_lines_speak_for_the_probe_when_it_ends() -> None:
    from rustyfarian_bench.steps import GuardedLines

    probe = FakeProbeLines()
    probe.lost = True
    guarded = GuardedLines(probe, FakeLines(), PANIC)
    assert guarded.lost_text == "probe command exited"
    assert guarded.link_lost_since(1.5) is True
    assert probe.lost_since_args == [1.5]
    guarded.drain()
    assert probe.drained == 1
