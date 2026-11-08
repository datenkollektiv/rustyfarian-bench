"""`reset = true` on a step: the board reboots once the step starts, e.g. into a boot window."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from rustyfarian_bench import ui
from rustyfarian_bench.config import ConfigError, Step, load
from rustyfarian_bench.serial_lines import SerialLines

from .fakes import FakeLines, FakeSerialWorld, shrink_sleep, wait_until
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
