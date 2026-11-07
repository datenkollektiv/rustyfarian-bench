from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rustyfarian_bench import device, runner, ui
from rustyfarian_bench.config import BenchConfig, Board, Scenario, Step
from rustyfarian_bench.device import Device
from rustyfarian_bench.steps import StepResult

from .fakes import FakeLines

_REAL_RUN = runner._run


def test_assume_yes_does_not_retry_a_failed_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    steps = (Step(say="one", expect=("a",)), Step(say="two", expect=("b",)))
    scenario = Scenario("chk", board, ("ex",), steps, tmp_path / "bench/s3/chk.toml")
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    judged: list[str] = []

    def fake_run_step(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        judged.append(step.say)
        return StepResult(step.say, False, "nope")

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: FakeLines())
    monkeypatch.setattr(runner, "run_step", fake_run_step)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )

    session = runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=True))
    result = session.run_example(scenario, "ex")

    assert judged == ["one"]  # failed once, not retried; "Continue?" defaults to no
    assert result.verdict == "fail"


def test_device_echo_is_muted_while_a_question_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("c3", "esp32c3", "C3")
    scenario = Scenario(
        "chk", board, ("ex",), (Step(say="look", confirm="ok?"),), tmp_path / "chk.toml"
    )
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"c3": board}, (scenario,))
    lines = FakeLines()
    echo_during_ask: list[bool] = []

    def fake_ask(question: str, default: bool) -> bool:
        echo_during_ask.append(lines.echo)
        return True

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: lines)
    monkeypatch.setattr(ui, "ask", fake_ask)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32c3", None)
    )

    result = runner.Session(cfg, runner.Options(repo=tmp_path)).run_example(scenario, "ex")

    assert echo_during_ask == [False]
    assert lines.echo is True
    assert result.verdict == "pass"


def test_armed_steps_wait_for_enter_and_drop_the_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    steps = (
        Step(say="banner", expect=("ready",), arm=False),
        Step(say="press", expect=("pressed",)),
    )
    scenario = Scenario("chk", board, ("ex",), steps, tmp_path / "chk.toml")
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    events: list[str] = []

    class Lines(FakeLines):
        def drain(self) -> None:
            events.append("drain")

    lines = Lines()
    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: lines)
    monkeypatch.setattr(ui, "ready", lambda msg: events.append(f"ready:{msg}"))
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask, duration=None: StepResult(step.say, True, "ok")
    )
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )

    result = runner.Session(cfg, runner.Options(repo=tmp_path)).run_example(scenario, "ex")

    assert events == ["ready:press", "drain"]
    assert result.verdict == "pass"


def test_a_failed_step_records_the_operator_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    scenario = Scenario(
        "chk", board, ("ex",), (Step(say="look", confirm="ok?"),), tmp_path / "chk.toml"
    )
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    lines = FakeLines()
    echo_during_note: list[bool] = []

    def fake_note(question: str) -> str:
        echo_during_note.append(lines.echo)
        return "only one LED pulsed"

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: lines)
    monkeypatch.setattr(ui, "ask", lambda question, default: False)
    monkeypatch.setattr(ui, "note", fake_note)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )

    result = runner.Session(cfg, runner.Options(repo=tmp_path)).run_example(scenario, "ex")

    assert result.verdict == "fail"
    assert result.steps[0].note == "only one LED pulsed"
    assert echo_during_note == [False]
    recorded = (tmp_path / "bench/s3/results.jsonl").read_text()
    assert '"note": "only one LED pulsed"' in recorded


def test_assume_yes_does_not_ask_for_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    scenario = Scenario(
        "chk", board, ("ex",), (Step(say="one", expect=("a",)),), tmp_path / "chk.toml"
    )
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))

    def no_note(question: str) -> str:
        raise AssertionError("asked for a note under -y")

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: FakeLines())
    monkeypatch.setattr(
        runner,
        "run_step",
        lambda step, lines, ask, duration=None: StepResult(step.say, False, "nope"),
    )
    monkeypatch.setattr(ui, "note", no_note)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )

    session = runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=True))
    result = session.run_example(scenario, "ex")

    assert result.steps[0].note == ""


