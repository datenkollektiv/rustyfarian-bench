"""Setup, teardown and `run`: host commands around an example and inside a step.

The commands themselves are faked (`FakeCommands`); a shared timeline records the order in
which build, detection, serial, probe, commands and judging happen.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal, cast

import pytest

from rustyfarian_bench import runner, ui
from rustyfarian_bench.config import Scenario, Step
from rustyfarian_bench.device import Device
from rustyfarian_bench.serial_lines import LineSource
from rustyfarian_bench.steps import StepResult, run_step

from .fakes import (
    FakeCommands,
    FakeProbeLines,
    FakeProbes,
    Outcome,
    Timeline,
    failing,
    timing_out,
    unstartable,
)
from .test_runner import MAC, CountingLines, _rows, _session, _with_mac

if TYPE_CHECKING:  # only named in a cast: tests that never open a probe need not import it
    from rustyfarian_bench.probe_lines import ProbeLines

Judging = Literal["pass", "fail", "error", "ctrl-c", "real"]

_REAL_RUN_STEP = run_step


class TimedLines(CountingLines):
    """Serial lines that note on the timeline when they are closed."""

    def __init__(self, *lines: str, timeline: Timeline) -> None:
        super().__init__(*lines, timeline=timeline)
        self.events = timeline

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.events.append("serial-close")
        super().__exit__(et, ev, tb)


@dataclass
class Rig:
    session: runner.Session
    scenario: Scenario
    serial: TimedLines
    probes: FakeProbes
    commands: FakeCommands
    timeline: Timeline
    warnings: list[str]

    def run(self, example: str = "ex") -> runner.ExampleResult:
        return self.session.run_example(self.scenario, example)


def _hooked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    steps: tuple[Step, ...] = (Step(say="one", expect=("a",)),),
    setup: tuple[tuple[str, ...], ...] = (),
    teardown: tuple[tuple[str, ...], ...] = (),
    hook_timeout: float = 60.0,
    commands: FakeCommands | None = None,
    probes: FakeProbes | None = None,
    serial_lines: tuple[str, ...] = (),
    examples: tuple[str, ...] = ("ex",),
    mode: runner.Mode | None = None,
    yes: bool = True,
    judging: Judging = "pass",
    fail: Literal["build", "flash"] | None = None,
) -> Rig:
    """A Session whose build, flash, detection, serial, probe, commands and judging all log to
    one timeline. `judging` says what a step does: pass, fail, raise, Ctrl-C or the real thing."""
    timeline: Timeline = []
    serial = TimedLines(*serial_lines, timeline=timeline)
    commands = FakeCommands() if commands is None else commands
    commands.timeline = timeline
    probes = FakeProbes() if probes is None else probes
    probes.timeline = timeline
    warnings: list[str] = []

    session, scenario = _session(tmp_path, monkeypatch, serial, *examples, yes=yes)

    def fake_run(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
        kind: Literal["build", "flash"] = "flash" if "port" in subst else "build"
        timeline.append(kind)
        return kind != fail

    def fake_serial(port: str, log: Path, **kw: object) -> TimedLines:
        timeline.append("serial-open")
        return serial

    def fake_device(self: runner.Session, s: Scenario) -> Device:
        timeline.append("device")
        return Device("/dev/x", "esp32s3", None)

    def fake_run_step(
        step: Step,
        lines: LineSource,
        ask: Callable[[str], bool | None],
        duration: float | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> StepResult:
        timeline.append("judge")
        if judging == "error":
            raise RuntimeError("boom")
        if judging == "ctrl-c":
            raise KeyboardInterrupt
        if judging == "real":
            return _REAL_RUN_STEP(step, lines, ask, duration, stop)
        return StepResult(step.say, judging == "pass", "ok" if judging == "pass" else "nope")

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "SerialLines", fake_serial)
    monkeypatch.setattr(runner.Session, "_device_for", fake_device)
    monkeypatch.setattr(runner, "run_step", fake_run_step)
    monkeypatch.setattr(ui, "warn", warnings.append)

    session = runner.Session(
        session.cfg,
        session.opts,
        open_probe=cast("Callable[..., ProbeLines]", probes),
        run_command=commands,
    )
    session.opts.mode = mode
    scenario = replace(
        scenario, steps=steps, setup=setup, teardown=teardown, hook_timeout=hook_timeout
    )
    return Rig(session, scenario, serial, probes, commands, timeline, warnings)


def _between(timeline: Timeline, first: str, last: str) -> Timeline:
    return timeline[timeline.index(first) : timeline.index(last)]


# Ordering: setup before build and detection, teardown after everything else.


def test_setup_runs_before_build_and_detection_and_teardown_runs_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        setup=(("setup-a",), ("setup-b",)),
        teardown=(("td-x",), ("td-y",)),
    )

    result = rig.run()

    assert rig.timeline == [
        "setup-a",
        "setup-b",
        "build",
        "device",
        "flash",
        "serial-open",
        "judge",
        "serial-close",
        "td-x",
        "td-y",
    ]
    assert result.verdict == "pass"
    assert {c.cwd for c in rig.commands.calls} == {tmp_path}  # the consumer repo


def test_hooks_get_the_hook_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _hooked(tmp_path, monkeypatch, setup=(("setup-a",),), teardown=(("td-x",),))
    rig.run()
    assert [c.timeout for c in rig.commands.calls] == [60.0, 60.0]

    rig = _hooked(
        tmp_path, monkeypatch, setup=(("setup-a",),), teardown=(("td-x",),), hook_timeout=5.0
    )
    rig.run()
    assert [c.timeout for c in rig.commands.calls] == [5.0, 5.0]


def test_hooks_run_once_per_example_not_once_per_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        setup=(("up", "{example}"),),
        teardown=(("down", "{example}"),),
        examples=("ex1", "ex2"),
    )

    rig.session.run((rig.scenario,), None)

    assert rig.commands.argvs == [["up", "ex1"], ["down", "ex1"], ["up", "ex2"], ["down", "ex2"]]
    assert len(rig.session.results) == 2


def test_hook_placeholders_are_substituted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        setup=(("up", "{example}", "{board}"),),
        teardown=(("down", "{port}", "{chip}", "{example}", "{board}"),),
    )

    rig.run()

    assert rig.commands.argvs == [
        ["up", "ex", "s3"],
        ["down", "/dev/x", "esp32s3", "ex", "s3"],
    ]


# Setup failure.


@pytest.mark.parametrize(
    "outcome",
    [failing(2, "no broker"), timing_out(), unstartable("no such file: just")],
    ids=["non-zero", "timeout", "cannot-start"],
)
def test_a_failed_setup_is_an_error_with_no_build_no_flash_and_teardown_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: Outcome
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(outcome),
        setup=(("setup-a",),),
        teardown=(("td-x",),),
    )

    result = rig.run()

    assert result.verdict == "error"
    assert result.detail.startswith("setup failed: ")
    assert "setup-a" in result.detail
    assert rig.timeline == ["setup-a", "td-x"]  # no build, device, flash, serial or judging
    assert [row["verdict"] for row in _rows(tmp_path)] == ["error"]
    assert not rig.session.stopped


def test_a_failed_setup_does_not_stop_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(failing(1)),  # the first example's setup only
        setup=(("setup-a",),),
        examples=("ex1", "ex2"),
    )

    outcome = rig.session.run((rig.scenario,), None)

    assert [r.verdict for r in rig.session.results] == ["error", "pass"]
    assert outcome == "error"
    assert not rig.session.stopped


def test_a_failure_part_way_through_setup_skips_the_rest_but_runs_all_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(by_argv={"setup-b": failing(1, "nope")}),
        setup=(("setup-a",), ("setup-b",), ("setup-c",)),
        teardown=(("td-x",), ("td-y",)),
    )

    result = rig.run()

    assert rig.commands.names == ["setup-a", "setup-b", "td-x", "td-y"]
    assert [list(rec.argv) for rec in result.setup] == [["setup-a"], ["setup-b"]]
    assert result.verdict == "error"


# Teardown runs whatever happened.


@pytest.mark.parametrize(
    ("judging", "fail", "verdict"),
    [
        ("pass", None, "pass"),
        ("fail", None, "fail"),
        ("error", None, "error"),
        ("pass", "build", "fail"),
        ("pass", "flash", "fail"),
    ],
    ids=["pass", "step-fails", "step-raises", "build-fails", "flash-fails"],
)
def test_teardown_runs_after_every_way_an_example_can_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    judging: Judging,
    fail: Literal["build", "flash"] | None,
    verdict: str,
) -> None:
    rig = _hooked(tmp_path, monkeypatch, teardown=(("td-x",),), judging=judging, fail=fail)

    result = rig.run()

    assert result.verdict == verdict  # what it would be without a teardown
    assert rig.timeline[-1] == "td-x"
    assert [row["verdict"] for row in _rows(tmp_path)] == [verdict]


def test_teardown_runs_after_ctrl_c_in_a_step_and_once_the_ports_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(tmp_path, monkeypatch, teardown=(("td-x",),), judging="ctrl-c")

    result = rig.run()

    assert result.verdict == "aborted"
    assert rig.session.stopped
    assert rig.timeline[-2:] == ["serial-close", "td-x"]
    assert [row["verdict"] for row in _rows(tmp_path)] == ["aborted"]


def test_ctrl_c_during_setup_still_runs_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(interrupt_on=1),
        setup=(("setup-a",), ("setup-b",)),
        teardown=(("td-x",),),
    )

    result = rig.run()

    assert result.verdict == "aborted"
    assert rig.session.stopped
    assert rig.timeline == ["setup-a", "td-x"]  # no build, and setup-b never started


@pytest.mark.parametrize("judging", ["ctrl-c", "pass"])
def test_a_second_ctrl_c_during_teardown_skips_the_rest_and_stops_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, judging: Judging
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(by_argv={"td-x": Outcome(raises=KeyboardInterrupt())}),
        teardown=(("td-x",), ("td-y",)),
        judging=judging,
    )

    result = rig.run()  # must not raise out of run_example

    assert rig.commands.names == ["td-x"]  # td-y is never started
    assert rig.session.stopped
    assert [list(rec.argv) for rec in result.teardown] == [["td-x"], ["td-y"]]
    assert all("interrupted" in rec.error for rec in result.teardown)
    assert len(_rows(tmp_path)) == 1
    if judging == "ctrl-c":
        assert result.verdict == "aborted"


def test_a_failing_teardown_is_a_warning_and_not_a_verdict_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(by_argv={"td-x": failing(1, "broker busy"), "td-y": timing_out()}),
        teardown=(("td-x",), ("td-y",), ("td-z",)),
    )

    result = rig.run()

    assert result.verdict == "pass"
    assert rig.commands.names == ["td-x", "td-y", "td-z"]  # one failing does not skip the rest
    assert [rec.ok for rec in result.teardown] == [False, False, True]
    assert any("td-x" in w for w in rig.warnings)
    assert any("td-y" in w for w in rig.warnings)


def test_a_failing_teardown_leaves_a_failed_example_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(by_argv={"td-x": failing(1)}),
        teardown=(("td-x",),),
        judging="fail",
    )

    assert rig.run().verdict == "fail"


@pytest.mark.parametrize("how", ["setup-fails", "build-fails"])
def test_a_teardown_command_needing_a_port_that_was_never_found_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(by_argv={"setup-a": failing(1)} if how == "setup-fails" else {}),
        setup=(("setup-a",),),
        teardown=(("td-port", "--port", "{port}"), ("td-x",)),
        fail="build" if how == "build-fails" else None,
    )
    rig.session.device = Device("/dev/cached", "esp32s3", None)  # an earlier example's board

    result = rig.run()

    assert "td-port" not in rig.commands.names  # nothing is run against the wrong port
    assert "td-x" in rig.commands.names  # the other teardown commands still are
    skipped, ran = result.teardown
    assert skipped.argv[0] == "td-port"
    assert skipped.error == "not run: {port} unknown"
    assert ran.ok
    assert any("td-port" in w or "{port}" in w for w in rig.warnings)
    assert result.verdict == ("error" if how == "setup-fails" else "fail")


def test_teardown_uses_this_examples_port_not_a_cached_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(tmp_path, monkeypatch, teardown=(("down", "{port}"),))
    rig.session.device = Device("/dev/cached", "esp32s3", None)

    rig.run()

    assert rig.commands.argvs == [["down", "/dev/x"]]


# Which examples get hooks.


def test_a_scenario_the_mode_drops_runs_no_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path, monkeypatch, setup=(("setup-a",),), teardown=(("td-x",),), mode="attended"
    )  # its only step needs no person

    outcome = rig.session.run((rig.scenario,), None)

    assert outcome == "fail"  # nothing to run
    assert rig.commands.calls == []
    assert rig.session.results == []


def test_a_declined_example_runs_no_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _hooked(tmp_path, monkeypatch, setup=(("setup-a",),), teardown=(("td-x",),))

    rig.session._skip(rig.scenario, ["ex"])

    assert rig.commands.calls == []


def test_a_scenario_the_mode_keeps_runs_its_hooks_once_per_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (Step(say="boot", expect=("a",)), Step(say="look", confirm="ok?"))
    rig = _hooked(
        tmp_path,
        monkeypatch,
        steps=steps,
        setup=(("setup-a",),),
        teardown=(("td-x",),),
        mode="attended",
    )

    rig.session.run((rig.scenario,), None)

    assert rig.commands.names == ["setup-a", "td-x"]


# Evidence.


def test_setup_and_teardown_evidence_is_on_the_example_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(
        tmp_path,
        monkeypatch,
        commands=FakeCommands(
            Outcome(stdout=("mosquitto up",), duration=1.5),
            Outcome(exit_code=0, stderr=("slow",)),
            Outcome(exit_code=1, stderr=("still running",)),
        ),
        setup=(("setup-a", "{example}"), ("setup-b",)),
        teardown=(("td-x",),),
    )

    result = rig.run()

    first, second = result.setup
    assert list(first.argv) == ["setup-a", "ex"]
    assert (first.exit_code, first.duration, list(first.stdout_tail)) == (0, 1.5, ["mosquitto up"])
    assert list(second.stderr_tail) == ["slow"]
    (down,) = result.teardown
    assert list(down.argv) == ["td-x"]
    assert down.exit_code == 1
    assert list(down.stderr_tail) == ["still running"]


def test_a_result_without_hooks_has_empty_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _hooked(tmp_path, monkeypatch).run()

    assert result.setup == []
    assert result.teardown == []


def test_command_output_goes_to_logs_beside_the_serial_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", probe=("watch",), run=("run-x",), expect=("online",), timeout=0.3)
    rig = _hooked(
        tmp_path,
        monkeypatch,
        steps=(step,),
        setup=(("setup-a",), ("setup-b",)),
        teardown=(("td-x",),),
        probes=FakeProbes(FakeProbeLines("online")),
    )

    result = rig.run()

    assert result.log is not None
    serial_log = tmp_path / result.log
    stem = serial_log.stem
    assert [c.log_path for c in rig.commands.calls] == [
        serial_log.with_name(f"{stem}.setup1.log"),
        serial_log.with_name(f"{stem}.setup2.log"),
        serial_log.with_name(f"{stem}.run1.log"),
        serial_log.with_name(f"{stem}.teardown1.log"),
    ]
    assert [c.log_path for c in rig.probes.calls] == [serial_log.with_name(f"{stem}.probe1.log")]


# run: a host command a step starts before it judges.


def test_run_executes_after_arming_and_reset_and_before_judging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui, "ask", lambda q, default=True: True)
    step = Step(say="act", run=("run-x",), reset=True, expect=("a",))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), yes=False)
    monkeypatch.setattr(ui, "ready", lambda msg: rig.timeline.append("ready"))

    rig.run()

    assert _between(rig.timeline, "ready", "serial-close") == ["ready", "reset", "run-x", "judge"]


def test_a_run_duration_step_ignores_serial_lines_queued_before_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x",), duration=0.1, reject="PANIC", arm=False, timeout=0.3)
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), serial_lines=("PANIC",), judging="real")

    result = rig.run()

    assert result.verdict == "pass"


def test_a_run_duration_step_rejects_a_serial_line_produced_during_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x",), duration=0.1, reject="PANIC", arm=False, timeout=0.3)
    commands = FakeCommands()
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), commands=commands, judging="real")
    commands.outcomes.append(Outcome(effect=lambda: rig.serial.queue.append("PANIC")))

    result = rig.run()

    assert result.verdict == "fail"
    assert "PANIC" in result.steps[0].detail


def test_with_a_probe_the_probe_starts_before_run_and_both_before_judging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", probe=("watch",), run=("run-x",), expect=("online",), timeout=0.3)
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), probes=FakeProbes(FakeProbeLines()))

    rig.run()

    assert _between(rig.timeline, "serial-open", "serial-close") == [
        "serial-open",
        "probe-open",
        "run-x",
        "judge",
    ]


def test_what_run_causes_is_what_the_probe_step_judges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = FakeProbeLines()
    commands = FakeCommands(Outcome(effect=lambda: probe.queue.append("online")))
    step = Step(say="act", probe=("watch",), run=("run-x",), expect=("online",), timeout=0.3)
    rig = _hooked(
        tmp_path,
        monkeypatch,
        steps=(step,),
        commands=commands,
        probes=FakeProbes(probe),
        judging="real",
    )

    result = rig.run()

    assert result.verdict == "pass"
    assert result.steps[0].detail == "matched online"


def test_run_with_a_probe_leaves_the_serial_backlog_and_board_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(
        say="act", probe=("watch",), run=("run-x",), reset=True, expect=("online",), timeout=0.3
    )
    rig = _hooked(
        tmp_path,
        monkeypatch,
        steps=(step,),
        serial_lines=("old line",),
        probes=FakeProbes(FakeProbeLines("online")),
        judging="real",
    )

    result = rig.run()

    assert result.verdict == "pass"
    assert rig.serial.drained == 0
    assert rig.serial.resets == 0


@pytest.mark.parametrize(
    ("outcome", "fragments"),
    [
        (failing(3, "connection refused"), ["exit 3", "connection refused"]),
        (timing_out(), ["timed out"]),
        (unstartable("no such file: run-x"), ["no such file: run-x"]),
    ],
    ids=["non-zero", "timeout", "cannot-start"],
)
def test_a_failing_run_fails_the_step_before_judging_with_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: Outcome, fragments: list[str]
) -> None:
    step = Step(say="act", run=("run-x",), expect=("a",))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), commands=FakeCommands(outcome))

    result = rig.run()

    res = result.steps[0]
    assert result.verdict == "fail"
    assert "judge" not in rig.timeline
    assert res.detail.startswith("run failed: ")
    assert "run-x" in res.detail
    for fragment in fragments:
        assert fragment in res.detail
    assert res.run is not None
    assert not res.run.ok
    assert not rig.session.stopped  # a failed action is a failed step, not a missing tool


def test_a_failing_run_with_a_probe_still_tears_the_probe_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", probe=("watch",), run=("run-x",), expect=("online",), timeout=0.3)
    rig = _hooked(
        tmp_path, monkeypatch, steps=(step,), commands=FakeCommands(failing(1, "refused"))
    )

    result = rig.run()

    assert result.verdict == "fail"
    assert [p.exited for p in rig.probes.opened] == [1]
    assert "judge" not in rig.timeline


def _retry_once(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Interactive answers: retry the failed step once, then give up; everything else is no."""
    asked: list[str] = []
    answers = iter([True, False])

    def ask(question: str, default: bool = True) -> bool:
        asked.append(question)
        return next(answers) if question == "Retry this step?" else False

    monkeypatch.setattr(ui, "ask", ask)
    monkeypatch.setattr(ui, "ready", lambda msg: None)
    monkeypatch.setattr(ui, "note", lambda question: "")
    return asked


