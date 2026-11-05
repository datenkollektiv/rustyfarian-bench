"""Drive one bench session: build, find the board, flash, reset, guide the steps, record."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
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
    return subprocess.run(argv, cwd=repo, env=device.ESPFLASH_ENV).returncode == 0


def _reset(port: str) -> None:
    subprocess.run(
        ["espflash", "reset", "--port", port, "--non-interactive"],
        capture_output=True,
        env=device.ESPFLASH_ENV,
    )


class Session:
    def __init__(self, cfg: BenchConfig, opts: Options):
        self.cfg = cfg
        self.opts = opts
        self.device: device.Device | None = None
        self.results: list[ExampleResult] = []

    def ask(self, question: str, default: bool = True) -> bool:
        return default if self.opts.assume_yes else ui.ask(question, default)

    def _device_for(self, scenario: Scenario) -> device.Device:
        d = self.device
        if d and os.path.exists(d.port) and device.matches(scenario.board, d):
            return d
        self.device = device.wait_for(scenario.board, self.opts.port)
        return self.device

    def run(self, scenarios: tuple[Scenario, ...], first_example: str | None) -> bool:
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
        return all(r.verdict == "pass" for r in self.results)

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
        _reset(d.port)
        with SerialLines(d.port, log) as lines:
            total = len(scenario.steps)
            for n, step in enumerate(scenario.steps, 1):
                while True:
                    ui.step(n, total, step.say)
                    res = run_step(step, lines, lambda q: self.ask(q, default=True))
                    ui.verdict(res.ok, res.detail, res.optional)
                    if res.counts_as_pass or not self.ask("Retry this step?"):
                        break
                r.steps.append(res)
                if not res.counts_as_pass and not self.ask("Continue with the next step?", False):
                    break

        failed = [s for s in r.steps if not s.counts_as_pass]
        complete = len(r.steps) == len(scenario.steps)
        r.verdict = "pass" if complete and not failed else "fail"
        r.detail = f"{len(r.steps) - len(failed)}/{len(scenario.steps)} steps passed"

    def _record(self, scenario: Scenario, r: ExampleResult) -> None:
        path = self.cfg.board_dir(scenario.board) / "results.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    def summary(self) -> None:
        ui.heading("Summary")
        for r in self.results:
            ui.verdict(r.verdict == "pass", f"{r.example:28} {r.verdict:8} {r.detail}")
            if r.log:
                print(f"      log: {r.log}")