def _session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: FakeLines,
    *examples: str,
    yes: bool = True,
) -> tuple[runner.Session, Scenario]:
    board = Board("s3", "esp32s3", "S3")
    scenario = Scenario(
        "chk", board, examples or ("ex",), (Step(say="one", expect=("a",)),), tmp_path / "chk.toml"
    )
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: lines)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )
    return runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=yes)), scenario


def _rows(tmp_path: Path) -> list[dict[str, object]]:
    text = (tmp_path / "bench/s3/results.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("just"),
        PermissionError("denied"),
        KeyError("port"),
        RuntimeError("boom"),
        re.error("bad pattern"),
    ],
)
def test_unexpected_exception_in_a_step_records_an_error_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines)

    def raising(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        raise exc

    monkeypatch.setattr(runner, "run_step", raising)

    result = session.run_example(scenario, "ex")  # must not raise

    assert result.verdict == "error"
    assert type(exc).__name__ in result.detail
    assert str(exc) in result.detail
    assert lines.exited == 1  # the port is released
    [row] = _rows(tmp_path)
    assert row["verdict"] == "error"
    assert row["detail"] == result.detail


@pytest.mark.parametrize("exc", [FileNotFoundError("just"), PermissionError("denied")])
def test_a_missing_build_tool_is_an_error_verdict_with_a_readable_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines())

    def raising(*a: object, **k: object) -> None:
        raise exc

    monkeypatch.setattr("rustyfarian_bench.runner.subprocess.run", raising)
    monkeypatch.setattr(runner, "_run", _REAL_RUN)

    result = session.run_example(scenario, "ex")

    assert result.verdict == "error"
    assert "cannot run 'true'" in result.detail
    assert [r["verdict"] for r in _rows(tmp_path)] == ["error"]


def test_keyboard_interrupt_is_still_aborted_and_releases_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines)

    def interrupted(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_step", interrupted)

    result = session.run_example(scenario, "ex")

    assert result.verdict == "aborted"
    assert lines.exited == 1
    assert [r["verdict"] for r in _rows(tmp_path)] == ["aborted"]


def test_ctrl_c_ends_the_session_even_with_assume_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines, "ex1", "ex2")
    built: list[str] = []

    def record_build(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
        built.append(subst["example"])
        return True

    def interrupted(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_run", record_build)
    monkeypatch.setattr(runner, "run_step", interrupted)

    outcome = session.run((scenario, replace(scenario, name="next")), None)

    assert "ex2" not in built
    assert [r.verdict for r in session.results] == ["aborted"]
    assert [r["verdict"] for r in _rows(tmp_path)] == ["aborted"]
    assert outcome == "fail"


def test_abort_in_a_later_example_skips_the_rest_of_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines(), "ex1", "ex2")
    judged: list[str] = []

    def second_is_interrupted(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        judged.append(step.say)
        if len(judged) == 2:
            raise KeyboardInterrupt
        return StepResult(step.say, True, "ok")

    monkeypatch.setattr(runner, "run_step", second_is_interrupted)

    session.run((scenario, replace(scenario, name="next")), None)

    assert [(r.scenario, r.verdict) for r in session.results] == [
        ("chk", "pass"),
        ("chk", "aborted"),
    ]


def test_a_missing_espflash_ends_the_run_with_one_readable_error_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines(), "ex1", "ex2")
    monkeypatch.undo()  # drop the `_device_for` patch: go through the real wait_for/probe
    board = scenario.board
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    session = runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=True))
    built: list[str] = []

    def record_build(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
        built.append(subst["example"])
        return True

    def missing(*a: object, **k: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "espflash")

    monkeypatch.setattr(runner, "_run", record_build)
    # `wait_for` binds `candidate_ports` as a default argument, so patch what it reads;
    # with no port listed, `wait_for` waits for a board forever (CI has none attached).
    fake_port = SimpleNamespace(vid=None, device="/dev/ttyACM0")
    monkeypatch.setattr("rustyfarian_bench.device.list_ports.comports", lambda: [fake_port])
    monkeypatch.setattr("rustyfarian_bench.device.os.path.exists", lambda p: True)
    monkeypatch.setattr("rustyfarian_bench.device.subprocess.run", missing)

    outcome = session.run((scenario,), None)

    assert built == ["ex1"]
    [row] = _rows(tmp_path)
    assert row["verdict"] == "error"
    assert str(row["detail"]).startswith("cannot run 'espflash'")
    assert "cargo install espflash" in str(row["detail"])
    assert outcome == "error"


def _interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declined: tuple[str, ...], ok: bool = True
) -> tuple[runner.Session, Scenario, Scenario]:
    """A session whose operator declines every question starting with one of `declined`."""
    session, scenario = _session(tmp_path, monkeypatch, FakeLines(), "ex1", "ex2", yes=False)
    other = replace(scenario, name="next", examples=("ex3", "ex4"))
    monkeypatch.setattr(ui, "ask", lambda q, default=True: not q.startswith(declined))
    monkeypatch.setattr(ui, "ready", lambda msg: None)
    monkeypatch.setattr(ui, "note", lambda msg: "")
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask, duration=None: StepResult(step.say, ok, "x")
    )
    return session, scenario, other


