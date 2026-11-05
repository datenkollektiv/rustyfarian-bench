"""`rustyfarian-bench` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, device, ui
from .runner import Options, Session


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load(args.repo)
    scenarios = cfg.find(args.target)
    opts = Options(repo=args.repo, port=args.port, build=not args.no_build, assume_yes=args.yes)
    try:
        ok = Session(cfg, opts).run(scenarios, first_example=args.target)
    except KeyboardInterrupt:
        print()
        ui.warn("aborted")
        return 130
    return 0 if ok else 1


def _cmd_list(args: argparse.Namespace) -> int:
    cfg = config.load(args.repo)
    for s in cfg.scenarios:
        print(f"{s.board.key}/{s.name:16} {', '.join(s.examples)}")
    return 0


def _cmd_detect(_: argparse.Namespace) -> int:
    ports = device.candidate_ports()
    if not ports:
        ui.warn("no candidate serial ports")
        return 1
    for p in ports:
        d = device.probe(p)
        print(f"{p:32} {d.chip if d else '?':10} {d.mac if d else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rustyfarian-bench", description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="consumer repo with bench/bench.toml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a guided check")
    run.add_argument("target", help="example name, board/check, or board")
    run.add_argument("--port", help="serial port (default: auto-detect)")
    run.add_argument("--no-build", action="store_true", help="skip the build step")
    run.add_argument(
        "-y", "--yes", action="store_true", help="answer every prompt with its default"
    )
    run.set_defaults(func=_cmd_run)

    sub.add_parser("list", help="list scenarios").set_defaults(func=_cmd_list)
    sub.add_parser("detect", help="probe connected boards").set_defaults(func=_cmd_detect)

    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    try:
        return int(args.func(args))
    except config.ConfigError as e:
        ui.warn(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
