"""Drive one bench session: build, find the board, flash, reset, guide the steps, record."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import device, ui
from .config import BenchConfig, Scenario
from .serial_lines import SerialLines
from .steps import StepResult, run_step


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
    verdict: str = "skipped"
    detail: str = ""
    steps: list[StepResult] = field(default_factory=list)


@dataclass
class Options:
    repo: Path
    port: str | None = None
    build: bool = True
    assume_yes: bool = False


def _run(cmd: tuple[str, ...], repo: Path, **subst: str) -> bool:
    argv = [part.format(**subst) for part in cmd]
    ui.info("$ " + " ".join(argv))
    try:
        return subprocess.run(argv, cwd=repo, env=device.ESPFLASH_ENV).returncode == 0
    except (FileNotFoundError, PermissionError) as e:
        raise RuntimeError(f"cannot run {argv[0]!r}: {e}") from e


def _overall(verdicts: list[str]) -> str:
    if not verdicts:
        return "fail"
    if "error" in verdicts:
        return "error"
    if any(v not in ("pass", "unverified") for v in verdicts):
        return "fail"
    return "unverified" if "unverified" in verdicts else "pass"


class Session:
    def __init__(self, cfg: BenchConfig, opts: Options):
        self.cfg = cfg
        self.opts = opts
        self.device: device.Device | None = None
        self.results: list[ExampleResult] = []

    def ask(self, question: str, default: bool = True) -> bool:
        return default if self.opts.assume_yes else ui.ask(question, default)

    def _ready_quietly(self, lines: SerialLines, msg: str) -> None:
        lines.echo = False
        try:
            ui.ready(msg)
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

    def _device_for(self, scenario: Scenario) -> device.Device:
        d = self.device
        if d and os.path.exists(d.port) and device.matches(scenario.board, d):
            return d
        self.device = device.wait_for(scenario.board, self.opts.port)
        return self.device

    def run(self, scenarios: tuple[Scenario, ...], first_example: str | None) -> str:
        """Run the scenarios and return the worst verdict: error > fail > unverified > pass.

        An empty run is a fail, never a pass.
        """
        for n, scenario in enumerate(scenarios):
            if n > 0 and not self.ask(f"Next check: {scenario.board.key}/{scenario.name}?"):
                break
            examples = scenario.examples
            if first_example in examples:
                examples = examples[examples.index(first_example) :]
            for i, example in enumerate(examples):
                if i > 0 and not self.ask(f"Start {example}?"):
                    break
                self.results.append(self.run_example(scenario, example))
        self.summary()
        return _overall([r.verdict for r in self.results])

    def run_example(self, scenario: Scenario, example: str) -> ExampleResult:
        stamp = datetime.now()
        r = ExampleResult(
            example=example,
            scenario=scenario.name,
            board=scenario.board.key,
            started=stamp.isoformat(timespec="seconds"),
        )
        ui.heading(f"{scenario.board.key}/{scenario.name}: {example}")
        try:
            self._run_example(scenario, example, stamp, r)
        except KeyboardInterrupt:
            print()
            r.verdict, r.detail = "aborted", "Ctrl-C"
            ui.warn("aborted")
        except Exception as e:  # any other failure must still leave a recorded row
            r.verdict, r.detail = "error", f"{type(e).__name__}: {e}"
            ui.warn(f"error: {r.detail}")
        self._record(scenario, r)
        return r

    def _run_example(
        self, scenario: Scenario, example: str, stamp: datetime, r: ExampleResult
    ) -> None:
        if self.opts.build and not _run(self.cfg.build, self.opts.repo, example=example):
            r.verdict, r.detail = "fail", "build failed"
            return

        d = self._device_for(scenario)
        r.port, r.chip, r.mac = d.port, d.chip, d.mac

        if not _run(self.cfg.flash, self.opts.repo, example=example, port=d.port):
            r.verdict, r.detail = "fail", "flash failed"
            return

        log = self.cfg.board_dir(scenario.board) / "logs" / f"{example}-{stamp:%Y%m%d-%H%M%S}.log"
        r.log = str(log.relative_to(self.opts.repo))
        with SerialLines(d.port, log) as lines:  # resets into the app on open
            total = len(scenario.steps)
            for n, step in enumerate(scenario.steps, 1):
                while True:
                    ui.step(n, total, step.say)
                    if step.arm and step.confirm is None:
                        # Start the clock only once the operator has read the
                        # instruction, and judge only what happens from now on.
                        if not self.opts.assume_yes:
                            self._ready_quietly(lines, step.say)
                        lines.drain()
                    res = run_step(step, lines, lambda q: self._ask_quietly(lines, q))
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
        complete = len(r.steps) == len(scenario.steps)
        if failed or not complete:
            r.verdict = "fail"
        else:
            # An optional step nobody looked at is reported, but holds nothing back.
            required = any(not s.optional for s in unverified)
            r.verdict = "unverified" if required else "pass"
        passed = len(r.steps) - len(failed) - len(unverified)
        r.detail = f"{passed}/{len(scenario.steps)} steps passed"
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
            )
            for s in r.steps:
                if s.note:
                    print(f"      note: {s.say}: {s.note}")
            if r.log:
                print(f"      log: {r.log}")