def _outcomes(tmp_path: Path) -> list[tuple[object, object]]:
    return [(r["example"], r["verdict"]) for r in _rows(tmp_path)]


def test_declining_the_next_check_records_its_examples_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, other = _interactive(tmp_path, monkeypatch, ("Next check",))

    outcome = session.run((scenario, other), "s3/chk")  # a check target requests every example

    assert outcome == "incomplete"
    assert _outcomes(tmp_path) == [
        ("ex1", "pass"),
        ("ex2", "pass"),
        ("ex3", "skipped"),
        ("ex4", "skipped"),
    ]
    skipped = [r for r in _rows(tmp_path) if r["verdict"] == "skipped"]
    assert [r["detail"] for r in skipped] == ["declined by the operator"] * 2


def test_declining_an_example_of_a_requested_check_records_it_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, _ = _interactive(tmp_path, monkeypatch, ("Start ex2",))

    outcome = session.run((scenario,), "s3/chk")

    assert outcome == "incomplete"
    assert _outcomes(tmp_path) == [("ex1", "pass"), ("ex2", "skipped")]


def test_declining_an_offered_twin_of_a_named_example_is_not_skipped_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, _ = _interactive(tmp_path, monkeypatch, ("Start ex2",))

    outcome = session.run((scenario,), "ex1")

    assert outcome == "pass"
    assert _outcomes(tmp_path) == [("ex1", "pass")]


def test_a_fail_outranks_skipped_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, scenario, _ = _interactive(
        tmp_path, monkeypatch, ("Start", "Retry", "Continue"), ok=False
    )

    outcome = session.run((scenario,), "s3/chk")

    assert outcome == "fail"
    assert _outcomes(tmp_path) == [("ex1", "fail"), ("ex2", "skipped")]


def test_a_named_example_in_two_scenarios_is_requested_in_both_but_not_its_twins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, other = _interactive(tmp_path, monkeypatch, ("Next check",))
    second = replace(other, examples=("ex1", "ex9"))

    outcome = session.run((scenario, second), "ex1")

    assert outcome == "incomplete"
    assert _outcomes(tmp_path) == [("ex1", "pass"), ("ex2", "pass"), ("ex1", "skipped")]


def test_declining_the_twin_in_a_second_scenario_of_a_named_example_is_not_skipped_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, other = _interactive(tmp_path, monkeypatch, ("Start ex9",))
    second = replace(other, examples=("ex1", "ex9"))

    outcome = session.run((scenario, second), "ex1")

    assert outcome == "pass"
    assert _outcomes(tmp_path) == [("ex1", "pass"), ("ex2", "pass"), ("ex1", "pass")]


def test_nothing_is_recorded_as_skipped_after_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, other = _interactive(tmp_path, monkeypatch, ("Next", "Start"))

    def interrupted(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_step", interrupted)

    outcome = session.run((scenario, other), "s3/chk")

    assert outcome == "fail"
    assert _outcomes(tmp_path) == [("ex1", "aborted")]


def test_assume_yes_never_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines(), "ex1", "ex2")
    other = replace(scenario, name="next", examples=("ex3", "ex4"))

    def no_questions(*a: object, **k: object) -> bool:
        raise AssertionError("asked")

    monkeypatch.setattr(ui, "ask", no_questions)
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask, duration=None: StepResult(step.say, True, "ok")
    )

    outcome = session.run((scenario, other), "s3/chk")

    assert outcome == "pass"
    assert [v for _, v in _outcomes(tmp_path)] == ["pass"] * 4


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["error", "skipped"], "error"),
        (["unverified", "skipped"], "incomplete"),
        (["pass", "skipped"], "incomplete"),
        (["aborted", "skipped"], "fail"),
    ],
)
def test_overall_precedence_with_skipped_work(verdicts: list[str], expected: str) -> None:
    assert runner._overall(verdicts) == expected


