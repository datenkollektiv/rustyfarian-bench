"""Drive one bench session: build, find the board, flash, reset, guide the steps, record."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import device, ui
from .config import BenchConfig, Board, Scenario, Step
from .serial_lines import SerialLines
from .steps import StepResult, run_step

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
    def __init__(self, cfg: BenchConfig, opts: Options):
        self.cfg = cfg
        self.opts = opts
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
        self._record(scenario, r)
        return r

    def _run_example(
        self, scenario: Scenario, example: str, stamp: datetime, r: ExampleResult
    ) -> None:
        board = scenario.board
        if self.opts.build and not _run(self.cfg.build_for(board), self.opts.repo, example=example):
            r.verdict, r.detail = "fail", "build failed"
            return

        d = self._device_for(scenario)
        r.port, r.chip, r.mac = d.port, d.chip, d.mac

        if not _run(self.cfg.flash_for(board), self.opts.repo, example=example, port=d.port):
            r.verdict, r.detail = "fail", "flash failed"
            return

        log = self.cfg.board_dir(scenario.board) / "logs" / f"{example}-{stamp:%Y%m%d-%H%M%S}.log"
        r.log = str(log.relative_to(self.opts.repo))
        # Resets into the app on open, as the board's `reset` says.
        with SerialLines(d.port, log, baud=board.baud, reset=board.reset) as lines:
            selected = steps_for(scenario, self.opts.mode)
            total = len(selected)
            for n, step in enumerate(selected, 1):
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
                        lines.drain()
                    # An attended watch ends early on Enter: the operator has seen enough.
                    stop = (
                        ui.enter_pressed
                        if step.attended and step.duration is not None and not self.opts.assume_yes
                        else None
                    )
                    res = run_step(
                        step,
                        lines,
                        lambda q: self._ask_quietly(lines, q),
                        duration=seconds,
                        stop=stop,
                    )
                    ui.verdict(res.ok, res.detail, res.optional, res.unverified)
                    # `-y` must not retry: answering its default (yes) would loop forever.
                    if not res.failed or self.opts.assume_yes:
                        break
                    if not self.ask("Retry this step?"):
                        break
                if res.failed and not self.opts.assume_yes:
                    res = replace(res, note=self._note_quietly(lines))
                r.steps.append(res)
                if res.failed and not self.ask("Continue with the next step?", False):
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