def test_a_retry_runs_the_command_again_and_keeps_the_last_attempts_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x",), expect=("a",))
    commands = FakeCommands(failing(1, "first"), failing(1, "second"))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), commands=commands, yes=False)
    asked = _retry_once(monkeypatch)

    result = rig.run()

    assert asked.count("Retry this step?") == 2
    assert commands.names == ["run-x", "run-x"]
    (res,) = result.steps
    assert res.run is not None
    assert list(res.run.stderr_tail) == ["second"]


def test_assume_yes_does_not_retry_a_failing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x",), expect=("a",))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), commands=FakeCommands(failing(1)))

    rig.run()

    assert rig.commands.names == ["run-x"]


def test_run_timeout_is_passed_to_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="default", run=("run-a",), expect=("a",)),
        Step(say="short", run=("run-b",), expect=("a",), run_timeout=5.0),
    )
    rig = _hooked(tmp_path, monkeypatch, steps=steps)

    rig.run()

    assert [(c.argv[0], c.timeout) for c in rig.commands.calls] == [("run-a", 30.0), ("run-b", 5.0)]


def test_run_placeholders_take_the_device_the_example_and_earlier_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="learn", expect=(r"client (?P<cid>\w+)",), arm=False, timeout=0.3),
        Step(
            say="act",
            run=("run-x", "{port}", "{example}", "{board}", "{chip}", "{cid}"),
            duration=0.1,
        ),
    )
    rig = _hooked(tmp_path, monkeypatch, steps=steps, serial_lines=("client abc1",), judging="real")

    result = rig.run()

    assert result.verdict == "pass"
    assert rig.commands.argvs == [["run-x", "/dev/x", "ex", "s3", "esp32s3", "abc1"]]


