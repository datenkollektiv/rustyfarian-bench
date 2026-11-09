"""Run guided steps against a stream of device lines."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import ui
from .commands import CommandRecord
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
    captured: dict[str, str] = field(default_factory=dict)  # named groups its expect matched
    evidence: tuple[str, ...] = ()  # the line each `expect` pattern matched, in order
    run: CommandRecord | None = None  # the host command the step ran before judging
    lacked: tuple[str, ...] = ()  # prompt fields with no value, shown as `{name}?`

    @property
    def counts_as_pass(self) -> bool:
        return self.ok or self.optional

    @property
    def failed(self) -> bool:
        return not (self.ok or self.optional or self.unverified)


class SerialRejected(Exception):
    """A serial line matched a probe step's `serial_reject` pattern."""

    def __init__(self, line: str):
        super().__init__(line)
        self.line = line


class GuardedLines:
    """A probe step's lines, with the serial lines judged against `pattern` meanwhile.

    Every `get` first looks at the serial lines that arrived since, so a match surfaces
    within one poll of whatever loop is reading. Serial lines from before the step are
    dropped at construction: they are not this step's to judge.
    """

    def __init__(self, source: LineSource, serial: LineSource, pattern: re.Pattern[str]):
        self._source = source
        self._serial = serial
        self._pattern = pattern
        self.lost_text = source.lost_text
        serial.drain()

    def check(self) -> None:
        """Raise `SerialRejected` for the first queued serial line that matches."""
        while (line := self._serial.get(0)) is not None:
            if self._pattern.search(line):
                raise SerialRejected(line)

    def get(self, timeout: float) -> str | None:
        self.check()
        return self._source.get(timeout)

    def drain(self) -> None:
        self._source.drain()

    def link_lost_since(self, t0: float) -> bool:
        return self._source.link_lost_since(t0)


def _watch(
    lines: LineSource,
    reject: re.Pattern[str] | None,
    seconds: float,
    stop: Callable[[], bool] | None = None,
) -> tuple[str, str] | None:
    """Watch for `seconds`; return ("reject", line) or ("lost", "") on the first problem.

    With `stop`, the operator may end the watch early: ("stopped", elapsed seconds).
    A problem seen before the stop still wins.
    """
    t0 = time.monotonic()
    deadline = t0 + seconds
    label = "Enter ends the watch" if stop is not None else ""
    try:
        while (left := deadline - time.monotonic()) > 0:
            ui.progress(left, seconds, label)
            line = lines.get(min(left, 0.2))
            if line is not None and reject is not None and reject.search(line):
                return "reject", line
            if lines.link_lost_since(t0):
                # Lines queued before the loss happened first: judge them before giving up.
                while (line := lines.get(0)) is not None:
                    if reject is not None and reject.search(line):
                        return "reject", line
                return "lost", ""
            if stop is not None and stop():
                elapsed = f"{time.monotonic() - t0:.0f}"
                # Lines already queued happened before the stop: judge them first.
                while (line := lines.get(0)) is not None:
                    if reject is not None and reject.search(line):
                        return "reject", line
                return "stopped", elapsed
    finally:
        ui.progress_done()
    return None


def run_step(
    step: Step,
    lines: LineSource,
    ask: Callable[[str], bool | None],
    duration: float | None = None,
    stop: Callable[[], bool] | None = None,
) -> StepResult:
    reject = re.compile(step.reject) if step.reject else None

    evidence: list[str] = []

    def result(ok: bool, detail: str) -> StepResult:
        return StepResult(
            step.say, ok, detail, optional=step.optional and not ok, evidence=tuple(evidence)
        )

    if step.confirm is not None:
        yes = ask(step.confirm)
        if yes is None:  # nobody was asked, so nothing was observed
            return StepResult(
                step.say, False, "not asked (--yes)", optional=step.optional, unverified=True
            )
        return result(yes, "confirmed by operator" if yes else "denied by operator")

    if step.duration is not None:
        # Watch-only steps judge what happens from now on, not the backlog; a reset step's
        # backlog is already the new boot (the reset dropped the old one).
        if not (step.reset or step.run):
            lines.drain()
        seconds = step.duration if duration is None else duration
        # An operator-chosen length is recorded next to the scenario's, so results stay comparable.
        suffix = f" (scenario: {step.duration:g} s)" if seconds != step.duration else ""
        bad = _watch(lines, reject, seconds, stop)
        if bad is not None and bad[0] == "stopped":
            return result(True, f"ended by operator after {bad[1]} s of {seconds:g} s{suffix}")
        if bad is not None:
            kind, seen = bad
            return result(
                False,
                (lines.lost_text if kind == "lost" else f"rejected line: {seen!r}") + suffix,
            )
        if step.reject is None:
            return result(True, f"watched for {seconds:g} s{suffix}")
        return result(True, f"no {step.reject!r} for {seconds:g} s{suffix}")

    # Expect steps consume the backlog, so a banner printed during reset still counts.
    patterns = [re.compile(p) for p in step.expect]
    captured: dict[str, str] = {}
    index = 0
    started = time.monotonic()
    deadline = started + step.timeout
    try:
        while index < len(patterns):
            left = deadline - time.monotonic()
            if left <= 0:
                detail = f"timeout after {step.timeout:g} s waiting for {step.expect[index]!r}"
                if lines.link_lost_since(started):
                    detail += f" ({lines.lost_text})"
                return result(False, detail)
            ui.progress(left, step.timeout, f"waiting for {step.expect[index]!r}")
            line = lines.get(min(left, 0.2))
            if line is None:
                continue
            # The expected line wins over `reject`, so a scenario can reject the very
            # event it expects when it arrives out of order or once too often.
            if m := patterns[index].search(line):
                captured.update({k: v for k, v in m.groupdict().items() if v is not None})
                evidence.append(line)
                index += 1
            elif reject is not None and reject.search(line):
                return result(False, f"rejected line: {line!r}")
    finally:
        ui.progress_done()

    if step.settle > 0:
        bad = _watch(lines, reject, step.settle)
        if bad is not None:
            kind, seen = bad
            if kind == "lost":
                return result(False, f"{lines.lost_text} while settling")
            return result(False, f"rejected line while settling: {seen!r}")
    detail = "matched " + " → ".join(step.expect)
    if captured:
        detail += " (" + ", ".join(f"{k}={v}" for k, v in captured.items()) + ")"
    return StepResult(step.say, True, detail, captured=captured, evidence=tuple(evidence))
