"""Terminal output and prompts."""

from __future__ import annotations

import sys

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def info(msg: str) -> None:
    print(_c("36", "• ") + msg, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "! ") + msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    print(_c("1", f"\n▶ [{n}/{total}] {msg}"), flush=True)


def device_line(line: str) -> None:
    print(_c("2", f"  │ {line}"), flush=True)


def verdict(ok: bool, msg: str, optional: bool = False) -> None:
    if ok:
        print(_c("32", "  ✓ ") + msg, flush=True)
    elif optional:
        print(_c("33", "  ~ ") + msg + " (optional)", flush=True)
    else:
        print(_c("31", "  ✗ ") + msg, flush=True)


def heading(msg: str) -> None:
    print(_c("1;35", f"\n=== {msg} ==="), flush=True)


def ask(question: str, default: bool = True) -> bool:
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
