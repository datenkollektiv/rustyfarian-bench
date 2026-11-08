"""`reset = true` on a step: the board reboots once the step starts, e.g. into a boot window."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
import serial

from rustyfarian_bench import ui
from rustyfarian_bench.config import ConfigError, Step, load
from rustyfarian_bench.serial_lines import SerialLines

from .fakes import Buffered, FakeLines, FakeSerialWorld, shrink_sleep, wait_until
from .test_config import BENCH, write
from .test_runner import _session


class ResettableLines(FakeLines):
    """Queued lines before the reset, `after` once it happened."""

    def __init__(self, *before: str, after: tuple[str, ...] = (), ok: bool = True):
        super().__init__(*before)
        self.after = after
        self.ok = ok
        self.resets = 0

    def reset(self, timeout: float = 5.0) -> bool:
        self.resets += 1
        if self.ok:
            self.queue.clear()
            self.queue.extend(self.after)
        return self.ok


def _request_reset(sl: SerialLines) -> None:
    """What `reset()` records, without waiting for the reader to serve it."""
    with sl._resets:
        sl._reset_wanted += 1


def test_reset_pulses_the_open_port_and_drops_the_lines_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"old\n"])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        assert wait_until(world.exhausted.is_set)
        assert sl.reset(2.0)
        assert sl.get(0.05) is None
    assert world.opened[0].events.count("rts=True") == 1


def test_a_reset_step_reboots_after_enter_and_judges_only_the_new_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ResettableLines("Calibration accepted", after=("boot", "Calibration accepted"))
    session, scenario = _session(tmp_path, monkeypatch, lines, yes=False)
    order: list[str] = []
    monkeypatch.setattr(ui, "ready", lambda msg: order.append(f"enter:{lines.resets}"))
    step = Step(say="turn", expect=("Calibration accepted",), timeout=0.3, reset=True)

    result = session.run_example(replace(scenario, steps=(step,)), "ex")

    assert order == ["enter:0"]  # Enter came before the reset
    assert lines.resets == 1
    assert result.verdict == "pass"


def test_a_board_that_does_not_reset_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ResettableLines("Calibration accepted", ok=False)
    session, scenario = _session(tmp_path, monkeypatch, lines)
    step = Step(say="turn", expect=("Calibration accepted",), timeout=0.3, arm=False, reset=True)

    result = session.run_example(replace(scenario, steps=(step,)), "ex")

    assert result.steps[0].detail == "the board did not reset"


def test_a_retry_resets_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ResettableLines(after=("falling back",))
    session, scenario = _session(tmp_path, monkeypatch, lines, yes=False)
    answers = iter([True, False, False])
    monkeypatch.setattr(ui, "ready", lambda msg: None)
    monkeypatch.setattr(ui, "ask", lambda q, default=True: next(answers))
    monkeypatch.setattr(ui, "note", lambda q: "")
    step = Step(say="turn", expect=("accepted",), reject="falling back", timeout=0.3, reset=True)

    session.run_example(replace(scenario, steps=(step,)), "ex")

    assert lines.resets == 2


def test_retry_and_continue_questions_mute_the_device_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = FakeLines()
    session, scenario = _session(tmp_path, monkeypatch, lines, yes=False)
    echo_while_asked: list[bool] = []

    def fake_ask(question: str, default: bool = True) -> bool:
        echo_while_asked.append(lines.echo)
        return False

    monkeypatch.setattr(ui, "ask", fake_ask)
    monkeypatch.setattr(ui, "note", lambda q: "")
    step = Step(say="wait", expect=("never",), timeout=0.1, arm=False)

    session.run_example(replace(scenario, steps=(step, step)), "ex")

    assert echo_while_asked == [False, False]  # Retry?, then Continue?
    assert lines.echo


def test_a_reset_step_on_a_board_without_reset_is_a_config_error(tmp_path: Path) -> None:
    bench = BENCH + 'reset = "none"\n'
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpect = "a"\nreset = true\n'
    with pytest.raises(ConfigError, match="has reset = none"):
        load(write(tmp_path, {"bench.toml": bench, "s3/bad.toml": bad}))


def test_a_partial_line_before_the_reset_does_not_prefix_the_new_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[SerialLines] = []
    started = threading.Event()

    def wait_for_start() -> None:
        started.wait(2.0)

    def ask_for_reset() -> None:  # the reset request lands mid-line, like a real one can
        _request_reset(held[0])

    world = FakeSerialWorld([wait_for_start, b"half a li", ask_for_reset, b"boot\n"])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        held.append(sl)
        started.set()
        assert sl.get(2.0) == "boot"


def test_a_timed_out_reset_does_not_fire_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"up\n"], present=False)  # unplugged when the step asks
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    monkeypatch.setattr("rustyfarian_bench.serial_lines.FIRST_OPEN_WAIT", 0.01)
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        assert not sl.reset(0.05)
        world.paths.add("/dev/x")  # plugged back in
        assert sl.get(2.0) == "up"
    assert "rts=True" not in world.opened[0].events


def test_a_reset_duration_step_judges_the_new_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ResettableLines(after=("PANIC at boot",))
    session, scenario = _session(tmp_path, monkeypatch, lines)
    step = Step(say="soak", duration=0.2, reject="PANIC", arm=False, reset=True)

    result = session.run_example(replace(scenario, steps=(step,)), "ex")

    assert result.verdict == "fail"
    assert "PANIC at boot" in result.steps[0].detail


def test_a_reset_that_lands_on_reopen_drops_the_lines_before_the_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld(present=False)
    world.paths.add("/dev/x")

    def unplug() -> None:
        world.paths.discard("/dev/x")

    world.scripts.extend([[b"old\n", unplug, serial.SerialException("gone")], [b"boot\n"]])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        assert wait_until(lambda: "/dev/x" not in world.paths and len(world.opened) == 1)
        _request_reset(sl)  # what reset() asks for while the board is away
        world.paths.add("/dev/x")  # back again: the reset fires on the reopen
        assert wait_until(lambda: sl._reset_served == 1)
        assert sl.get(2.0) == "boot"


def test_a_startup_pulse_still_running_does_not_answer_a_steps_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gates = [threading.Event(), threading.Event()]  # one per pulse: startup, then the step's
    finished: list[int] = []

    def slow_pulse(s: object) -> None:
        n = len(finished)
        gates[min(n, 1)].wait(2.0)
        finished.append(n)

    world = FakeSerialWorld([])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    monkeypatch.setattr("rustyfarian_bench.serial_lines._hard_reset", slow_pulse)
    answers: list[tuple[bool, int]] = []
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="rts") as sl:
        asker = threading.Thread(target=lambda: answers.append((sl.reset(2.0), len(finished))))
        asker.start()  # the step asks while the startup pulse is still running
        gates[0].set()  # the startup pulse ends
        assert not wait_until(lambda: bool(answers), timeout=0.2)  # still waiting for its own
        gates[1].set()
        asker.join(2.0)
    assert answers == [(True, 2)]


def test_an_optional_reset_step_that_does_not_reset_stays_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = ResettableLines("Calibration accepted", ok=False)
    session, scenario = _session(tmp_path, monkeypatch, lines)
    step = Step(say="turn", expect=("accepted",), timeout=0.3, arm=False, reset=True, optional=True)

    result = session.run_example(replace(scenario, steps=(step,)), "ex")

    assert result.steps[0].detail == "the board did not reset"
    assert result.steps[0].optional
    assert result.verdict == "pass"


def test_a_reset_that_times_out_during_its_own_pulse_reports_the_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = threading.Event(), threading.Event()
    finished: list[int] = []

    def slow_pulse(s: object) -> None:
        entered.set()
        gate.wait(2.0)
        finished.append(1)

    world = FakeSerialWorld([])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    monkeypatch.setattr("rustyfarian_bench.serial_lines._hard_reset", slow_pulse)
    answers: list[tuple[bool, int]] = []
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        asker = threading.Thread(target=lambda: answers.append((sl.reset(0.1), len(finished))))
        asker.start()
        assert entered.wait(2.0)  # the reader took the request and is pulsing
        threading.Event().wait(0.3)  # the request's own timeout passes mid-pulse
        gate.set()
        asker.join(2.0)
    assert answers == [(True, 1)]  # told the truth: the board did reset


def test_bytes_the_driver_still_holds_are_flushed_before_the_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[SerialLines] = []
    started = threading.Event()

    def wait_for_start() -> None:
        started.wait(2.0)

    def ask_for_reset() -> None:
        _request_reset(held[0])

    old_boot = Buffered(b"Calibration accepted\n")  # sent before the reset, not yet read
    world = FakeSerialWorld([wait_for_start, ask_for_reset, old_boot, b"boot\n"])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    with SerialLines("/dev/x", tmp_path / "run.log", echo=False, reset="none") as sl:
        held.append(sl)
        started.set()
        assert sl.get(2.0) == "boot"
    events = world.opened[0].events
    assert events.index("flush") < events.index("rts=True")  # flushed, then reset
