"""Run guided steps against a stream of device lines."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Step
from .serial_lines import LineSource


@dataclass(frozen=True)
class StepResult:
    say: str
    ok: bool
    detail: str
    optional: bool = False
    unverified: bool = False  # never observed, e.g. a confirm under --yes
    note: str = ""  # what the operator saw, asked for when the step finally fails

    @property
    def counts_as_pass(self) -> bool:
        return self.ok or self.optional

    @property
    def failed(self) -> bool:
        return not (self.ok or self.optional or self.unverified)


def _watch(
    lines: LineSource, reject: re.Pattern[str] | None, seconds: float
) -> tuple[str, str] | None:
    """Watch for `seconds`; return ("reject", line) or ("lost", "") on the first problem."""
    t0 = time.monotonic()
    deadline = t0 + seconds
    while (left := deadline - time.monotonic()) > 0:
        line = lines.get(min(left, 0.2))
        if line is not None and reject is not None and reject.search(line):
            return "reject", line
        if lines.link_lost_since(t0):
            return "lost", ""
    return None


def run_step(step: Step, lines: LineSource, ask: Callable[[str], bool | None]) -> StepResult:
    reject = re.compile(step.reject) if step.reject else None

    def result(ok: bool, detail: str) -> StepResult:
        return StepResult(step.say, ok, detail, optional=step.optional and not ok)

    if step.confirm is not None:
        yes = ask(step.confirm)
        if yes is None:  # nobody was asked, so nothing was observed
            return StepResult(
                step.say, False, "not asked (--yes)", optional=step.optional, unverified=True
            )
        return result(yes, "confirmed by operator" if yes else "denied by operator")

    if step.duration is not None:
        # Watch-only steps judge what happens from now on, not the backlog.
        lines.drain()
        bad = _watch(lines, reject, step.duration)
        if bad is not None:
            kind, seen = bad
            return result(
                False, "serial connection lost" if kind == "lost" else f"rejected line: {seen!r}"
            )
        return result(True, f"no {step.reject!r} for {step.duration:g} s")

    # Expect steps consume the backlog, so a banner printed during reset still counts.
    patterns = [re.compile(p) for p in step.expect]
    index = 0
    started = time.monotonic()
    deadline = started + step.timeout
    while index < len(patterns):
        left = deadline - time.monotonic()
        if left <= 0:
            detail = f"timeout after {step.timeout:g} s waiting for {step.expect[index]!r}"
            if lines.link_lost_since(started):
                detail += " (serial connection lost)"
            return result(False, detail)
        line = lines.get(min(left, 0.2))
        if line is None:
            continue
        # The expected line wins over `reject`, so a scenario can reject the very
        # event it expects when it arrives out of order or once too often.
        if patterns[index].search(line):
            index += 1
        elif reject is not None and reject.search(line):
            return result(False, f"rejected line: {line!r}")

    if step.settle > 0:
        bad = _watch(lines, reject, step.settle)
        if bad is not None:
            kind, seen = bad
            if kind == "lost":
                return result(False, "serial connection lost while settling")
            return result(False, f"rejected line while settling: {seen!r}")
    return result(True, "matched " + " → ".join(step.expect))
