"""Drive one bench session: build, find the board, flash, reset, guide the steps, record."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import device, ui
from .commands import CommandRecord, run_command
from .config import (
    PROBE_FIELDS,
    BenchConfig,
    Board,
    Scenario,
    Step,
    defines,
    placeholders,
    references,
    render_prompt,
    resolve,
)
from .probe_lines import ProbeLines
from .serial_lines import LineSource, SerialLines
from .steps import GuardedLines, SerialRejected, StepResult, run_step

Mode = Literal["unattended", "attended"]


def steps_for(scenario: Scenario, mode: Mode | None) -> tuple[Step, ...]:
    """The steps a run in `mode` covers; `None` covers all of them."""
    if mode is None:
        return scenario.steps
    wanted = mode == "attended"
    return tuple(s for s in scenario.steps if s.needs_person == wanted)


@dataclass
class ExampleResult:
    example: str
    scenario: str
    board: str
    started: str
    port: str | None = None
    chip: str | None = None
    mac: str | None = None
    log: str | None = None
    verdict: str = "error"
    detail: str = ""
    steps: list[StepResult] = field(default_factory=list)
    mode: Mode | None = None
    setup: list[CommandRecord] = field(default_factory=list)
    teardown: list[CommandRecord] = field(default_factory=list)


@dataclass
class Options:
    repo: Path
    port: str | None = None
    build: bool = True
    assume_yes: bool = False
    mode: Mode | None = None


def _run(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
    argv = [part.format(**subst) for part in cmd]
    ui.info("$ " + " ".join(argv))
    try:
        return subprocess.run(argv, cwd=repo, env=device.ESPFLASH_ENV).returncode == 0
    except (FileNotFoundError, PermissionError) as e:
        raise device.missing_tool(argv[0], e) from e


def _warn_lacked(lacked: tuple[str, ...]) -> None:
    if lacked:
        ui.warn("prompt lacked " + " ".join(f"{{{name}}}" for name in lacked))


def _overall(verdicts: list[str]) -> str:
    if not verdicts:
        return "fail"
    if "error" in verdicts:
        return "error"
    if any(v not in ("pass", "unverified", "skipped") for v in verdicts):
        return "fail"
    if "skipped" in verdicts:
        return "incomplete"
    return "unverified" if "unverified" in verdicts else "pass"


class Session:
    def __init__(
        self,
        cfg: BenchConfig,
        opts: Options,
        *,
        open_probe: Callable[..., ProbeLines] = ProbeLines,
        run_command: Callable[..., CommandRecord] = run_command,
    ):
        self.cfg = cfg
        self.opts = opts
        self.open_probe = open_probe  # (argv, cwd, log_path) -> a probe step's line source
        self.run_command = run_command  # (argv, cwd, log_path, timeout) -> how it went
        self.device: device.Device | None = None
        self.results: list[ExampleResult] = []
        self.stopped = (
            False  # set by Ctrl-C, a missing tool or a missing --port: no further examples
        )

    def ask(self, question: str, default: bool = True) -> bool:
        return default if self.opts.assume_yes else ui.ask(question, default)

    def _ready_quietly(self, lines: SerialLines, msg: str) -> None:
        lines.echo = False
        try:
            ui.ready(msg)
        finally:
            lines.echo = True

    def _ready_duration_quietly(self, lines: SerialLines, msg: str, seconds: float) -> float:
        lines.echo = False
        try:
            return ui.ready_duration(msg, seconds)
        finally:
            lines.echo = True

    def confirm(self, question: str) -> bool | None:
        """Ask the operator to vouch for something; `None` under `--yes`, never a default."""
        return None if self.opts.assume_yes else ui.ask(question, True)

    def _ask_quietly(self, lines: SerialLines, question: str) -> bool | None:
        """Ask without device lines scrolling over the prompt; the log still gets them."""
        lines.echo = False
        try:
            return self.confirm(question)
        finally:
            lines.echo = True

    def _ask_muted(self, lines: SerialLines, question: str, default: bool = True) -> bool:
        """`ask`, without device lines scrolling over the question."""
        lines.echo = False
        try:
            return self.ask(question, default)
        finally:
            lines.echo = True

    def _note_quietly(self, lines: SerialLines) -> str:
        """Ask what the operator saw, for whoever reads the result later."""
        lines.echo = False
        try:
            return ui.note("What did you see?")
        finally:
            lines.echo = True

    def _still_fits(self, board: Board, d: device.Device) -> bool:
        """Whether the cached device can serve `board` without looking for it again."""
        if not (os.path.exists(d.port) and device.matches(board, d)):
            return False
        # An undetected board has no MAC, so a matching chip proves nothing: two
        # same-chip boards would share a port. Only the port it was found by counts.
        if board.detect:
            return d.mac is not None  # found by espflash, not by port or USB identity
        if self.opts.port is not None:
            return d.port == self.opts.port
        if board.usb_id is not None:
            # The same rule as `wait_for_usb`: exactly one match, or ask which one.
            return device.usb_ports(board.usb_id, board.usb_serial) == [d.port]
        return False

    def _device_for(self, scenario: Scenario) -> device.Device:
        d = self.device
        if d and self._still_fits(scenario.board, d):
            return d
        if scenario.board.detect:
            self.device = device.wait_for(scenario.board, self.opts.port)
        else:
            self.device = self._undetected(scenario)
        return self.device

    def _undetected(self, scenario: Scenario) -> device.Device:
        """A board with `detect = false`: found by its `usb_id`, else `--port` is required.

        Never runs espflash. A lone attached port is no proof of which board it is,
        so nothing is guessed; a USB identity is.
        """
        board = scenario.board
        port = self.opts.port
        if port is None and board.usb_id is not None:
            return device.wait_for_usb(board)
        if port is None:
            raise device.PortRequired(f"{board.name} cannot be detected; pick its port with --port")
        ui.info(f"Using {port} for {board.name} (not detected: {board.chip})")
        return device.Device(port=port, chip=device.normalize_chip(board.chip), mac=None)

    @staticmethod
    def _plan(scenario: Scenario, first_example: str | None) -> tuple[tuple[str, ...], list[str]]:
        """The examples to offer, and the ones the operator asked for.

        An example named as the target is the only one requested; its twins are extras.
        """
        examples = scenario.examples
        if first_example in examples:
            return examples[examples.index(first_example) :], [first_example]
        return examples, list(examples)

    def _skip(self, scenario: Scenario, examples: list[str]) -> None:
        for example in examples:
            r = ExampleResult(
                example=example,
                scenario=scenario.name,
                board=scenario.board.key,
                started=datetime.now().isoformat(timespec="seconds"),
                verdict="skipped",
                detail="declined by the operator",
                mode=self.opts.mode,
            )
            self._record(scenario, r)
            self.results.append(r)

    def run(self, scenarios: tuple[Scenario, ...], first_example: str | None) -> str:
        """Run the scenarios and return the worst verdict.

        error > fail > incomplete > unverified > pass. A requested example the operator
        declined is recorded as skipped and makes the run incomplete. An empty run is a
        fail, never a pass. Scenarios with no step for the mode are dropped up front, so
        they are never built, flashed, prompted for or recorded.
        """
        mode = self.opts.mode
        selected = tuple(s for s in scenarios if steps_for(s, mode))
        if scenarios and not selected:
            ui.warn(f"nothing to run {mode}: no {mode} steps in the selected checks")
            self.summary()
            return "fail"
        scenarios = selected
        for n, scenario in enumerate(scenarios):
            examples, requested = self._plan(scenario, first_example)
            if n > 0 and not self.ask(f"Next check: {scenario.board.key}/{scenario.name}?"):
                for later in scenarios[n:]:
                    self._skip(later, self._plan(later, first_example)[1])
                break
            for i, example in enumerate(examples):
                if i > 0 and not self.ask(f"Start {example}?"):
                    # Twins of a named example are extras: declining them records nothing.
                    self._skip(scenario, [e for e in examples[i:] if e in requested])
                    break
                self.results.append(self.run_example(scenario, example))
                if self.stopped:
                    break
            if self.stopped:
                break  # `--yes` must not carry on past Ctrl-C or a missing tool
        self.summary()
        return _overall([r.verdict for r in self.results])

    def run_example(self, scenario: Scenario, example: str) -> ExampleResult:
        stamp = datetime.now()
        r = ExampleResult(
            example=example,
            scenario=scenario.name,
            board=scenario.board.key,
            started=stamp.isoformat(timespec="seconds"),
            mode=self.opts.mode,
        )
        ui.heading(f"{scenario.board.key}/{scenario.name}: {example}")
        try:
            self._run_example(scenario, example, stamp, r)
        except KeyboardInterrupt:
            ui.progress_done()
            print()
            r.verdict, r.detail = "aborted", "Ctrl-C"
            self.stopped = True
            ui.warn("aborted")
        except (device.MissingTool, device.PortRequired) as e:
            r.verdict, r.detail = "error", str(e)
            self.stopped = True
            ui.warn(f"error: {r.detail}")
        except Exception as e:  # any other failure must still leave a recorded row
            r.verdict, r.detail = "error", f"{type(e).__name__}: {e}"
            ui.warn(f"error: {r.detail}")
        self._teardown(scenario, example, stamp, r)
        self._record(scenario, r)
        return r

    def _log_path(self, scenario: Scenario, example: str, stamp: datetime) -> Path:
        return self.cfg.board_dir(scenario.board) / "logs" / f"{example}-{stamp:%Y%m%d-%H%M%S}.log"

    def _host(
        self,
        argv: list[str],
        log: Path,
        timeout: float,
        into: list[CommandRecord] | None = None,
    ) -> CommandRecord:
        """Run a host command for its record; Ctrl-C leaves a stub record and propagates."""
        ui.info("$ " + " ".join(argv))
        shown = str(log.relative_to(self.opts.repo))
        try:
            rec = replace(self.run_command(argv, self.opts.repo, log, timeout), log=shown)
        except KeyboardInterrupt:
            if into is not None:
                into.append(
                    CommandRecord(tuple(argv), None, 0.0, error="interrupted by Ctrl-C", log=shown)
                )
            raise
        if into is not None:
            into.append(rec)
        return rec

    def _setup(self, scenario: Scenario, example: str, stamp: datetime, r: ExampleResult) -> bool:
        """Run the scenario's setup commands; on the first failure the example is an error."""
        log = self._log_path(scenario, example, stamp)
        for i, command in enumerate(scenario.setup, 1):
            argv = [part.format(example=example, board=scenario.board.key) for part in command]
            rec = self._host(
                argv,
                log.with_name(f"{log.stem}.setup{i}{log.suffix}"),
                scenario.hook_timeout,
                r.setup,
            )
            if not rec.ok:
                r.verdict, r.detail = "error", f"setup failed: {rec.summary()}"
                return False
        return True

    def _teardown(
        self, scenario: Scenario, example: str, stamp: datetime, r: ExampleResult
    ) -> None:
        """Run the scenario's teardown commands after any outcome; never raises.

        A failing command is a warning and leaves the verdict alone. A command that needs the
        port or chip of a board that was never detected is not run. Ctrl-C skips the rest.
        """
        log = self._log_path(scenario, example, stamp)
        known = {"port": r.port, "chip": r.chip, "mac": r.mac}
        try:
            for i, command in enumerate(scenario.teardown, 1):
                path = log.with_name(f"{log.stem}.teardown{i}{log.suffix}")
                shown = str(path.relative_to(self.opts.repo))
                unknown = sorted(
                    f for f in placeholders(command) if f in known and known[f] is None
                )
                if unknown:
                    text = f"not run: {{{unknown[0]}}} unknown"
                    r.teardown.append(CommandRecord(command, None, 0.0, error=text, log=shown))
                    ui.warn(f"teardown: {' '.join(command)}: {text}")
                    continue
                argv = [
                    part.format(
                        port=r.port,
                        chip=r.chip,
                        mac=r.mac,
                        example=example,
                        board=scenario.board.key,
                    )
                    for part in command
                ]
                try:
                    rec = self._host(argv, path, scenario.hook_timeout, r.teardown)
                except Exception as e:  # one broken hook must not keep the others from running
                    rec = CommandRecord(
                        tuple(argv), None, 0.0, error=f"{type(e).__name__}: {e}", log=shown
                    )
                    r.teardown.append(rec)
                if not rec.ok:
                    ui.warn(f"teardown: {rec.summary()}")
        except KeyboardInterrupt:
            ui.progress_done()
            print()
            for command in scenario.teardown[len(r.teardown) :]:
                r.teardown.append(
                    CommandRecord(command, None, 0.0, error="interrupted by Ctrl-C", log="")
                )
            self.stopped = True
            ui.warn("teardown interrupted; the remaining commands were skipped")

    def _run_example(
        self, scenario: Scenario, example: str, stamp: datetime, r: ExampleResult
    ) -> None:
        board = scenario.board
        if not self._setup(scenario, example, stamp, r):
            return
        if self.opts.build and not _run(self.cfg.build_for(board), self.opts.repo, example=example):
            r.verdict, r.detail = "fail", "build failed"
            return

        d = self._device_for(scenario)
        r.port, r.chip, r.mac = d.port, d.chip, d.mac

        if not _run(self.cfg.flash_for(board), self.opts.repo, example=example, port=d.port):
            r.verdict, r.detail = "fail", "flash failed"
            return

        log = self._log_path(scenario, example, stamp)
        r.log = str(log.relative_to(self.opts.repo))
        # Resets into the app on open, as the board's `reset` says.
        with SerialLines(d.port, log, baud=board.baud, reset=board.reset) as lines:
            selected = steps_for(scenario, self.opts.mode)
            chosen = {id(s) for s in selected}
            total = len(selected)
            captured: dict[str, str] = {}  # named groups matched so far, for `(?P=name)`
            n = 0
            for step in scenario.steps:
                # A step that declares a name owns it from here on: whatever it does not
                # capture must not survive from an earlier step. Its own references are
                # resolved first, so it may still compare against the earlier value.
                declared = {name for p in step.expect for name in defines(p)}
                if id(step) not in chosen:
                    # Skipped by the mode: what it would have captured is unknown, so a
                    # later reference fails instead of taking an earlier step's value.
                    for name in declared:
                        captured.pop(name, None)
                    continue
                n += 1
                # What a prompt, a probe or a run may use: only values that exist. A capture
                # never stands in for a reserved name, so an unknown `{mac}` stays unknown.
                subst = {k: v for k, v in captured.items() if k not in PROBE_FIELDS}
                subst |= {"port": d.port, "example": example, "board": board.key, "chip": d.chip}
                if d.mac is not None:
                    subst["mac"] = d.mac
                say, lacked = render_prompt(step.say, subst)
                confirm = None
                if step.confirm is not None:
                    confirm, more = render_prompt(step.confirm, subst)
                    lacked += tuple(name for name in more if name not in lacked)
                unset = placeholders((*step.probe, *step.run)) - set(subst)
                missing = [
                    f"(?P={name})"
                    for name in sorted(
                        {
                            name
                            for p in (*step.expect, step.reject or "", step.serial_reject or "")
                            for name in references(p)
                        }
                        - set(captured)
                    )
                ] + sorted(unset - PROBE_FIELDS)
                if missing or unset:
                    ui.step(n, total, say)
                    _warn_lacked(lacked)
                    detail = (
                        f"nothing captured for {missing[0]}: its step was skipped or failed"
                        if missing
                        else "no value for {mac}: the board's MAC address is unknown"
                    )
                    res = StepResult(say, False, detail, optional=step.optional, lacked=lacked)
                    ui.verdict(res.ok, res.detail, res.optional, res.unverified)
                    r.steps.append(res)
                    for name in declared:
                        captured.pop(name, None)
                    if res.failed and not self._ask_muted(
                        lines, "Continue with the next step?", False
                    ):
                        break
                    continue
                step = replace(
                    step,
                    say=say,
                    confirm=confirm,
                    expect=tuple(resolve(p, captured) for p in step.expect),
                    reject=resolve(step.reject, captured) if step.reject else None,
                    serial_reject=(
                        resolve(step.serial_reject, captured) if step.serial_reject else None
                    ),
                )
                for name in declared:
                    captured.pop(name, None)
                while True:
                    ui.step(n, total, step.say)
                    seconds: float | None = None
                    if step.arm and step.confirm is None:
                        # Start the clock only once the operator has read the
                        # instruction, and judge only what happens from now on.
                        if not self.opts.assume_yes:
                            if step.duration is not None:
                                seconds = self._ready_duration_quietly(
                                    lines, step.say, step.duration
                                )
                            else:
                                self._ready_quietly(lines, step.say)
                        if not step.probe:
                            lines.drain()
                    # An attended watch ends early on Enter: the operator has seen enough.
                    stop = (
                        ui.enter_pressed
                        if step.attended and step.duration is not None and not self.opts.assume_yes
                        else None
                    )
                    res = self._attempt(step, subst, lines, log, n, seconds, stop)
                    res = replace(res, lacked=lacked)
                    ui.verdict(res.ok, res.detail, res.optional, res.unverified)
                    # `-y` must not retry: answering its default (yes) would loop forever.
                    if not res.failed or self.opts.assume_yes:
                        break
                    if not self._ask_muted(lines, "Retry this step?"):
                        break
                _warn_lacked(lacked)
                if res.failed and not self.opts.assume_yes:
                    res = replace(res, note=self._note_quietly(lines))
                captured.update(res.captured)
                r.steps.append(res)
                if res.failed and not self._ask_muted(lines, "Continue with the next step?", False):
                    break

        failed = [s for s in r.steps if s.failed]
        unverified = [s for s in r.steps if s.unverified]
        complete = len(r.steps) == len(selected)
        if failed or not complete:
            r.verdict = "fail"
        else:
            # An optional step nobody looked at is reported, but holds nothing back.
            required = any(not s.optional for s in unverified)
            r.verdict = "unverified" if required else "pass"
        passed = len(r.steps) - len(failed) - len(unverified)
        r.detail = f"{passed}/{len(selected)} steps passed"
        if unverified:
            r.detail += f", {len(unverified)} unverified"

    def _attempt(
        self,
        step: Step,
        subst: dict[str, str],
        serial: SerialLines,
        log: Path,
        n: int,
        seconds: float | None,
        stop: Callable[[], bool] | None,
    ) -> StepResult:
        """One try at a step: reset, start the probe, run the action, then judge.

        A probe step judges its command's stdout; its serial lines are judged only against
        `serial_reject`. A failing `run` command fails the step before any judging.
        """
        if step.reset and not step.probe and not serial.reset():
            return StepResult(step.say, False, "the board did not reset", optional=step.optional)
        started = time.monotonic()
        record: CommandRecord | None = None
        with ExitStack() as stack:
            probe: ProbeLines | None = None
            guard: GuardedLines | None = None
            source: LineSource = serial
            if step.probe:
                argv = [part.format(**subst) for part in step.probe]
                ui.info("$ " + " ".join(argv))
                probe_log = log.with_name(f"{log.stem}.probe{n}{log.suffix}")
                try:
                    probe = stack.enter_context(self.open_probe(argv, self.opts.repo, probe_log))
                except (FileNotFoundError, PermissionError) as e:
                    raise device.missing_tool(argv[0], e) from e
                source = probe
                if step.serial_reject:
                    guard = GuardedLines(probe, serial, re.compile(step.serial_reject))
                    source = guard
            if step.run:
                if step.duration is not None and not step.reset and not step.probe:
                    # judge the board's reaction to the action, not what earlier steps left behind
                    serial.drain()
                run_log = log.with_name(f"{log.stem}.run{n}{log.suffix}")
                record = self._host(
                    [part.format(**subst) for part in step.run], run_log, step.run_timeout
                )
                if not record.ok:
                    return StepResult(
                        step.say,
                        False,
                        f"run failed: {record.summary()}",
                        optional=step.optional,
                        run=record,
                    )
            try:
                res = run_step(
                    step,
                    source,
                    lambda q: self._ask_quietly(serial, q),
                    duration=seconds,
                    stop=stop,
                )
                if guard is not None and res.ok:
                    guard.check()  # a line that came in after the last poll still counts
            except SerialRejected as e:
                res = StepResult(
                    step.say, False, f"rejected serial line: {e.line!r}", optional=step.optional
                )
            if res.failed and probe is not None:
                res = replace(res, detail=f"{res.detail}; {probe.exit_note()}")
        if step.probe and serial.link_lost_since(started):
            ui.warn("serial connection lost during this step; the board was not judged")
        return replace(res, run=record) if record is not None else res

    def _record(self, scenario: Scenario, r: ExampleResult) -> None:
        path = self.cfg.board_dir(scenario.board) / "results.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    def summary(self) -> None:
        ui.heading("Summary")
        for r in self.results:
            ui.verdict(
                r.verdict == "pass",
                f"{r.example:28} {r.verdict:10} {r.detail}",
                unverified=r.verdict == "unverified",
                skipped=r.verdict == "skipped",
            )
            for s in r.steps:
                if s.note:
                    print(f"      note: {s.say}: {s.note}")
            if r.log:
                print(f"      log: {r.log}")
