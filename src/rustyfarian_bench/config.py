"""Load a consumer repo's bench definition: `bench/bench.toml` plus `bench/<board>/*.toml`.

Layout in the consumer repo:

    bench/bench.toml          commands and boards
    bench/<board>/<check>.toml  one scenario: examples run in order, shared steps
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """The bench definition is missing or malformed."""


@dataclass(frozen=True)
class Board:
    key: str
    chip: str
    name: str
    mac: str | None = None


@dataclass(frozen=True)
class Step:
    """One guided step.

    - `expect`: patterns that must appear in order within `timeout` seconds.
    - `reject`: a pattern that fails the step whenever it appears.
    - `duration`: watch-only window (no `expect`); passes if `reject` never matches.
    - `settle`: after the last `expect` match, keep watching `reject` this long.
    - `confirm`: a yes/no question for checks the output cannot prove.
    - `optional`: a miss is reported as a warning, not a failure.
    """

    say: str
    expect: tuple[str, ...] = ()
    reject: str | None = None
    timeout: float = 30.0
    duration: float | None = None
    settle: float = 0.0
    confirm: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    board: Board
    examples: tuple[str, ...]
    steps: tuple[Step, ...]
    path: Path


@dataclass(frozen=True)
class BenchConfig:
    root: Path
    build: tuple[str, ...]
    flash: tuple[str, ...]
    boards: dict[str, Board]
    scenarios: tuple[Scenario, ...] = field(default=())

    def board_dir(self, board: Board) -> Path:
        return self.root / board.key

    def find(self, target: str) -> tuple[Scenario, ...]:
        """Scenarios for an example name, a `board/check` name, or a whole board."""
        by_example = tuple(s for s in self.scenarios if target in s.examples)
        if by_example:
            return by_example
        by_name = tuple(s for s in self.scenarios if f"{s.board.key}/{s.name}" == target)
        if by_name:
            return by_name
        by_board = tuple(s for s in self.scenarios if s.board.key == target)
        if by_board:
            return by_board
        raise ConfigError(f"no bench scenario for {target!r}")


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ConfigError(f"{where}: expected a string or a list of strings")


def _parse_step(raw: dict[str, Any], where: str) -> Step:
    known = {"say", "expect", "reject", "timeout", "duration", "settle", "confirm", "optional"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    if "say" not in raw:
        raise ConfigError(f"{where}: missing 'say'")
    step = Step(
        say=str(raw["say"]),
        expect=_str_tuple(raw["expect"], where) if "expect" in raw else (),
        reject=raw.get("reject"),
        timeout=float(raw.get("timeout", 30.0)),
        duration=float(raw["duration"]) if "duration" in raw else None,
        settle=float(raw.get("settle", 0.0)),
        confirm=raw.get("confirm"),
        optional=bool(raw.get("optional", False)),
    )
    kinds = sum([bool(step.expect), step.duration is not None, step.confirm is not None])
    if kinds != 1:
        raise ConfigError(f"{where}: needs exactly one of 'expect', 'duration' or 'confirm'")
    return step


def load(repo: Path) -> BenchConfig:
    root = repo / "bench"
    main = root / "bench.toml"
    if not main.is_file():
        raise ConfigError(f"{main} not found")
    data = tomllib.loads(main.read_text())

    commands = data.get("commands", {})
    try:
        build = _str_tuple(commands["build"], "commands.build")
        flash = _str_tuple(commands["flash"], "commands.flash")
    except KeyError as e:
        raise ConfigError(f"{main}: missing commands.{e.args[0]}") from None

    boards = {
        key: Board(key=key, chip=b["chip"], name=b.get("name", key), mac=b.get("mac"))
        for key, b in data.get("boards", {}).items()
    }

    scenarios = []
    for key, board in sorted(boards.items()):
        for path in sorted(root.joinpath(key).glob("*.toml")):
            raw = tomllib.loads(path.read_text())
            where = str(path.relative_to(repo))
            if "examples" not in raw:
                raise ConfigError(f"{where}: missing 'examples'")
            steps = tuple(
                _parse_step(s, f"{where} step {i + 1}") for i, s in enumerate(raw.get("step", []))
            )
            if not steps:
                raise ConfigError(f"{where}: no [[step]] entries")
            scenarios.append(
                Scenario(
                    name=path.stem,
                    board=board,
                    examples=_str_tuple(raw["examples"], f"{where}: examples"),
                    steps=steps,
                    path=path,
                )
            )

    return BenchConfig(
        root=root, build=build, flash=flash, boards=boards, scenarios=tuple(scenarios)
    )
