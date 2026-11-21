"""Guard the CLI contract that consumer repos rely on.

The snapshot is the surface as of v0.7.0. Changes must be additive: the only
allowed additions are `--status-file` on `serve` and a `kiosk` subcommand, and
both are optional here so the guard can land before them. The `kiosk` options
are not in the snapshot, so `test_kiosk_has_its_options` pins them. Anything
else that appears or disappears fails.
"""

import argparse
import re
from pathlib import Path
from typing import NoReturn

import pytest

from rustyfarian_bench import cli

# Top-level options.
TOP_OPTIONS = {"--repo"}

# Per subcommand: option strings, and positional names in order.
SNAPSHOT_OPTIONS: dict[str, set[str]] = {
    "run": {
        "--port",
        "--detect-timeout",
        "--unique-board",
        "--no-build",
        "-y",
        "--yes",
        "--no-twins",
        "--unattended",
        "--attended",
    },
    "submit": {"--bench", "--check", "--image", "--no-build", "--wait"},
    "serve": {
        "--host",
        "--port",
        "--outbox",
        "--job-timeout",
        "--detect-timeout",
        "--token-file",
        "--ssl-certfile",
        "--ssl-keyfile",
    },
    "list": set(),
    "detect": set(),
}
SNAPSHOT_POSITIONALS: dict[str, list[str]] = {
    "run": ["target", "check"],
    "submit": ["board", "example"],
    "serve": [],
    "list": [],
    "detect": [],
}

# Additive entries that may (not must) be present.
OPTIONAL_COMMANDS = {"kiosk"}
OPTIONAL_OPTIONS = {"serve": {"--status-file"}}


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


@pytest.fixture
def parser(monkeypatch: pytest.MonkeyPatch) -> argparse.ArgumentParser:
    """The real parser, as `main` builds it (it has no separate builder)."""

    def grab(self: argparse.ArgumentParser, *_: object, **__: object) -> NoReturn:
        raise _Captured(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    with pytest.raises(_Captured) as caught:
        cli.main(["list"])
    return caught.value.parser


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1
    return dict(actions[0].choices)


def _options(p: argparse.ArgumentParser) -> set[str]:
    return {
        s for a in p._actions if not isinstance(a, argparse._HelpAction) for s in a.option_strings
    }


def _positionals(p: argparse.ArgumentParser) -> list[str]:
    return [
        a.dest
        for a in p._actions
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
    ]


def test_top_level_options(parser: argparse.ArgumentParser) -> None:
    assert _options(parser) == TOP_OPTIONS


def test_subcommand_names(parser: argparse.ArgumentParser) -> None:
    names = set(_subparsers(parser))
    assert set(SNAPSHOT_OPTIONS) <= names
    assert names - set(SNAPSHOT_OPTIONS) <= OPTIONAL_COMMANDS


@pytest.mark.parametrize("command", sorted(SNAPSHOT_OPTIONS))
def test_subcommand_options(parser: argparse.ArgumentParser, command: str) -> None:
    got = _options(_subparsers(parser)[command])
    want = SNAPSHOT_OPTIONS[command]
    assert want <= got, f"{command}: removed {sorted(want - got)}"
    extra = got - want
    assert extra <= OPTIONAL_OPTIONS.get(command, set()), f"{command}: unexpected {sorted(extra)}"


@pytest.mark.parametrize("command", sorted(SNAPSHOT_POSITIONALS))
def test_subcommand_positionals(parser: argparse.ArgumentParser, command: str) -> None:
    assert _positionals(_subparsers(parser)[command]) == SNAPSHOT_POSITIONALS[command]


def test_kiosk_has_its_options(parser: argparse.ArgumentParser) -> None:
    got = _options(_subparsers(parser)["kiosk"])
    assert got == {
        "--status-file",
        "--title",
        "--address",
        "--fb-match",
        "--sleep-after",
        "--once",
        "--png-dir",
    }
    assert _positionals(_subparsers(parser)["kiosk"]) == []


UNITS = Path(__file__).parent.parent / "deploy" / "ansible" / "roles"


@pytest.mark.parametrize(
    ("unit", "command"),
    [
        ("bench/templates/rustyfarian-bench.service.j2", "serve"),
        ("display/templates/rustyfarian-bench-display.service.j2", "kiosk"),
    ],
)
def test_systemd_units_pass_only_options_the_command_has(
    parser: argparse.ArgumentParser, unit: str, command: str
) -> None:
    text = (UNITS / unit).read_text(encoding="utf-8").replace("\\\n", " ")
    (exec_start,) = (line for line in text.splitlines() if line.startswith("ExecStart="))
    assert f"rustyfarian-bench {command} " in exec_start
    flags = set(re.findall(r"(?<!\S)--[a-z][a-z-]*", exec_start))
    assert flags, exec_start
    assert flags <= _options(_subparsers(parser)[command])