def test_summary_shows_skipped_rows_as_a_warning_not_a_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session, scenario, _ = _interactive(tmp_path, monkeypatch, ("Start ex2",))

    session.run((scenario,), "s3/chk")

    [line] = [ln for ln in capsys.readouterr().out.splitlines() if "declined" in ln]
    assert "SKIPPED" in line
    assert "✗" not in line


def test_an_unset_verdict_is_an_error_not_a_skip() -> None:
    assert runner.ExampleResult("e", "s", "b", "now").verdict == "error"


def test_an_error_example_does_not_stop_the_next_one_and_the_run_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines, "ex1", "ex2")
    calls: list[str] = []

    def flaky(step: Step, lines: object, ask: object, duration: float | None = None) -> StepResult:
        calls.append(step.say)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return StepResult(step.say, True, "ok")

    monkeypatch.setattr(runner, "run_step", flaky)

    outcome = session.run((scenario,), None)

    assert [r.verdict for r in session.results] == ["error", "pass"]
    assert outcome != "pass"
    assert [r["verdict"] for r in _rows(tmp_path)] == ["error", "pass"]


def test_session_run_with_no_examples_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines())
    empty = replace(scenario, examples=())

    assert session.run((), None) == "fail"
    assert session.run((empty,), None) == "fail"
    assert session.results == []


def test_session_run_reports_pass_only_when_every_example_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines())
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask, duration=None: StepResult(step.say, True, "ok")
    )

    assert session.run((scenario,), None) == "pass"


def test_session_run_reports_fail_for_a_failed_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines())
    monkeypatch.setattr(
        runner,
        "run_step",
        lambda step, lines, ask, duration=None: StepResult(step.say, False, "nope"),
    )

    assert session.run((scenario,), None) == "fail"


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: tuple[Step, ...], *, yes: bool
) -> tuple[runner.Session, Scenario, list[str]]:
    board = Board("s3", "esp32s3", "S3")
    scenario = Scenario("chk", board, ("ex",), steps, tmp_path / "chk.toml")
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, (scenario,))
    asked: list[str] = []

    def fake_ask(question: str, default: bool = True) -> bool:
        asked.append(question)
        return True

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: FakeLines("hello", "hello"))
    monkeypatch.setattr(ui, "ask", fake_ask)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32s3", None)
    )
    session = runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=yes))
    return session, scenario, asked


def test_yes_never_confirms_and_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="boot", expect=("hello",), arm=False),
        Step(say="look", confirm="ok?"),
        Step(say="boot again", expect=("hello",), timeout=0.2, arm=False),
    )
    session, scenario, asked = _setup(tmp_path, monkeypatch, steps, yes=True)

    r = session.run_example(scenario, "ex")

    assert asked == []  # ui.ask is never reached under -y
    assert [s.say for s in r.steps] == ["boot", "look", "boot again"]  # later steps still ran
    assert r.steps[1].unverified
    assert r.verdict == "unverified"
    assert "1 unverified" in r.detail


def test_unverified_optional_step_does_not_hold_back_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="boot", expect=("hello",), arm=False),
        Step(say="listen", confirm="ok?", optional=True),
    )
    session, scenario, _ = _setup(tmp_path, monkeypatch, steps, yes=True)

    r = session.run_example(scenario, "ex")

    assert r.steps[1].unverified
    assert r.verdict == "pass"
    assert "1 unverified" in r.detail


def test_failure_outranks_unverified_in_an_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = (
        Step(say="look", confirm="ok?"),
        Step(say="missing", expect=("never",), timeout=0.1),
    )
    session, scenario, _ = _setup(tmp_path, monkeypatch, steps, yes=True)

    r = session.run_example(scenario, "ex")

    assert [s.say for s in r.steps] == ["look", "missing"]
    assert r.verdict == "fail"
    assert "1 unverified" in r.detail


