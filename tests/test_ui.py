from __future__ import annotations

import os
import re
import shutil
import unicodedata
from collections.abc import Iterator

import pytest

from rustyfarian_bench import ui

CLEAR = "\r\033[K"


@pytest.fixture(autouse=True)
def _reset_status() -> Iterator[None]:
    ui._status = None
    ui._title = None
    yield
    ui._status = None
    ui._title = None


def _inputs(monkeypatch: pytest.MonkeyPatch, *answers: str | type[EOFError]) -> list[str]:
    """Feed `answers` to input(); returns the prompts it was shown."""
    it = iter(answers)
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        answer = next(it)
        if isinstance(answer, str):
            return answer
        raise answer

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def test_ready_duration_enter_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _inputs(monkeypatch, "")
    assert ui.ready_duration("rotate", 10) == 10.0
    assert "10 s" in prompts[0]


def test_ready_duration_accepts_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _inputs(monkeypatch, "4")
    assert ui.ready_duration("rotate", 10) == 4.0
    assert len(prompts) == 1  # shorter needs no confirmation


def test_ready_duration_junk_warns_and_reprompts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = _inputs(monkeypatch, "abc", "5")
    assert ui.ready_duration("rotate", 10) == 5.0
    assert len(prompts) == 2
    assert capsys.readouterr().out.strip() != ""


@pytest.mark.parametrize("bad", ["0", "-3", "nan", "inf"])
def test_ready_duration_rejects_non_positive_and_non_finite(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    prompts = _inputs(monkeypatch, bad, "7")
    assert ui.ready_duration("rotate", 10) == 7.0
    assert len(prompts) == 2


def test_ready_duration_confirms_a_longer_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _inputs(monkeypatch, "600", "y")
    assert ui.ready_duration("rotate", 60) == 600.0
    assert "600 s (10 min) instead of 60 s" in prompts[1]


@pytest.mark.parametrize("declined", ["n", ""])  # the confirmation defaults to no
def test_ready_duration_declined_longer_watch_reprompts(
    monkeypatch: pytest.MonkeyPatch, declined: str
) -> None:
    prompts = _inputs(monkeypatch, "600", declined, "")
    assert ui.ready_duration("rotate", 60) == 60.0
    assert len(prompts) == 3


def test_ready_duration_same_length_needs_no_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _inputs(monkeypatch, "60")
    assert ui.ready_duration("rotate", 60) == 60.0
    assert len(prompts) == 1


def test_ready_duration_eof_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs(monkeypatch, EOFError)
    assert ui.ready_duration("rotate", 12.5) == 12.5


def test_progress_is_silent_without_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", False)
    ui.progress(5, 10)
    ui.progress_done()
    assert capsys.readouterr().out == ""


def test_progress_bar_has_20_cells_and_shrinks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.progress(10, 10)
    full = capsys.readouterr().out
    ui.progress(0, 10)
    empty = capsys.readouterr().out
    assert full.count("█") == 20 and full.count("░") == 0
    assert empty.count("█") == 0 and empty.count("░") == 20
    assert "10 s left" in full
    assert "\n" not in full


def _cells(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
        if not unicodedata.combining(ch)
    )


@pytest.mark.parametrize(
    ("text", "columns", "fitted"),
    [
        ("abcdef", 4, "abcd"),
        ("漢字漢字", 5, "漢字"),
        ("a🔥b", 2, "a"),
        ("e\u0301e\u0301", 1, "e\u0301"),
        ("short", 80, "short"),
    ],
)
def test_fit_counts_terminal_cells(text: str, columns: int, fitted: str) -> None:
    assert ui._fit(text, columns) == fitted


@pytest.mark.parametrize(
    "label", ["waiting for '温度センサー準備完了'", "waiting for '🔥🔥🔥🔥🔥🔥'"]
)
def test_progress_with_wide_characters_never_wraps_a_narrow_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], label: str
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((40, 24)))
    ui.progress(5, 10, label)
    status = capsys.readouterr().out.removeprefix(CLEAR)
    assert status.startswith("  ⏱ ")
    assert _cells(status) <= 38


def test_progress_redraws_only_when_the_text_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.progress(9.5, 10)
    first = capsys.readouterr().out
    ui.progress(9.5, 10)
    ui.progress(9.5, 10)
    assert first != ""
    assert capsys.readouterr().out == ""


def test_device_line_is_written_above_the_status_which_is_redrawn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.progress(5, 10)
    capsys.readouterr()
    ui.device_line("hello")
    out = capsys.readouterr().out
    assert out.startswith(CLEAR)
    line_at = out.index("hello")
    assert line_at > 0
    assert "█" in out[line_at:]
    assert "5 s left" in out[line_at:]


def test_progress_done_clears_the_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.progress(5, 10)
    capsys.readouterr()
    ui.progress_done()
    assert capsys.readouterr().out == CLEAR
    assert ui._status is None
    ui.device_line("later")
    assert "█" not in capsys.readouterr().out


def test_progress_done_is_safe_when_nothing_is_shown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", False)
    ui.progress_done()
    assert ui._status is None
    assert capsys.readouterr().out == ""


UP = "\033[1A\033[K"


def test_the_step_instruction_is_pinned_above_the_countdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.step(1, 2, "Watch the ring")
    capsys.readouterr()
    ui.progress(5, 10)
    status = capsys.readouterr().out
    title_at, bar_at = status.index("▶ [1/2] Watch the ring"), status.index("5 s left")
    assert title_at < status.index("\n") < bar_at
    assert not status.endswith("\n")


def test_a_device_line_scrolls_above_both_pinned_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.step(1, 2, "Watch the ring")
    ui.progress(5, 10)
    capsys.readouterr()
    ui.device_line("boot: entry 0x4086c11a")
    out = capsys.readouterr().out
    assert out.startswith(CLEAR + UP)  # both rows erased before the line
    line_at = out.index("boot: entry")
    assert out.index("▶ [1/2] Watch the ring", line_at) < out.index("5 s left", line_at)


def test_progress_done_erases_both_pinned_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.step(1, 2, "Watch the ring")
    ui.progress(5, 10)
    capsys.readouterr()
    ui.progress_done()
    assert capsys.readouterr().out == CLEAR + UP


def test_the_pinned_instruction_fits_a_narrow_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((30, 24)))
    ui.step(1, 2, "Watch the ring: all 12 LEDs breathe blue together, dark to dim and back")
    capsys.readouterr()
    ui.progress(5, 10)
    rows = capsys.readouterr().out.removeprefix(CLEAR).split("\n")
    assert len(rows) == 2
    assert all(_cells(re.sub(r"\033\[[0-9;]*m", "", row)) <= 28 for row in rows)


def test_a_heading_unpins_the_instruction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ui, "_TTY", True)
    ui.step(1, 2, "Watch the ring")
    ui.heading("s3/pulse: hal_s3_pulse")
    capsys.readouterr()
    ui.progress(5, 10)
    assert "\n" not in capsys.readouterr().out
