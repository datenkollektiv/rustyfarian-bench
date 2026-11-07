from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from rustyfarian_bench import device as device_mod
from rustyfarian_bench import runner, ui
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

    def fake_run_step(step: Step, lines: object, ask: object) -> StepResult:
        judged.append(step.say)
        return StepResult(step.say, False, "nope")

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: FakeLines())
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: lines)
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: lines)
    monkeypatch.setattr(ui, "ready", lambda msg: events.append(f"ready:{msg}"))
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask: StepResult(step.say, True, "ok")
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: lines)
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: FakeLines())
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask: StepResult(step.say, False, "nope")
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: lines)
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

    def raising(step: Step, lines: object, ask: object) -> StepResult:
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

    def interrupted(step: Step, lines: object, ask: object) -> StepResult:
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

    def interrupted(step: Step, lines: object, ask: object) -> StepResult:
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

    def second_is_interrupted(step: Step, lines: object, ask: object) -> StepResult:
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
    monkeypatch.setattr(device_mod, "candidate_ports", lambda: ["/dev/x"])
    monkeypatch.setattr("rustyfarian_bench.device.os.path.exists", lambda p: True)
    monkeypatch.setattr("rustyfarian_bench.device.subprocess.run", missing)

    outcome = session.run((scenario,), None)

    assert built == ["ex1"]
    [row] = _rows(tmp_path)
    assert row["verdict"] == "error"
    assert str(row["detail"]).startswith("cannot run 'espflash'")
    assert "cargo install espflash" in str(row["detail"])
    assert outcome == "error"


def test_an_error_example_does_not_stop_the_next_one_and_the_run_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines, "ex1", "ex2")
    calls: list[str] = []

    def flaky(step: Step, lines: object, ask: object) -> StepResult:
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
        runner, "run_step", lambda step, lines, ask: StepResult(step.say, True, "ok")
    )

    assert session.run((scenario,), None) == "pass"


def test_session_run_reports_fail_for_a_failed_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, scenario = _session(tmp_path, monkeypatch, FakeLines())
    monkeypatch.setattr(
        runner, "run_step", lambda step, lines, ask: StepResult(step.say, False, "nope")
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
    monkeypatch.setattr(runner, "SerialLines", lambda port, log: FakeLines("hello", "hello"))
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
    session, scenario, _ = _setup(tmp_path, monkeypatch, (), yes=True)
    scenario = replace(scenario, examples=tuple(f"e{i}" for i in range(len(verdicts))))
    it = iter(verdicts)

    def fake_example(self: runner.Session, sc: Scenario, ex: str) -> runner.ExampleResult:
        return runner.ExampleResult(ex, sc.name, "s3", "now", verdict=next(it))

    monkeypatch.setattr(runner.Session, "run_example", fake_example)

    assert session.run((scenario,), None) == expected