def test_a_run_placeholder_nothing_captured_fails_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x", "{cid}"), expect=("a",))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,))

    result = rig.run()

    assert result.verdict == "fail"
    assert result.steps[0].detail.startswith("nothing captured for cid")
    assert rig.commands.calls == []


@pytest.mark.parametrize(
    ("attended", "mode", "runs"),
    [
        (False, "unattended", True),
        (False, "attended", False),
        (True, "unattended", False),
        (True, "attended", True),
    ],
)
def test_a_run_step_follows_the_mode_like_any_other_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attended: bool, mode: runner.Mode, runs: bool
) -> None:
    steps = (
        Step(say="act", run=("run-x",), expect=("a",), attended=attended),
        Step(say="boot", expect=("a",)),
        Step(say="look", confirm="ok?"),
    )
    rig = _hooked(tmp_path, monkeypatch, steps=steps, mode=mode)

    rig.run()

    assert rig.commands.names == (["run-x"] if runs else [])


def test_run_evidence_lands_on_the_step_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="act", run=("run-x",), expect=("a",)),
        Step(say="plain", expect=("a",)),
    )
    commands = FakeCommands(Outcome(stdout=("published",), stderr=("warn",)))
    rig = _hooked(tmp_path, monkeypatch, steps=steps, commands=commands)

    acted, plain = rig.run().steps

    assert acted.run is not None
    assert list(acted.run.argv) == ["run-x"]
    assert acted.run.exit_code == 0
    assert acted.run.duration >= 0
    assert list(acted.run.stdout_tail) == ["published"]
    assert list(acted.run.stderr_tail) == ["warn"]
    assert plain.run is None


