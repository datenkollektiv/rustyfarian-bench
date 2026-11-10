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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


class ConfigError(Exception):
    """The bench definition is missing or malformed."""


ResetMode = Literal["rts", "dtr", "none"]
RESET_MODES: tuple[ResetMode, ...] = ("rts", "dtr", "none")


@dataclass(frozen=True)
class Board:
    key: str
    chip: str
    name: str
    mac: str | None = None
    checks: tuple[str, ...] = ()
    # Per-board commands override `[commands]`; empty means use the global one.
    build: tuple[str, ...] = ()
    flash: tuple[str, ...] = ()
    # `False` for boards espflash cannot identify (e.g. AVR): the port is taken as given.
    detect: bool = True
    # Serial settings: reset via RTS (native USB), DTR (auto-reset bridges) or not at all.
    # "none" sends no reset pulse but opens like an ESP (DTR briefly high), which can
    # still reset a DTR auto-reset board: such a board belongs on "dtr".
    baud: int = 115_200
    reset: ResetMode = "rts"
    # USB identity of an undetected board, `"vid:pid"` in hex, plus an optional serial number:
    # the port is found by it, so `--port` is not needed.
    usb_id: str | None = None
    usb_serial: str | None = None


# Placeholders a probe command or a prompt may use besides the names earlier steps captured.
PROBE_FIELDS = frozenset({"port", "example", "board", "chip", "mac"})
# Placeholders a scenario's `setup` may use: it runs before the board is detected.
SETUP_FIELDS = frozenset({"example", "board"})


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
    - `attended`: the step needs a person although it has no `confirm` (a PIR, a button);
      `--unattended` skips it and `--attended` keeps it.
    - `reset`: reset the board once the step starts (after Enter when armed), for a check
      the firmware runs at boot, e.g. a calibration window.
    - `probe`: a host command (argv) whose stdout is judged instead of the serial lines, with
      `expect` or `duration`; `{port}`, `{example}`, `{board}`, `{chip}`, `{mac}` and captures
      fill in.
    - `serial_reject`: with `probe`, a pattern that fails the step when a serial line matches it.
    - `run`: a host command (argv) run once the step starts, before it is judged; same
      placeholders as `probe`. A failing command fails the step. `run_timeout` bounds it.

    `say` and `confirm` take the same placeholders, `{mac}` included; a value that is missing
    at run time shows as a marker such as `{ip}?` and never fails the prompt. A literal brace
    is written `{{` or `}}`.

    A named group in an `expect` pattern, `(?P<pole>SOUTH|NORTH)`, captures the matched text;
    a later step's `expect` or `reject` refers to it as `(?P=pole)`, matched literally.
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
    attended: bool = False
    reset: bool = False
    probe: tuple[str, ...] = ()
    run: tuple[str, ...] = ()
    run_timeout: float = 30.0
    serial_reject: str | None = None

    @property
    def needs_person(self) -> bool:
        return self.confirm is not None or self.attended


@dataclass(frozen=True)
class Scenario:
    name: str
    board: Board
    examples: tuple[str, ...]
    steps: tuple[Step, ...]
    path: Path
    # Host commands around each example; `hook_timeout` bounds every one of them.
    setup: tuple[tuple[str, ...], ...] = ()
    teardown: tuple[tuple[str, ...], ...] = ()
    hook_timeout: float = 60.0


@dataclass(frozen=True)
class BenchConfig:
    root: Path
    build: tuple[str, ...]
    flash: tuple[str, ...]
    boards: dict[str, Board]
    scenarios: tuple[Scenario, ...] = field(default=())

    def board_dir(self, board: Board) -> Path:
        return self.root / board.key

    def build_for(self, board: Board) -> tuple[str, ...]:
        return board.build or self.build

    def flash_for(self, board: Board) -> tuple[str, ...]:
        return board.flash or self.flash

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


def _baud(raw: dict[str, Any], where: str) -> int:
    value = raw.get("baud", 115_200)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}.baud: must be a positive integer")
    return value


