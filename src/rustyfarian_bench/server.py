"""`rustyfarian-bench serve`: the HTTP face of the remote bench (needs the `serve` extra).

Handlers are plain `def`s, so FastAPI runs them in its thread pool and file work never blocks
the event loop. Jobs run one at a time on the `remote.Worker` thread, so this must stay a
single process: the queue lives in memory.
"""

import base64
import binascii
import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import remote
from .config import ConfigError

# Decoded image: the largest flash is 16 MiB on these boards; the scenario is a small TOML file.
MAX_IMAGE = 16 * 1024 * 1024
MAX_SCENARIO = 256 * 1024


class BoardMeta(BaseModel):
    key: str
    chip: str
    name: str
    baud: int = Field(gt=0, le=remote.MAX_BAUD)
    reset: Literal["rts", "dtr", "none"]
    mac: str | None = None  # checked by `remote._check_board`, like the other unsafe fields


class SubmitRequest(BaseModel):
    board: BoardMeta
    check: str
    example: str
    scenario: str
    image: str  # base64


def _body_budget() -> int:
    # base64 image plus a scenario that JSON may escape up to six-fold (`\\u00XX`), plus the rest.
    return MAX_IMAGE * 4 // 3 + MAX_SCENARIO * 6 + 4096


class _TooLarge(BaseException):  # not Exception: FastAPI would turn that into a 400
    pass


class _BodyLimit:
    """Refuse an oversized request body before the framework reads it all into memory."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _body_budget()
        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit() and int(value) > limit:
                await self._refuse(scope, receive, send)
                return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))  # chunked bodies carry no Content-Length
            if seen > limit:
                raise _TooLarge
            return message

        try:
            await self.app(scope, counted, send)
        except _TooLarge:
            await self._refuse(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        reply = JSONResponse({"detail": "request body too large"}, status_code=413)
        await reply(scope, receive, send)


class _Bearer:
    """Let a request through only with a token from the file; the token's name goes in the scope.

    The file is read on every request, so a token is added or revoked without a restart. It is
    the outermost middleware: a stranger gets a 401 before anything of the body is read.
    """

    def __init__(self, app: ASGIApp, token_file: Path):
        self.app = app
        self.token_file = token_file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            tokens = remote.load_tokens(self.token_file)
        except ConfigError:  # the reply must not reveal where the file lives
            reply = JSONResponse({"detail": "the token file cannot be read"}, status_code=503)
            await reply(scope, receive, send)
            return
        name = _submitter(scope, tokens)
        if name is None:
            reply = JSONResponse(
                {"detail": "a bearer token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await reply(scope, receive, send)
            return
        scope["submitter"] = name
        await self.app(scope, receive, send)


def _submitter(scope: Scope, tokens: dict[str, str]) -> str | None:
    """The name of the token in the request's `Authorization: Bearer` header, or None."""
    header = next((v for n, v in scope["headers"] if n == b"authorization"), b"")
    scheme, _, token = header.decode("latin-1").partition(" ")
    if scheme.lower() != "bearer":
        return None
    sent = token.strip().encode("utf-8")
    for known, name in tokens.items():
        if hmac.compare_digest(known.encode("utf-8"), sent):
            return name
    return None


def _decode_image(text: str) -> bytes:
    if len(text) > MAX_IMAGE * 4 // 3 + 4:  # refuse before decoding a huge string
        raise HTTPException(413, f"image: more than {MAX_IMAGE} bytes")
    try:
        image = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "image: not valid base64") from None
    if len(image) > MAX_IMAGE:
        raise HTTPException(413, f"image: more than {MAX_IMAGE} bytes")
    return image


def _read_log(job_dir: Path, offset: int, done: bool) -> tuple[str, int]:
    """The log from byte `offset`; until the job is done only up to the last line break."""
    try:
        with remote.contained(job_dir, "run.log").open("rb") as f:
            f.seek(offset)
            data = f.read()
    except (OSError, ConfigError):
        return "", offset
    if not done:
        # A line still being written (or a progress bar's \r frame) is sent once it is whole.
        cut = max(data.rfind(b"\n"), data.rfind(b"\r"))
        data = data[: cut + 1]
    return data.decode("utf-8", errors="replace"), offset + len(data)