# results.jsonl: the new fields are additive.

OLD_ROW_KEYS = {
    "example", "scenario", "board", "started", "port", "chip", "mac", "log", "verdict",
    "detail", "steps", "mode",
}  # fmt: skip
OLD_STEP_KEYS = {
    "say", "ok", "detail", "optional", "unverified", "note", "captured", "evidence",
}  # fmt: skip
RECORD_KEYS = {"argv", "exit_code", "duration", "timed_out", "error", "stdout_tail", "stderr_tail"}


def test_results_jsonl_carries_the_new_fields_and_keeps_the_old_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (Step(say="act", run=("run-x",), expect=("a",)), Step(say="plain", expect=("a",)))
    commands = FakeCommands(by_argv={"run-x": Outcome(stdout=("up",))})
    rig = _hooked(
        tmp_path,
        monkeypatch,
        steps=steps,
        commands=commands,
        setup=(("setup-a",),),
        teardown=(("td-x",),),
    )

    rig.run()

    [row] = _rows(tmp_path)
    assert row.keys() >= OLD_ROW_KEYS
    assert row.keys() >= {"setup", "teardown"}
    setup, teardown = (
        cast(list[dict[str, object]], row["setup"]),
        cast(list[dict[str, object]], row["teardown"]),
    )
    assert [rec["argv"] for rec in setup] == [["setup-a"]]
    assert [rec["argv"] for rec in teardown] == [["td-x"]]
    assert setup[0].keys() >= RECORD_KEYS
    acted, plain = cast(list[dict[str, object]], row["steps"])
    assert acted.keys() >= OLD_STEP_KEYS
    assert plain.keys() >= OLD_STEP_KEYS
    run = cast(dict[str, object], acted["run"])
    assert run.keys() >= RECORD_KEYS
    assert run["argv"] == ["run-x"]
    assert run["stdout_tail"] == ["up"]
    assert plain["run"] is None
    json.dumps(row)  # round-trips


