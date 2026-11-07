"""Load a consumer repo's bench definition: `bench/bench.toml` plus `bench/<board>/*.toml`.

Layout in the consumer repo:

    bench/bench.toml          commands and boards
    bench/<board>/<check>.toml  one scenario: examples run in order, shared steps

A board's optional `checks` list runs those checks first, in that order; the rest follow by name.
"""

from __future__ import annotations

import math
import re
import string
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
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    """One guided step.

    - `expect`: patterns that must appear in order within `timeout` seconds.
    - `reject`: a pattern that fails the step whenever it appears.
    - `duration`: watch-only window (no `expect`); passes if `reject` never matches.
    - `settle`: after the last `expect` match, keep watching `reject` this long.
    - `confirm`: a yes/no question for checks the output cannot prove.
    - `optional`: a miss is reported as a warning, not a failure.
    - `arm`: wait for Enter and drop the backlog before the step starts (default);
      `false` for steps that must read what the reset already printed.
    """

    say: str
    expect: tuple[str, ...] = ()
    reject: str | None = None
    timeout: float = 30.0
    duration: float | None = None
    settle: float = 0.0
    confirm: str | None = None
    optional: bool = False
    arm: bool = True


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


def _bool(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be true or false")
    return value


def _number(raw: dict[str, Any], key: str, default: float | None, where: str) -> float | None:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{where}: '{key}' must be a finite number")
    return float(value)


def _text(raw: dict[str, Any], key: str, where: str) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value


def _pattern(pattern: str, key: str, where: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"{where}: invalid regex in '{key}' ({pattern!r}): {e}") from None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _command(value: Any, where: str, allowed: set[str]) -> tuple[str, ...]:
    command = _str_tuple(value, where)
    if not command:
        raise ConfigError(f"{where}: must not be empty")
    for part in command:
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(part) if f is not None]
        except ValueError as e:
            raise ConfigError(f"{where}: bad placeholder in {part!r}: {e}") from None
        bad = [f for f in fields if f not in allowed]
        if bad:
            raise ConfigError(
                f"{where}: unknown placeholder {{{bad[0]}}}; allowed: {sorted(allowed)}"
            )
    return command


def _parse_step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    known = {
        "say",
        "expect",
        "reject",
        "timeout",
        "duration",
        "settle",
        "confirm",
        "optional",
        "arm",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    say = _text(raw, "say", where)
    if say is None:
        raise ConfigError(f"{where}: missing 'say'")
    expect: tuple[str, ...] = ()
    if "expect" in raw:
        expect = _str_tuple(raw["expect"], f"{where}: expect")
        if not expect or not all(p.strip() for p in expect):
            raise ConfigError(f"{where}: 'expect' must hold non-empty patterns")
        for p in expect:
            _pattern(p, "expect", where)
    reject = _text(raw, "reject", where)
    if reject is not None:
        _pattern(reject, "reject", where)
    timeout = _number(raw, "timeout", 30.0, where)
    duration = _number(raw, "duration", None, where)
    settle = _number(raw, "settle", 0.0, where)
    assert timeout is not None and settle is not None
    if timeout <= 0:
        raise ConfigError(f"{where}: 'timeout' must be > 0")
    if duration is not None and duration <= 0:
        raise ConfigError(f"{where}: 'duration' must be > 0")
    if settle < 0:
        raise ConfigError(f"{where}: 'settle' must be >= 0")
    confirm = _text(raw, "confirm", where)
    kinds = sum(k in raw for k in ("expect", "duration", "confirm"))
    if kinds != 1:
        raise ConfigError(f"{where}: needs exactly one of 'expect', 'duration' or 'confirm'")
    return Step(
        say=say,
        expect=expect,
        reject=reject,
        timeout=timeout,
        duration=duration,
        settle=settle,
        confirm=confirm,
        optional=_bool(raw, "optional", False, where),
        arm=_bool(raw, "arm", True, where),
    )


def _parse_board(key: str, raw: Any, where: str) -> Board:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    chip = _text(raw, "chip", where)
    if chip is None:
        raise ConfigError(f"{where}: missing 'chip'")
    name = _text(raw, "name", where)
    return Board(
        key=key,
        chip=chip,
        name=key if name is None else name,
        mac=_text(raw, "mac", where),
        checks=_str_tuple(raw.get("checks", []), f"{where}.checks"),
    )


def _examples(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):  # a single example, as before
        value = [value]
    ok = (
        isinstance(value, list)
        and value
        and all(isinstance(v, str) and v and "/" not in v and ".." not in v for v in value)
    )
    if not ok:
        raise ConfigError(
            f"{where}: 'examples' must be a non-empty list of names without '/' or '..'"
        )
    return tuple(value)


def _table(data: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: '{key}' must be a table")
    return value


def load(repo: Path) -> BenchConfig:
    root = repo / "bench"
    main = root / "bench.toml"
    if not main.is_file():
        raise ConfigError(f"{main} not found")
    data = _read_toml(main)

    commands = _table(data, "commands", str(main))
    try:
        build = _command(commands["build"], "commands.build", {"example"})
        flash = _command(commands["flash"], "commands.flash", {"example", "port"})
    except KeyError as e:
        raise ConfigError(f"{main}: missing commands.{e.args[0]}") from None

    boards = {
        key: _parse_board(key, b, f"boards.{key}")
        for key, b in _table(data, "boards", str(main)).items()
    }

    scenarios = []
    for key, board in sorted(boards.items()):
        paths = {p.stem: p for p in root.joinpath(key).glob("*.toml")}
        missing = [c for c in board.checks if c not in paths]
        if missing:
            raise ConfigError(f"{main}: boards.{key}.checks names no scenario: {missing}")
        rest = sorted(n for n in paths if n not in board.checks)
        for path in (paths[n] for n in (*board.checks, *rest)):
            raw = _read_toml(path)
            where = str(path.relative_to(repo))
            if "examples" not in raw:
                raise ConfigError(f"{where}: missing 'examples'")
            raw_steps = raw.get("step", [])
            if not isinstance(raw_steps, list):
                raise ConfigError(f"{where}: 'step' must be an array of [[step]] tables")
            steps = tuple(_parse_step(s, f"{where} step {i + 1}") for i, s in enumerate(raw_steps))
            if not steps:
                raise ConfigError(f"{where}: no [[step]] entries")
            scenarios.append(
                Scenario(
                    name=path.stem,
                    board=board,
                    examples=_examples(raw["examples"], where),
                    steps=steps,
                    path=path,
                )
            )

    return BenchConfig(
        root=root, build=build, flash=flash, boards=boards, scenarios=tuple(scenarios)
    )