def _reset(raw: dict[str, Any], where: str) -> ResetMode:
    value = raw.get("reset", "rts")
    for mode in RESET_MODES:
        if value == mode:
            return mode
    raise ConfigError(f"{where}.reset: must be one of {', '.join(RESET_MODES)}")


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


# `(?P=name)` outside the group's own pattern refers to what an earlier step captured.
_REF = re.compile(r"\(\?P=(\w+)\)")
_GROUP = re.compile(r"\(\?P<(\w+)>")
_GLOBAL_FLAGS = re.compile(r"(?:\(\?[aiLmsux]+\))+")  # global flag groups must lead the pattern
_SCOPED = re.compile(r"\(\?([aiLmsux]*)(?:-([imsx]+))?:")  # `(?x:...)`, `(?-x:...)`


def _scan(pattern: str) -> Iterator[tuple[str, re.Match[str]]]:
    """Each `(?P=name)` ("ref") and `(?P<name>` ("group") that is regex syntax.

    Skips escapes, character classes, `(?#...)` comments and, where verbose mode is on
    (`x` in the leading global flag groups, or inside `(?x:...)` until `(?-x:...)`),
    `#` comments: what looks like a reference there is literal text.
    """
    leading = _GLOBAL_FLAGS.match(pattern)
    verbose = leading is not None and "x" in leading.group(0)
    outer: list[bool] = []  # the verbose state to restore at each open group's `)`
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
        elif c == "[":
            i += 1
            if pattern.startswith("^", i):
                i += 1
            if pattern.startswith("]", i):  # a leading ] is a member, not the end
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
        elif (c == "#" and verbose) or pattern.startswith("(?#", i):
            end = pattern.find("\n" if c == "#" else ")", i)
            i = n if end < 0 else end + 1
        elif m := _REF.match(pattern, i):  # closes itself
            yield "ref", m
            i = m.end()
        elif m := _GROUP.match(pattern, i):
            yield "group", m
            outer.append(verbose)
            i = m.end()
        elif m := _SCOPED.match(pattern, i):
            outer.append(verbose)
            on, off = m.group(1), m.group(2) or ""
            verbose = (verbose or "x" in on) and "x" not in off
            i = m.end()
        elif c == "(":
            outer.append(verbose)
            i += 1
        elif c == ")":
            if outer:
                verbose = outer.pop()
            i += 1
        else:
            i += 1


def defines(pattern: str) -> set[str]:
    """Names `pattern` defines with `(?P<name>...)`, whether or not they end up matched."""
    return {m.group(1) for kind, m in _scan(pattern) if kind == "group"}


def references(pattern: str) -> set[str]:
    """Names `pattern` takes from earlier steps: `(?P=name)` with no `(?P<name>...)` of its own."""
    found = list(_scan(pattern))
    own = {m.group(1) for kind, m in found if kind == "group"}
    return {m.group(1) for kind, m in found if kind == "ref"} - own


def resolve(pattern: str, values: dict[str, str]) -> str:
    """`pattern` with each earlier-step reference replaced by its captured text, literally."""
    found = list(_scan(pattern))
    own = {m.group(1) for kind, m in found if kind == "group"}
    out, at = [], 0
    for kind, m in found:
        if kind == "ref" and m.group(1) not in own:
            out += [pattern[at : m.start()], f"(?:{re.escape(values[m.group(1)])})"]
            at = m.end()
    return "".join(out) + pattern[at:]


