"""`rustyfarian-bench submit`: build here, upload an image and the scenario, optionally wait.

Stdlib only (urllib), so the base install stays pyserial-only.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config, remote, runner, ui
from .config import ConfigError, Scenario

POLL_INTERVAL = 1.0
POST_TIMEOUT = 120.0  # an upload of a whole image
GET_TIMEOUT = 30.0
# The environment carries what a devcontainer gets through `remoteEnv`; "" counts as unset.
URL_ENV = "RUSTYFARIAN_BENCH_URL"  # the bench's base URL, the default for --bench
TOKEN_ENV = "RUSTYFARIAN_BENCH_TOKEN"  # the bearer token
CA_ENV = "RUSTYFARIAN_BENCH_CA"  # a PEM file with the CA that signed the bench's certificate


class RemoteError(RuntimeError):
    """The bench host could not be reached or refused the request."""


class BuildError(Exception):
    """The build or the image command failed; submit exits 1, as a failed run does."""


def _select(cfg: config.BenchConfig, board: str, example: str, check: str | None) -> Scenario:
    found = [
        s
        for s in cfg.scenarios
        if s.board.key == board and example in s.examples and check in (None, s.name)
    ]
    where = f"example {example!r} on board {board!r}" + (f", check {check!r}" if check else "")
    if not found:
        raise ConfigError(f"no bench scenario for {where}")
    if len(found) > 1:
        names = ", ".join(s.name for s in found)
        raise ConfigError(f"several checks list {where}: {names}; pick one with --check")
    return found[0]


def _build_image(
    cfg: config.BenchConfig, board: config.Board, example: str, repo: Path, build: bool
) -> bytes:
    """Build, then run the `image` command to a temp path; `image` must be configured."""
    if build and not runner._run(cfg.build_for(board), repo, example=example):
        raise BuildError("build failed")
    with tempfile.TemporaryDirectory(prefix="rustyfarian-bench-") as tmp:
        path = Path(tmp) / "image.bin"
        if not runner._run(cfg.image_for(board), repo, example=example, image=str(path)):
            raise BuildError("the image command failed")
        try:
            return path.read_bytes()
        except OSError:
            raise BuildError(f"the image command wrote no file at {path}") from None


def _ssl_context() -> ssl.SSLContext:
    """Verifying TLS, trusting the system CAs or, with `CA_ENV` set, that PEM file alone."""
    cafile = os.environ.get(CA_ENV) or None
    try:
        return ssl.create_default_context(cafile=cafile)
    except OSError as e:  # ssl.SSLError included
        raise ConfigError(f"${CA_ENV}={cafile}: {e.strerror or e}") from None


def _token() -> str | None:
    """The bearer token from `TOKEN_ENV`, `""` counting as unset; the error never shows it."""
    token = os.environ.get(TOKEN_ENV) or None
    if token is not None and not (token.isascii() and token.isprintable()):
        # http.client would reject a CR or LF quoting the whole header, token and all
        raise ConfigError(
            f"${TOKEN_ENV}: expected printable ASCII; look for a stray newline or carriage return"
        )
    return token


def _opener(url: str, context: ssl.SSLContext) -> urllib.request.OpenerDirector:
    """Verifying TLS with `context`; a loopback URL skips any proxy, which would see the token."""
    host = urlsplit(url).hostname or ""
    direct: list[urllib.request.BaseHandler] = (
        [urllib.request.ProxyHandler({})] if remote.loopback(host) else []
    )
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), *direct)


def _request(url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    if token := _token():
        # unredirected: urllib would copy a plain header onto a redirect to any host or scheme
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with _opener(url, _ssl_context()).open(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        hint = f"; set ${TOKEN_ENV} to a token the bench knows" if e.code == 401 else ""
        raise RemoteError(f"{url}: {e.code} {_detail(e)}{hint}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        # unreachable, timeout, bad JSON, a reply cut short
        reason = getattr(e, "reason", e)
        hint = ""
        if isinstance(reason, ssl.SSLCertVerificationError):
            hint = f"; point ${CA_ENV} at the CA that signed the bench's certificate"
        raise RemoteError(f"{url}: {reason}{hint}") from None
    if not isinstance(reply, dict):
        raise RemoteError(f"{url}: unexpected reply")
    return reply


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        detail = json.load(e).get("detail", "")
    except (OSError, ValueError, AttributeError):
        return e.reason
    return detail if isinstance(detail, str) else json.dumps(detail)


def _wait(base: str, job_id: str) -> int:
    offset = 0
    while True:
        reply = _request(f"{base}/jobs/{job_id}?offset={offset}", None, GET_TIMEOUT)
        sys.stdout.write(reply["log"])
        sys.stdout.flush()
        offset = reply["offset"]
        job = reply["job"]
        if job["status"] == remote.DONE:
            break
        time.sleep(POLL_INTERVAL)
    verdict = str(job["verdict"])
    print()
    ui.info(f"{verdict}: {job['detail']}" if job["detail"] else verdict)
    return remote.EXIT.get(verdict, 1)


def cmd_submit(args: argparse.Namespace) -> int:
    try:
        return _submit(args)
    except BuildError as e:
        ui.warn(str(e))
        return 1
    except RemoteError as e:
        ui.warn(str(e))
        return 2
    except KeyboardInterrupt:
        print()
        ui.warn("aborted")
        return 130


def _base_url(raw: str) -> str:
    """`--bench` without a trailing slash; refuses a non-URL and a token over plain http."""
    base = raw.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ConfigError(f"--bench {raw!r}: expected an http:// or https:// URL")
    host = urlsplit(base).hostname or ""
    if _token() and base.startswith("http://") and not remote.loopback(host):
        raise ConfigError(
            f"--bench {base}: a token travels only over https:// (or to loopback through a "
            f"tunnel); unset ${TOKEN_ENV} for an open bench"
        )
    return base


def _submit(args: argparse.Namespace) -> int:
    base = _base_url(args.bench)
    cfg = config.load(args.repo)
    scenario = _select(cfg, args.board, args.example, args.check)
    if reasons := remote.remote_violations(scenario):
        ui.warn(f"{scenario.board.key}/{scenario.name} cannot run remotely: " + "; ".join(reasons))
        return 2
    if args.image:
        try:
            image = args.image.read_bytes()
        except OSError as e:
            raise ConfigError(f"--image {args.image}: {e.strerror or e}") from None
    elif cfg.image_for(scenario.board):
        image = _build_image(cfg, scenario.board, args.example, args.repo, not args.no_build)
    else:
        raise ConfigError(
            f"board {scenario.board.key} has no image: pass --image PATH or define an "
            "`image` command in bench.toml"
        )
    board = scenario.board
    board_meta: dict[str, Any] = {
        "key": board.key,
        "chip": board.chip,
        "name": board.name,
        "baud": board.committed_baud,
        "reset": board.reset,
    }
    if board.committed_mac:
        board_meta["mac"] = board.committed_mac
    body = {
        "board": board_meta,
        "check": scenario.name,
        "example": args.example,
        "scenario": scenario.path.read_text(encoding="utf-8"),
        "image": base64.b64encode(image).decode("ascii"),
    }
    job = _request(f"{base}/jobs", body, POST_TIMEOUT)
    job_id = str(job["id"])
    ui.info(f"submitted job {job_id}")
    if not args.wait:
        return 0
    try:
        return _wait(base, job_id)
    except KeyboardInterrupt:
        print()
        ui.warn(f"stopped waiting; job {job_id} keeps running on the bench host")
        return 130


_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe(value: object) -> str:
    """Text for a terminal: control characters from the host's reply become `?`."""
    return _UNSAFE.sub("?", str(value))


