"""`rustyfarian-bench` command line."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__, client, config, device, remote, ui
from .runner import Options, Session


def _positive_seconds(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {text!r}")
    return seconds


def _whole_seconds(text: str) -> int:
    try:
        seconds = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if seconds < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {text!r}")
    return seconds


def _positive_int(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {text!r}")
    return number


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
    server.serve(
        args.host,
        args.port,
        args.outbox,
        args.job_timeout,
        args.detect_timeout,
        token_file=args.token_file,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        parts_file=args.parts,
        **({} if args.status_file is None else {"status_file": args.status_file}),
    )
    return 0


# Modules the `kiosk` extra brings.
_KIOSK_MODULES = frozenset({"PIL"})


def _cmd_kiosk(args: argparse.Namespace) -> int:
    try:
        from . import kiosk
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] not in _KIOSK_MODULES:
            raise
        ui.warn("kiosk needs Pillow: install `rustyfarian-bench[kiosk]`")
        return 2
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    dev_dir = Path(os.environ.get("BENCH_DEV_DIR", "/dev"))
    touch = None
    if args.sleep_after > 0 and not args.once:
        # stdlib only, but nothing else needs it: sleep off (or `--once`) builds no reader
        from .kiosk_touch import TouchReader

        touch = TouchReader(
            Path(os.environ.get("BENCH_SYSFS_INPUT", "/sys/class/input")),
            dev_dir,
            os.environ.get("BENCH_TOUCH_MATCH", "ADS7846"),
        )
    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        return kiosk.run(
            args.status_file,
            title=args.title,
            address=args.address,
            version=__version__,
            fb_match=args.fb_match,
            sysfs=Path(os.environ.get("BENCH_SYSFS_GRAPHICS", "/sys/class/graphics")),
            dev_dir=dev_dir,
            png_dir=args.png_dir,
            once=args.once,
            wait=stop.wait,
            sleep_after=args.sleep_after,
            touch=touch,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)


def _add_bench(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--bench",
        default=os.environ.get(client.URL_ENV) or None,  # "" is unset, as the devcontainer sets
        metavar="URL",
        help=f"the bench host's base URL (default: ${client.URL_ENV})",
    )


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
    _add_bench(submit)
    submit.add_argument("--check", help="check on the board, when several list the example")
    submit.add_argument("--image", type=Path, help="flash image to upload instead of building one")
    submit.add_argument("--no-build", action="store_true", help="skip the build and image steps")
    submit.add_argument("--wait", action="store_true", help="wait for the verdict and exit with it")
    submit.set_defaults(func=client.cmd_submit)

    boards = sub.add_parser("boards", help="list the boards on a remote bench host")
    _add_bench(boards)
    boards.add_argument(
        "--probe",
        action="store_true",
        help="read each board's MAC and chip with espflash; resets the board",
    )
    boards.add_argument(
        "--port",
        action="append",
        metavar="DEVICE",
        help="serial port on the bench host to probe, as listed (repeatable; needs --probe)",
    )
    boards.add_argument("--json", action="store_true", help="print the host's reply as JSON")
    boards.set_defaults(func=client.cmd_boards)

    flash = sub.add_parser(
        "flash", help="flash an image onto one remote bench board; passes on its boot banner"
    )
    _add_bench(flash)
    target = flash.add_mutually_exclusive_group(required=True)
    target.add_argument("--port", help="a port from `boards`")
    target.add_argument(
        "--part", metavar="NAME", help="a verified board's parts.toml name, from `boards`"
    )
    flash.add_argument("image", type=Path, help="merged image (espflash save-image --merge)")
    flash.add_argument(
        "--baud", type=_positive_int, help=f"console baud rate (default {remote.FLASH_BAUD})"
    )
    flash.add_argument(
        "--reset",
        choices=config.RESET_MODES,
        help=f"how to reset the board (default {remote.FLASH_RESET})",
    )
    flash.add_argument(
        "--expect",
        metavar="REGEX",
        help="boot line to wait for instead of the first app line",
    )
    flash.add_argument(
        "--probe", action="store_true", help="probe unverified boards first; resets them"
    )
    flash.add_argument("--wait", action="store_true", help="wait for the verdict and exit with it")
    flash.set_defaults(func=client.cmd_flash)

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
    serve.add_argument(
        "--token-file",
        type=Path,
        metavar="PATH",
        help='TOML of `name = "token"` lines; required off loopback',
    )
    serve.add_argument(
        "--ssl-certfile", type=Path, metavar="PATH", help="TLS certificate; required off loopback"
    )
    serve.add_argument("--ssl-keyfile", type=Path, metavar="PATH", help="its private key")
    serve.add_argument(
        "--status-file",
        type=Path,
        metavar="PATH",
        help="keep this world-readable status file for the kiosk panel (off by default)",
    )
    serve.add_argument(
        "--parts",
        type=Path,
        metavar="PATH",
        help="parts.toml naming boards by base MAC in `GET /boards` (off by default)",
    )
    serve.set_defaults(func=_cmd_serve)

    kiosk = sub.add_parser(
        "kiosk", help="draw the bench status on the SPI panel (needs the kiosk extra)"
    )
    kiosk.add_argument(
        "--status-file",
        type=Path,
        # the same path as in the serve and display unit templates under deploy/ansible/roles
        default=Path("/run/rustyfarian-bench/status.json"),
        metavar="PATH",
        help="the file `serve --status-file` keeps (default: %(default)s)",
    )
    kiosk.add_argument(
        "--title",
        default=os.environ.get("BENCH_TITLE", "rustyfarian bench"),
        help="banner title (default: $BENCH_TITLE or %(default)s)",
    )
    kiosk.add_argument(
        "--address",
        default=os.environ.get("BENCH_ADDRESS", ""),
        help="where the bench is reached, shown when idle (default: $BENCH_ADDRESS)",
    )
    kiosk.add_argument(
        "--fb-match",
        default=os.environ.get("BENCH_FB_MATCH", "ili9486"),
        help="part of the panel's framebuffer name (default: $BENCH_FB_MATCH or %(default)s)",
    )
    kiosk.add_argument(
        "--sleep-after",
        type=_whole_seconds,
        default=os.environ.get("BENCH_SLEEP_AFTER", "900"),
        metavar="SECONDS",
        help="go dark after this many idle seconds, a touch wakes the panel; 0 turns sleep off"
        " (default: $BENCH_SLEEP_AFTER or %(default)s)",
    )
    kiosk.add_argument("--once", action="store_true", help="draw once and exit")
    kiosk.add_argument("--png-dir", type=Path, help="also save each screen drawn there as a PNG")
    kiosk.set_defaults(func=_cmd_kiosk)

    sub.add_parser("list", help="list scenarios").set_defaults(func=_cmd_list)
    sub.add_parser("detect", help="probe connected boards").set_defaults(func=_cmd_detect)

    remote_parsers = {"submit": submit, "boards": boards, "flash": flash}
    args = parser.parse_args(argv)
    if args.command in remote_parsers and args.bench is None:
        remote_parsers[args.command].error(f"--bench URL is required, or set ${client.URL_ENV}")
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