def _pattern(pattern: str, key: str, where: str, known: frozenset[str] = frozenset()) -> None:
    unknown = sorted(references(pattern) - known)
    if unknown:
        raise ConfigError(
            f"{where}: '{key}' refers to (?P={unknown[0]}), which no earlier step's expect captures"
        )
    pattern = resolve(pattern, dict.fromkeys(known, "x"))
    try:
        re.compile(pattern)
    except re.error as e:
        raise ConfigError(f"{where}: invalid regex in '{key}' ({pattern!r}): {e}") from None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _check_fields(part: str, where: str, allowed: set[str]) -> None:
    """Fail on a brace that is malformed or names a field outside `allowed`."""
    try:
        parsed = list(string.Formatter().parse(part))
    except ValueError as e:
        raise ConfigError(f"{where}: bad placeholder in {part!r}: {e}") from None
    if any(spec and "{" in spec for _, f, spec, _ in parsed if f is not None):
        raise ConfigError(
            f"{where}: nested placeholder in {part!r}: a format spec cannot contain a field"
        )
    fields = [f for _, f, _, _ in parsed if f is not None]
    bad = [f for f in fields if f not in allowed]
    if bad:
        raise ConfigError(f"{where}: unknown placeholder {{{bad[0]}}}; allowed: {sorted(allowed)}")
    try:  # a dry run catches bad conversions, format specs and fields nested in a spec
        string.Formatter().vformat(part, (), dict.fromkeys(allowed, "x"))
    except (ValueError, KeyError, IndexError) as e:
        raise ConfigError(f"{where}: bad placeholder in {part!r}: {e}") from None


def _command(value: Any, where: str, allowed: set[str]) -> tuple[str, ...]:
    command = _str_tuple(value, where)
    if not command:
        raise ConfigError(f"{where}: must not be empty")
    for part in command:
        _check_fields(part, where, allowed)
    return command


def placeholders(command: Iterable[str]) -> set[str]:
    """Field names a command's parts use, e.g. `{example}`; `{{` escapes are not fields."""
    return {f for part in command for _, f, _, _ in string.Formatter().parse(part) if f is not None}


class _Prompt(string.Formatter):
    """Fills a prompt; a name with no value becomes `{name}?` and is noted in `lacked`."""

    def __init__(self, values: dict[str, str]):
        super().__init__()
        self.values = values
        self.lacked: list[str] = []

    def get_value(self, key: int | str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
        name = str(key)
        if name in self.values:
            return self.values[name]
        if name not in self.lacked:
            self.lacked.append(name)
        return f"{{{name}}}?"


def render_prompt(text: str, values: dict[str, str]) -> tuple[str, tuple[str, ...]]:
    """A `say` or `confirm` with its fields filled in, and the names that had no value.

    A missing value shows as `{name}?`; each missing name is listed once, in order of appearance.
    """
    prompt = _Prompt(values)
    return prompt.vformat(text, (), {}), tuple(prompt.lacked)


def _parse_step(raw: Any, where: str, captures: frozenset[str] = frozenset()) -> Step:
    """One `[[step]]`; `captures` are the group names earlier steps' `expect` patterns capture."""
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
        "attended",
        "reset",
        "probe",
        "run",
        "run_timeout",
        "serial_reject",
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
            _pattern(p, "expect", where, captures)
    reject = _text(raw, "reject", where)
    if reject is not None:
        _pattern(reject, "reject", where, captures)
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
    allowed = set(PROBE_FIELDS | captures)
    _check_fields(say, f"{where}: say", allowed)
    if confirm is not None:
        _check_fields(confirm, f"{where}: confirm", allowed)
    kinds = sum(k in raw for k in ("expect", "duration", "confirm"))
    if kinds != 1:
        raise ConfigError(f"{where}: needs exactly one of 'expect', 'duration' or 'confirm'")
    attended = _bool(raw, "attended", False, where)
    reset = _bool(raw, "reset", False, where)
    probe: tuple[str, ...] = ()
    if "probe" in raw:
        probe = _command(raw["probe"], f"{where}: probe", allowed)
        for key, used in (("confirm", confirm is not None), ("reset", reset)):
            if used:
                raise ConfigError(f"{where}: 'probe' cannot be combined with '{key}'")
    serial_reject = _text(raw, "serial_reject", where)
    if serial_reject is not None:
        if not probe:
            raise ConfigError(f"{where}: 'serial_reject' needs 'probe'")
        _pattern(serial_reject, "serial_reject", where, captures)
    run: tuple[str, ...] = ()
    if "run" in raw:
        if confirm is not None:
            raise ConfigError(f"{where}: 'run' cannot be combined with 'confirm'")
        run = _command(raw["run"], f"{where}: run", allowed)
    run_timeout = _number(raw, "run_timeout", 30.0, where)
    assert run_timeout is not None
    if "run_timeout" in raw:
        if not run:
            raise ConfigError(f"{where}: 'run_timeout' needs 'run'")
        if run_timeout <= 0:
            raise ConfigError(f"{where}: 'run_timeout' must be > 0")
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
        attended=attended,
        reset=reset,
        probe=probe,
        run=run,
        run_timeout=run_timeout,
        serial_reject=serial_reject,
    )