def cmd_boards(args: argparse.Namespace) -> int:
    try:
        return _boards(args)
    except RemoteError as e:
        ui.warn(_safe(e))
        return 2
    except KeyboardInterrupt:
        print()
        ui.warn("aborted; a probe already queued keeps running on the bench host")
        return 130


def _boards(args: argparse.Namespace) -> int:
    base = _base_url(args.bench)
    if args.port and not args.probe:
        raise ConfigError("--port names a port to probe: add --probe")
    probe: dict[str, Any] | None = None
    if args.probe:
        if args.json:
            print("probing resets the boards", file=sys.stderr)
        else:
            ui.warn(
                "probing resets each board: a running app restarts, "
                "and the probe waits behind any running job"
            )
        probe = _probe(base, args.port or None)
    reply = _request(f"{base}/boards", None, GET_TIMEOUT)
    if not isinstance(reply.get("boards"), list):
        raise RemoteError(f"{base}/boards: unexpected reply")
    failed = probe is not None and any(r["error"] for r in probe["results"])
    if args.json:
        print(json.dumps({**reply, "probe": probe} if probe else reply, indent=2))
    else:
        for line in _render(reply):
            print(line)
        if probe:
            for r in probe["results"]:
                if r["error"]:
                    ui.warn(f"{_safe(r['port'])}: {_safe(r['error'])}")
        elif any(not b.get("verified") for b in reply["boards"]):
            ui.info("unverified: run `boards --probe` to read the MAC (it resets the board)")
    return 1 if failed else 0


def _probe(base: str, ports: list[str] | None) -> dict[str, Any]:
    """Queue a probe, poll it until every port is done; returns the final probe reply."""
    body: dict[str, Any] = {"ports": ports} if ports else {}
    queued = _request(f"{base}/boards/probe", body, POST_TIMEOUT)
    probe_id = queued.get("id")
    if not isinstance(probe_id, str):
        raise RemoteError(f"{base}/boards/probe: unexpected reply")
    url = f"{base}/boards/probe/{probe_id}"
    while True:
        reply = _request(url, None, GET_TIMEOUT)
        results = reply.get("results")
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise RemoteError(f"{url}: unexpected reply")
        if reply.get("state") == "done":
            for r in results:
                r.setdefault("error", None)
                r.setdefault("port", "?")
            return reply
        time.sleep(POLL_INTERVAL)


def _render(reply: dict[str, Any]) -> list[str]:
    """The board table; an empty cell is `-`, an unverified board shows no MAC, chip or name."""
    rows: list[tuple[str, ...]] = [("PORT", "USB", "SERIAL", "CHIP", "MAC", "NAME", "SEEN")]
    for b in reply["boards"]:
        ok = bool(b.get("verified"))
        seen = "unverified"
        if ok:
            seen = _safe(b.get("mac_source") or "-")
            if b.get("verified_at"):
                seen += f" {_safe(b['verified_at'])}"
        cells = [
            b.get("port"),
            b.get("vid_pid"),
            b.get("usb_serial"),
            b.get("chip") if ok else None,
            b.get("mac") if ok else None,
            b.get("name") if ok else None,
        ]
        rows.append((*(_safe(c) if c else "-" for c in cells), seen))
    if len(rows) == 1:
        return ["no serial ports attached to the bench host"]
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    return [
        "  ".join(c.ljust(w) for c, w in zip(r[:6], widths, strict=True)) + "  " + r[6]
        for r in rows
    ]
