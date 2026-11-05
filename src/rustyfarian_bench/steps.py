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

    @property
    def counts_as_pass(self) -> bool:
        return self.ok or self.optional


def _watch_reject(lines: LineSource, reject: re.Pattern[str] | None, seconds: float) -> str | None:
    """Watch for `seconds`; return the offending line if `reject` matches."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        line = lines.get(min(left, 0.2))
        if line is not None and reject is not None and reject.search(line):
            return line
    return None


def run_step(step: Step, lines: LineSource, ask: Callable[[str], bool]) -> StepResult:
    reject = re.compile(step.reject) if step.reject else None

    def result(ok: bool, detail: str) -> StepResult:
        return StepResult(step.say, ok, detail, optional=step.optional and not ok)

    if step.confirm is not None:
        return result(ask(step.confirm), "confirmed by operator")

    if step.duration is not None:
        # Watch-only steps judge what happens from now on, not the backlog.
        lines.drain()
        bad = _watch_reject(lines, reject, step.duration)
        if bad is not None:
            return result(False, f"rejected line: {bad!r}")
        return result(True, f"no {step.reject!r} for {step.duration:g} s")

    # Expect steps consume the backlog, so a banner printed during reset still counts.
    patterns = [re.compile(p) for p in step.expect]
    index = 0
    deadline = time.monotonic() + step.timeout
    while index < len(patterns):
        left = deadline - time.monotonic()
        if left <= 0:
            return result(
                False, f"timeout after {step.timeout:g} s waiting for {step.expect[index]!r}"
            )
        line = lines.get(min(left, 0.2))
        if line is None:
            continue
        if reject is not None and reject.search(line):
            return result(False, f"rejected line: {line!r}")
        if patterns[index].search(line):
            index += 1

    if step.settle > 0:
        bad = _watch_reject(lines, reject, step.settle)
        if bad is not None:
            return result(False, f"rejected line while settling: {bad!r}")
    return result(True, "matched " + " → ".join(step.expect))
