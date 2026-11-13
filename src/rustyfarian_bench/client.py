"""`rustyfarian-bench submit`: build here, upload an image and the scenario, optionally wait.

Stdlib only (urllib), so the base install stays pyserial-only.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config, remote, runner, ui
from .config import ConfigError, Scenario

POLL_INTERVAL = 1.0
POST_TIMEOUT = 120.0  # an upload of a whole image
GET_TIMEOUT = 30.0


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


def _request(url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RemoteError(f"{url}: {e.code} {_detail(e)}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        # unreachable, timeout, bad JSON, a reply cut short
        raise RemoteError(f"{url}: {getattr(e, 'reason', e)}") from None
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


def _submit(args: argparse.Namespace) -> int:
    base = args.bench.rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ConfigError(f"--bench {args.bench!r}: expected an http:// or https:// URL")
    cfg = config.load(args.repo)
    scenario = _select(cfg, args.board, args.example, args.check)
    if reasons := remote.remote_violations(scenario):
        ui.warn(f"{scenario.board.key}/{scenario.name} cannot run remotely: " + "; ".join(reasons))
        return 2
    if (mac := scenario.board.mac) and not remote.MAC.fullmatch(mac):
        raise ConfigError(
            f"board {scenario.board.name}: mac {mac!r} is not of the form aa:bb:cc:dd:ee:ff"
        )
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
        "baud": board.baud,
        "reset": board.reset,
    }
    if board.mac:
        board_meta["mac"] = board.mac
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
