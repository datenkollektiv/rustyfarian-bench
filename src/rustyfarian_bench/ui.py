"""Terminal output and prompts."""

from __future__ import annotations

import math
import select
import shutil
import sys
import threading
import unicodedata

_TTY = sys.stdout.isatty()
_BAR = 20
_CLEAR = "\r\033[K"

# Device lines are printed from the serial reader thread, so the status line needs a lock.
_lock = threading.Lock()
_status: str | None = None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _emit(text: str) -> None:
    """Print above the status line: clear it, print, redraw it."""
    with _lock:
        shown = _status is not None and _TTY
        if shown:
            sys.stdout.write(_CLEAR)
        print(text, flush=True)
        if shown:
            sys.stdout.write(_status or "")
            sys.stdout.flush()


def info(msg: str) -> None:
    _emit(_c("36", "• ") + msg)


def warn(msg: str) -> None:
    _emit(_c("33", "! ") + msg)


def step(n: int, total: int, msg: str) -> None:
    _emit(_c("1", f"\n▶ [{n}/{total}] {msg}"))


def device_line(line: str) -> None:
    _emit(_c("2", f"  │ {line}"))


def verdict(
    ok: bool, msg: str, optional: bool = False, unverified: bool = False, skipped: bool = False
) -> None:
    if skipped:
        _emit(_c("33", "  - SKIPPED ") + msg)
    elif unverified:
        _emit(_c("33", "  ? UNVERIFIED ") + msg)
    elif ok:
        _emit(_c("32", "  ✓ ") + msg)
    elif optional:
        _emit(_c("33", "  ~ ") + msg + " (optional)")
    else:
        _emit(_c("31", "  ✗ ") + msg)


def heading(msg: str) -> None:
    _emit(_c("1;35", f"\n=== {msg} ==="))


def _fit(text: str, columns: int) -> str:
    """The longest prefix of `text` that fits in `columns` terminal cells."""
    used = 0
    for i, ch in enumerate(text):
        if unicodedata.combining(ch):
            continue
        used += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used > columns:
            return text[:i]
    return text


def progress(left: float, total: float, label: str = "") -> None:
    """Draw a shrinking bar on one line; no-op off a TTY."""
    global _status
    if not _TTY:
        return
    remaining = min(_BAR, max(0, round(_BAR * left / total))) if total > 0 else 0
    text = "  ⏱ " + "█" * remaining + "░" * (_BAR - remaining) + f"  {math.ceil(left)} s left"
    if label:
        text += f" — {label}"
    # A wrapped status line cannot be cleared with \r, so keep it to one row; the spare
    # cell is for ⏱, which some terminals draw 2 wide.
    text = _fit(text, shutil.get_terminal_size().columns - 2)
    with _lock:
        if text == _status:
            return
        _status = text
        sys.stdout.write(_CLEAR + text)
        sys.stdout.flush()


def progress_done() -> None:
    """Erase the status line; safe when none is shown."""
    global _status
    with _lock:
        if _status is not None and _TTY:
            sys.stdout.write(_CLEAR)
            sys.stdout.flush()
        _status = None


def ask(question: str, default: bool = True) -> bool:
    progress_done()
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"? {question} {hint} ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def note(question: str) -> str:
    """Free-text answer; empty when skipped."""
    progress_done()
    try:
        return input(_c("1", f"✎ {question} (Enter to skip) ")).strip()
    except EOFError:
        return ""


def enter_pressed() -> bool:
    """True once the operator has pressed Enter; never blocks, and False off a TTY."""
    if not sys.stdin.isatty():
        return False
    try:
        pressed, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):  # no select() on this stdin, e.g. Windows
        return False
    if not pressed:
        return False
    sys.stdin.readline()
    return True


def ready(msg: str) -> None:
    """Block until the operator presses Enter; the step's clock starts afterwards."""
    progress_done()
    try:
        input(_c("1", f"⏎ Press Enter, then: {msg} "))
    except EOFError:
        return


def ready_duration(msg: str, seconds: float) -> float:
    """Like `ready`, but the operator may type a different watch length in seconds."""
    progress_done()
    prompt = _c("1", f"⏎ Press Enter to watch {seconds:g} s (or type seconds), then: {msg} ")
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return seconds
        if not answer:
            return seconds
        try:
            value = float(answer)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and value > 0):
            warn(f"enter a positive number of seconds, or just Enter for {seconds:g}")
            continue
        # Ctrl-C ends the whole session, so a typo like 600 for 60 would cost a restart.
        if value <= seconds or ask(f"Watch {_span(value)} instead of {_span(seconds)}?", False):
            return value


def _span(seconds: float) -> str:
    return f"{seconds:g} s ({seconds / 60:.0f} min)" if seconds >= 120 else f"{seconds:g} s"