def test_interactive_confirm_still_asks_and_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario, asked = _setup(
        tmp_path, monkeypatch, (Step(say="look", confirm="ok?"),), yes=False
    )

    r = session.run_example(scenario, "ex")

    assert asked == ["ok?"]
    assert r.verdict == "pass"
    assert not r.steps[0].unverified


def test_interactive_denied_confirm_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, scenario, _ = _setup(
        tmp_path, monkeypatch, (Step(say="look", confirm="ok?"),), yes=False
    )
    monkeypatch.setattr(ui, "ask", lambda q, default=True: False)
    monkeypatch.setattr(ui, "note", lambda q: "")

    assert session.run_example(scenario, "ex").verdict == "fail"


def test_results_row_keeps_every_old_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, scenario, _ = _setup(
        tmp_path, monkeypatch, (Step(say="look", confirm="ok?"),), yes=True
    )

    session.run_example(scenario, "ex")

    row = json.loads((tmp_path / "bench/s3/results.jsonl").read_text().splitlines()[0])
    old = {"example", "scenario", "board", "started", "port", "chip", "mac", "log", "verdict",
           "detail", "steps"}  # fmt: skip
    assert old <= row.keys()
    assert {"say", "ok", "detail", "optional", "note"} <= row["steps"][0].keys()
    assert row["steps"][0]["unverified"] is True


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["pass", "pass"], "pass"),
        (["pass", "unverified"], "unverified"),
        (["unverified", "fail"], "fail"),
        (["fail", "error", "unverified"], "error"),
        (["pass", "fail"], "fail"),
        ([], "fail"),
    ],
)
def test_session_verdict_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdicts: list[str], expected: str
) -> None:
    # run_example is faked, but a scenario with no steps would be dropped as unselected.
    steps = (Step(say="x", expect=("a",)),)
    session, scenario, _ = _setup(tmp_path, monkeypatch, steps, yes=True)
    scenario = replace(scenario, examples=tuple(f"e{i}" for i in range(len(verdicts))))
    it = iter(verdicts)

    def fake_example(self: runner.Session, sc: Scenario, ex: str) -> runner.ExampleResult:
        return runner.ExampleResult(ex, sc.name, "s3", "now", verdict=next(it))

    monkeypatch.setattr(runner.Session, "run_example", fake_example)

    assert session.run((scenario,), None) == expected


def _undetected(tmp_path: Path) -> tuple[BenchConfig, Scenario]:
    board = Board("avr", "atmega328p", "Nano", detect=False)
    scenario = Scenario(
        "chk", board, ("ex",), (Step(say="look", confirm="ok?"),), tmp_path / "chk.toml"
    )
    return BenchConfig(
        tmp_path / "bench", ("true",), ("true",), {"avr": board}, (scenario,)
    ), scenario


def test_undetected_board_uses_the_given_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, scenario = _undetected(tmp_path)

    def no_wait(*a: object, **k: object) -> Device:
        raise AssertionError("probed a board with detect = false")

    monkeypatch.setattr(device, "wait_for", no_wait)
    session = runner.Session(cfg, runner.Options(repo=tmp_path, port="/dev/cu.usbserial-1"))
    d = session._device_for(scenario)
    assert (d.port, d.chip, d.mac) == ("/dev/cu.usbserial-1", "atmega328p", None)


@pytest.mark.parametrize(
    "ports", [[], ["/dev/cu.usbserial-9"], ["/dev/a", "/dev/b"]], ids=["none", "one", "several"]
)
def test_undetected_board_needs_port_and_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ports: list[str]
) -> None:
    cfg, scenario = _undetected(tmp_path)
    monkeypatch.setattr(device, "candidate_ports", lambda: ports)

    def no_probe(*a: object, **k: object) -> object:
        raise AssertionError("probed a board with detect = false")

    monkeypatch.setattr(device, "probe", no_probe)
    monkeypatch.setattr(device, "wait_for", no_probe)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    session = runner.Session(cfg, runner.Options(repo=tmp_path, assume_yes=True))

    result = session.run_example(scenario, "ex")

    assert result.verdict == "error"
    assert "--port" in result.detail
    assert "espflash" not in result.detail
    assert session.stopped