def create_app(
    outbox: Path,
    job_timeout: float = 600.0,
    runner: Callable[[Path], None] | None = None,
    detect_timeout: float | None = remote.DETECT_TIMEOUT,
    token_file: Path | None = None,
) -> FastAPI:
    """The app; `runner` replaces the child-process job runner (for tests).

    With `token_file`, every request needs a bearer token from that file; without, the app is
    open, which is only for loopback.
    """
    outbox = outbox.resolve()
    worker = remote.Worker(runner, timeout=job_timeout, detect_timeout=detect_timeout)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        outbox.mkdir(parents=True, exist_ok=True)
        remote.recover(outbox)
        worker.start()
        try:
            yield
        finally:
            worker.stop()  # ends a running child, so no flashing goes on after shutdown

    app = FastAPI(title="rustyfarian-bench remote bench", lifespan=lifespan)
    app.add_middleware(_BodyLimit)
    if token_file is not None:
        app.add_middleware(_Bearer, token_file=token_file)  # added last, so outermost

    @app.post("/jobs", status_code=201)
    def post_job(req: SubmitRequest, request: Request) -> dict[str, Any]:
        if len(req.scenario.encode("utf-8", errors="replace")) > MAX_SCENARIO:
            raise HTTPException(413, f"scenario: more than {MAX_SCENARIO} bytes")
        image = _decode_image(req.image)
        meta = {"board": req.board.model_dump(), "check": req.check, "example": req.example}
        submitter = str(request.scope.get("submitter", ""))
        try:
            job = remote.create_job(outbox, meta, req.scenario, image, submitter)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        worker.submit(remote.job_path(outbox, job.id))
        return asdict(job)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, offset: int = Query(0, ge=0)) -> dict[str, Any]:
        try:
            job_dir = remote.job_path(outbox, job_id)
        except ConfigError:
            raise HTTPException(404, "no such job") from None
        # job.json first: a job seen as done has a final log, so the chunk below is complete.
        job = remote.read_job(outbox, job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        log, end = _read_log(job_dir, offset, job.status == remote.DONE)
        return {"job": asdict(job), "offset": end, "log": log}

    return app


def serve(
    host: str,
    port: int,
    outbox: Path,
    job_timeout: float,
    detect_timeout: float | None = remote.DETECT_TIMEOUT,
    token_file: Path | None = None,
    ssl_certfile: Path | None = None,
    ssl_keyfile: Path | None = None,
) -> None:
    """Run the server in this process until it is stopped.

    Off loopback the bench must be closed: `token_file` and both TLS files are required there,
    and a ConfigError says which is missing. The token file must load before anything listens.
    """
    if token_file is not None:
        remote.load_tokens(token_file)
    if (ssl_certfile is None) != (ssl_keyfile is None):
        missing = "--ssl-keyfile" if ssl_keyfile is None else "--ssl-certfile"
        raise ConfigError(f"TLS needs both files: {missing} is missing")
    if not remote.loopback(host):
        if token_file is None:
            raise ConfigError(f"serving on {host} needs --token-file: the bench must not be open")
        if ssl_certfile is None:
            raise ConfigError(
                f"serving on {host} needs TLS (--ssl-certfile and --ssl-keyfile): "
                "a token must not travel in the clear"
            )
    for flag, path in (("--ssl-certfile", ssl_certfile), ("--ssl-keyfile", ssl_keyfile)):
        if path is not None and not path.is_file():
            raise ConfigError(f"{flag} {path}: no such file")
    app = create_app(outbox, job_timeout, detect_timeout=detect_timeout, token_file=token_file)
    uvicorn.run(app, host=host, port=port, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