def test_results_jsonl_has_empty_hooks_and_no_run_for_a_scenario_without_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hooked(tmp_path, monkeypatch).run()

    [row] = _rows(tmp_path)
    assert row["setup"] == []
    assert row["teardown"] == []
    assert cast(list[dict[str, object]], row["steps"])[0]["run"] is None


# {mac}: reserved for run, probe and teardown; a prompt only gets a marker without one.


def test_a_run_with_mac_unknown_fails_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x", "{mac}"), expect=("a",))
    rig = _hooked(tmp_path, monkeypatch, steps=(step,))

    result = rig.run()

    assert result.verdict == "fail"
    assert result.steps[0].detail == "no value for {mac}: the board's MAC address is unknown"
    assert rig.commands.calls == []


def test_run_placeholders_include_the_detected_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="act", run=("run-x", "{port}", "{mac}"), duration=0.1)
    rig = _hooked(tmp_path, monkeypatch, steps=(step,), judging="real")
    _with_mac(monkeypatch)

    result = rig.run()

    assert result.verdict == "pass"
    assert rig.commands.argvs == [["run-x", "/dev/x", MAC]]


def test_a_run_command_never_takes_a_capture_named_mac_for_a_missing_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="learn", expect=(r"mac (?P<mac>\w+)",), arm=False, timeout=0.3),
        Step(say="act", run=("run-x", "{mac}"), expect=("a",)),
    )
    rig = _hooked(tmp_path, monkeypatch, steps=steps, serial_lines=("mac zz",), judging="real")

    result = rig.run()

    assert result.steps[1].detail == "no value for {mac}: the board's MAC address is unknown"
    assert rig.commands.calls == []