def test_serial_settings_come_from_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("avr", "atmega328p", "Nano", detect=False, baud=57_600, reset="dtr")
    scenario = Scenario(
        "chk", board, ("ex",), (Step(say="look", confirm="ok?"),), tmp_path / "chk.toml"
    )
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"avr": board}, (scenario,))
    seen: dict[str, object] = {}

    def fake_lines(port: str, log: Path, **kw: object) -> FakeLines:
        seen.update(kw)
        return FakeLines()

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", fake_lines)
    session = runner.Session(cfg, runner.Options(repo=tmp_path, port="/dev/x", assume_yes=True))
    session.run_example(scenario, "ex")

    assert seen == {"baud": 57_600, "reset": "dtr"}


def _armed_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: Step, yes: bool
) -> tuple[runner.Session, Scenario, list[str], list[float | None]]:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines(), "ex", yes=yes)
    scenario = replace(scenario, steps=(step,))
    prompts: list[str] = []
    durations: list[float | None] = []

    def fake_ready_duration(msg: str, seconds: float) -> float:
        prompts.append(f"duration:{msg}:{seconds:g}")
        return 4.0

    def fake_run_step(
        step: Step, lines: object, ask: object, duration: float | None = None
    ) -> StepResult:
        durations.append(duration)
        return StepResult(step.say, True, "ok")

    monkeypatch.setattr(ui, "ready", lambda msg: prompts.append(f"ready:{msg}"))
    monkeypatch.setattr(ui, "ready_duration", fake_ready_duration)
    monkeypatch.setattr(ui, "ask", lambda q, default=True: True)
    monkeypatch.setattr(runner, "run_step", fake_run_step)
    return session, scenario, prompts, durations


def test_armed_duration_step_asks_for_the_length_and_passes_it_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="rotate", duration=10, reject="pressed")
    session, scenario, prompts, durations = _armed_duration(tmp_path, monkeypatch, step, False)

    session.run_example(scenario, "ex")

    assert prompts == ["duration:rotate:10"]
    assert durations == [4.0]


def test_armed_non_duration_step_uses_plain_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(say="press", expect=("a",))
    session, scenario, prompts, durations = _armed_duration(tmp_path, monkeypatch, step, False)

    session.run_example(scenario, "ex")

    assert prompts == ["ready:press"]
    assert durations == [None]


@pytest.mark.parametrize(
    ("yes", "arm"), [(True, True), (False, False)], ids=["assume_yes", "not_armed"]
)
def test_duration_step_without_a_prompt_keeps_the_scenario_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yes: bool, arm: bool
) -> None:
    step = Step(say="rotate", duration=10, reject="pressed", arm=arm)
    session, scenario, prompts, durations = _armed_duration(tmp_path, monkeypatch, step, yes)

    session.run_example(scenario, "ex")

    assert prompts == []
    assert durations == [None]


def _mixed(tmp_path: Path) -> Scenario:
    steps = (
        Step(say="boot", expect=("a",)),
        Step(say="look", confirm="ok?"),
        Step(say="wave", expect=("b",), attended=True),
        Step(say="settle", expect=("c",)),
    )
    return Scenario("mix", Board("s3", "esp32s3", "S3"), ("ex",), steps, tmp_path / "mix.toml")


def test_steps_for_selects_per_mode_and_keeps_order(tmp_path: Path) -> None:
    sc = _mixed(tmp_path)

    assert runner.steps_for(sc, None) == sc.steps
    assert [s.say for s in runner.steps_for(sc, "unattended")] == ["boot", "settle"]
    assert [s.say for s in runner.steps_for(sc, "attended")] == ["look", "wave"]


