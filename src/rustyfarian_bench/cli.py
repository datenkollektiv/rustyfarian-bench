"""`rustyfarian-bench` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import client, config, device, remote, ui
from .runner import Options, Session


def _positive_seconds(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {text!r}")
    return seconds


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load(args.repo)
    target = f"{args.target}/{args.check}" if args.check else args.target
    scenarios = cfg.find(target)
    opts = Options(
        repo=args.repo,
        port=args.port,
        detect_timeout=args.detect_timeout,
        unique_board=args.unique_board,
        build=not args.no_build,
        assume_yes=args.yes or args.unattended,
        twins=not args.no_twins,
        mode="unattended" if args.unattended else "attended" if args.attended else None,
    )
    try:
        outcome = Session(cfg, opts).run(scenarios, first_example=target)
    except KeyboardInterrupt:
        print()
        ui.warn("aborted")
        return 130
    return remote.EXIT.get(outcome, 1)


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
        try:
            d, error = device.probe(p)
        except device.MissingTool as e:
            ui.warn(str(e))
            return 1
        print(f"{p:32} {d.chip:10} {d.mac}" if d else f"{p:32} ?          {error}")
    return 0


# Modules the `serve` extra brings; a missing one means the extra is not installed.
_SERVE_MODULES = frozenset({"fastapi", "uvicorn", "pydantic", "starlette"})


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from . import server
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] not in _SERVE_MODULES:
            raise
        ui.warn("serve needs its web dependencies: install `rustyfarian-bench[serve]`")
        return 2
    server.serve(args.host, args.port, args.outbox, args.job_timeout, args.detect_timeout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rustyfarian-bench", description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="consumer repo with bench/bench.toml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a guided check")
    run.add_argument("target", help="example name, board/check, or board")
    run.add_argument("check", nargs="?", help="check on the board, e.g. `s3 b3f`")
    run.add_argument("--port", help="serial port (default: auto-detect)")
    run.add_argument(
        "--detect-timeout",
        type=_positive_seconds,
        metavar="SECONDS",
        help="give up on a missing board after this long (default: wait for it)",
    )
    run.add_argument(
        "--unique-board",
        action="store_true",
        help="refuse to guess between boards of the same chip; pin one with `mac`",
    )
    run.add_argument("--no-build", action="store_true", help="skip the build step")
    run.add_argument(
        "-y", "--yes", action="store_true", help="answer every prompt with its default"
    )
    run.add_argument(
        "--no-twins", action="store_true", help="run only the named example, not its twins"
    )
    modes = run.add_mutually_exclusive_group()
    modes.add_argument(
        "--unattended",
        action="store_true",
        help="run only steps that need no person; implies --yes",
    )
    modes.add_argument("--attended", action="store_true", help="run only steps that need a person")
    run.set_defaults(func=_cmd_run)

    submit = sub.add_parser("submit", help="build here, run the check on a remote bench host")
    submit.add_argument("board", help="board key, e.g. `s3`")
    submit.add_argument("example", help="example name")
    submit.add_argument("--bench", required=True, metavar="URL", help="the bench host's base URL")
    submit.add_argument("--check", help="check on the board, when several list the example")
    submit.add_argument("--image", type=Path, help="flash image to upload instead of building one")
    submit.add_argument("--no-build", action="store_true", help="skip the build and image steps")
    submit.add_argument("--wait", action="store_true", help="wait for the verdict and exit with it")
    submit.set_defaults(func=client.cmd_submit)

    serve = sub.add_parser("serve", help="serve the bench to `submit` (needs the serve extra)")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on")
    serve.add_argument("--port", type=int, default=8000, help="port to listen on")
    serve.add_argument("--outbox", type=Path, default=Path("outbox"), help="where jobs are kept")
    serve.add_argument(
        "--job-timeout", type=float, default=600.0, help="seconds before a job is ended"
    )
    serve.add_argument(
        "--detect-timeout",
        type=_positive_seconds,
        default=remote.DETECT_TIMEOUT,
        metavar="SECONDS",
        help="seconds a job waits for its board before it fails",
    )
    serve.set_defaults(func=_cmd_serve)

    sub.add_parser("list", help="list scenarios").set_defaults(func=_cmd_list)
    sub.add_parser("detect", help="probe connected boards").set_defaults(func=_cmd_detect)

    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    try:
        return int(args.func(args))
    except config.ConfigError as e:
        ui.warn(str(e))
        return 2
    except device.MissingTool as e:
        if args.command != "submit":
            raise
        ui.warn(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