def test_teardown_mac_unknown_is_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _hooked(tmp_path, monkeypatch, teardown=(("td-mac", "{mac}"), ("td-x",)))

    result = rig.run()

    assert "td-mac" not in rig.commands.names
    assert "td-x" in rig.commands.names  # the other teardown commands still are
    skipped, ran = result.teardown
    assert skipped.argv[0] == "td-mac"
    assert skipped.error == "not run: {mac} unknown"
    assert ran.ok
    assert any("td-mac" in w or "{mac}" in w for w in rig.warnings)
    assert result.verdict == "pass"


def test_teardown_substitutes_the_mac_when_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _hooked(tmp_path, monkeypatch, teardown=(("down", "{mac}"),))
    _with_mac(monkeypatch)

    result = rig.run()

    assert rig.commands.argvs == [["down", MAC]]
    assert all(rec.ok for rec in result.teardown)
    assert rig.warnings == []


def test_setup_hooks_never_get_a_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _hooked(tmp_path, monkeypatch, setup=(("up", "{example}", "{board}"),))
    _with_mac(monkeypatch)

    rig.run()

    assert rig.commands.argvs == [["up", "ex", "s3"]]
    assert all(MAC not in part for argv in rig.commands.argvs for part in argv)


def test_a_prompt_and_a_probe_share_one_field_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="learn", expect=(r"client (?P<cid>\w+)",), arm=False, timeout=0.3),
        Step(say="watch {cid}", probe=("watch", "{cid}"), expect=("online",), timeout=0.3),
    )
    probes = FakeProbes(FakeProbeLines("online"))
    rig = _hooked(
        tmp_path, monkeypatch, steps=steps, probes=probes, serial_lines=("client abc1",),
        judging="real",
    )  # fmt: skip

    result = rig.run()

    assert result.verdict == "pass"
    assert [c.argv for c in probes.calls] == [["watch", "abc1"]]
    assert result.steps[1].say == "watch abc1"
    assert result.steps[1].lacked == ()