class _ModeRun:
    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenarios: tuple[Scenario, ...],
        mode: runner.Mode | None,
    ) -> None:  # fmt: skip
        board = scenarios[0].board
        cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"s3": board}, scenarios)
        self.judged: list[str] = []
        self.totals: list[int] = []
        self.built: list[str] = []
        self.flashed: list[str] = []
        self.devices = 0
        self.warnings: list[str] = []

        def fake_run(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
            if "port" not in subst:  # the build; the flash command also passes the port
                self.built.append(subst["example"])
            else:
                self.flashed.append(subst["example"])
            return True

        def fake_run_step(
            step: Step, lines: object, ask: object, duration: float | None = None
        ) -> StepResult:
            self.judged.append(step.say)
            return StepResult(step.say, True, "ok")

        def fake_device(session: runner.Session, s: Scenario) -> Device:
            self.devices += 1
            return Device("/dev/x", "esp32s3", None)

        monkeypatch.setattr(runner, "_run", fake_run)
        monkeypatch.setattr(runner, "SerialLines", lambda port, log, **kw: FakeLines())
        monkeypatch.setattr(runner, "run_step", fake_run_step)
        monkeypatch.setattr(runner.Session, "_device_for", fake_device)
        monkeypatch.setattr(ui, "step", lambda n, total, say: self.totals.append(total))
        monkeypatch.setattr(ui, "warn", self.warnings.append)
        opts = runner.Options(repo=tmp_path, assume_yes=True, mode=mode)
        self.session = runner.Session(cfg, opts)


def test_attended_mode_runs_only_the_steps_that_need_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _ModeRun(tmp_path, monkeypatch, (_mixed(tmp_path),), "attended")

    outcome = m.session.run(m.session.cfg.scenarios, None)

    assert m.judged == ["look", "wave"]
    assert m.totals == [2, 2]
    (result,) = m.session.results
    assert [s.say for s in result.steps] == ["look", "wave"]
    assert result.detail == "2/2 steps passed"
    assert outcome == "pass"


def test_unattended_mode_runs_only_the_other_steps_and_can_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _ModeRun(tmp_path, monkeypatch, (_mixed(tmp_path),), "unattended")

    outcome = m.session.run(m.session.cfg.scenarios, None)

    assert m.judged == ["boot", "settle"]
    assert m.totals == [2, 2]
    assert m.session.results[0].detail == "2/2 steps passed"
    assert outcome == "pass"


def test_a_scenario_without_selected_steps_is_not_built_flashed_or_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    logs_only = Scenario(
        "sweep", board, ("sweep_ex",), (Step(say="x", expect=("a",)),), tmp_path / "sweep.toml"
    )
    mixed = _mixed(tmp_path)
    m = _ModeRun(tmp_path, monkeypatch, (logs_only, mixed), "attended")
    prompts: list[str] = []
    monkeypatch.setattr(runner.Session, "ask", lambda self, q, default=True: prompts.append(q))

    m.session.run((logs_only, mixed), None)

    assert m.built == ["ex"]
    assert m.flashed == ["ex"]
    assert m.devices == 1
    assert prompts == []  # no "Next check: s3/sweep?" for a dropped scenario
    assert [r.scenario for r in m.session.results] == ["mix"]
    assert {r["scenario"] for r in _rows(tmp_path)} == {"mix"}


def test_an_empty_selection_fails_and_names_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board("s3", "esp32s3", "S3")
    logs_only = Scenario(
        "sweep", board, ("ex",), (Step(say="x", expect=("a",)),), tmp_path / "sweep.toml"
    )
    m = _ModeRun(tmp_path, monkeypatch, (logs_only,), "attended")

    outcome = m.session.run((logs_only,), None)

    assert outcome == "fail"
    assert any("attended" in w for w in m.warnings)
    assert m.built == [] and m.devices == 0 and m.session.results == []
    assert not (tmp_path / "bench/s3/results.jsonl").exists()


@pytest.mark.parametrize("mode", ["unattended", "attended", None])
def test_the_mode_lands_in_every_results_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: runner.Mode | None
) -> None:
    m = _ModeRun(tmp_path, monkeypatch, (_mixed(tmp_path),), mode)

    m.session.run(m.session.cfg.scenarios, None)

    assert [r["mode"] for r in _rows(tmp_path)] == [mode]


def test_skipped_rows_carry_the_mode_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _ModeRun(tmp_path, monkeypatch, (_mixed(tmp_path),), "attended")

    m.session._skip(m.session.cfg.scenarios[0], ["ex"])

    assert [(r["verdict"], r["mode"]) for r in _rows(tmp_path)] == [("skipped", "attended")]