def _commands(
    raw: dict[str, Any], key: str, where: str, allowed: set[str]
) -> tuple[tuple[str, ...], ...]:
    """A scenario's `setup` or `teardown`: a list of argv lists; `()` when absent."""
    if key not in raw:
        return ()
    value = raw[key]
    if not isinstance(value, list):
        raise ConfigError(f'{where}: \'{key}\' must be a list of argv lists, e.g. [["just", "up"]]')
    return tuple(_command(item, f"{where}: {key}[{i}]", allowed) for i, item in enumerate(value))


def _board_command(raw: dict[str, Any], key: str, where: str, allowed: set[str]) -> tuple[str, ...]:
    """A board's own command, checked like `[commands]`; `()` when absent."""
    return _command(raw[key], f"{where}.{key}", allowed) if key in raw else ()


def _parse_board(key: str, raw: Any, where: str) -> Board:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    chip = _text(raw, "chip", where)
    if chip is None:
        raise ConfigError(f"{where}: missing 'chip'")
    name = _text(raw, "name", where)
    checks = _str_tuple(raw.get("checks", []), f"{where}.checks")
    if duplicated := sorted({c for c in checks if checks.count(c) > 1}):
        raise ConfigError(f"{where}.checks: duplicate names {duplicated}")
    detect = _bool(raw, "detect", True, where)
    usb_id = _text(raw, "usb_id", where)
    usb_serial = _text(raw, "usb_serial", where)
    if usb_id is not None and not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", usb_id):
        raise ConfigError(f"{where}.usb_id: expected 'vid:pid' in hex, e.g. '0403:6001'")
    if usb_serial is not None and usb_id is None:
        raise ConfigError(f"{where}.usb_serial: needs usb_id")
    if usb_id is not None and detect:
        raise ConfigError(f"{where}.usb_id: only for boards with detect = false")
    return Board(
        key=key,
        chip=chip,
        name=key if name is None else name,
        mac=_text(raw, "mac", where),
        checks=checks,
        build=_board_command(raw, "build", where, {"example"}),
        flash=_board_command(raw, "flash", where, {"example", "port"}),
        detect=detect,
        baud=_baud(raw, where),
        reset=_reset(raw, where),
        usb_id=None if usb_id is None else usb_id.lower(),
        usb_serial=usb_serial,
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
            parsed: list[Step] = []
            captures: frozenset[str] = frozenset()
            for i, s in enumerate(raw_steps):
                parsed.append(_parse_step(s, f"{where} step {i + 1}", captures))
                for p in parsed[-1].expect:
                    groups = re.compile(resolve(p, dict.fromkeys(captures, "x"))).groupindex
                    captures |= set(groups)
            steps = tuple(parsed)
            setup = _commands(raw, "setup", where, set(SETUP_FIELDS))
            teardown = _commands(raw, "teardown", where, set(PROBE_FIELDS))
            hook_timeout = _number(raw, "hook_timeout", 60.0, where)
            assert hook_timeout is not None
            if hook_timeout <= 0:
                raise ConfigError(f"{where}: 'hook_timeout' must be > 0")
            if board.reset == "none" and any(st.reset for st in steps):
                raise ConfigError(f"{where}: a step sets 'reset', but board {key} has reset = none")
            if not steps:
                raise ConfigError(f"{where}: no [[step]] entries")
            scenarios.append(
                Scenario(
                    name=path.stem,
                    board=board,
                    examples=_examples(raw["examples"], where),
                    steps=steps,
                    path=path,
                    setup=setup,
                    teardown=teardown,
                    hook_timeout=hook_timeout,
                )
            )

    return BenchConfig(
        root=root, build=build, flash=flash, boards=boards, scenarios=tuple(scenarios)
    )
