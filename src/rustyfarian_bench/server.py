"""`rustyfarian-bench serve`: the HTTP face of the remote bench (needs the `serve` extra).

Handlers are plain `def`s, so FastAPI runs them in its thread pool and file work never blocks
the event loop. Jobs run one at a time on the `remote.Worker` thread, so this must stay a
single process: the queue lives in memory.
"""

import base64
import binascii
import hmac
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__, boards, remote
from .config import ConfigError
from .status import StatusWriter

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


class FlashRequest(BaseModel):
    port: str | None = Field(default=None, max_length=200)
    part: str | None = Field(default=None, max_length=remote.NAME_MAX)
    image: str  # base64
    baud: int | None = Field(default=None, gt=0, le=remote.MAX_BAUD)
    reset: Literal["rts", "dtr", "none"] | None = None
    expect: str | None = Field(default=None, max_length=remote.EXPECT_MAX)


def _body_budget() -> int:
    # base64 image plus a scenario that JSON may escape up to six-fold (`\\u00XX`), plus the rest.
    return MAX_IMAGE * 4 // 3 + MAX_SCENARIO * 6 + 4096


class ProbeRequest(BaseModel):
    ports: list[str] | None = Field(default=None, max_length=64)  # None: every listed port


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
    # compare against every token, so the time does not reveal which one matched
    matches = [
        name for known, name in tokens.items() if hmac.compare_digest(known.encode("utf-8"), sent)
    ]
    return matches[0] if matches else None


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
    status: StatusWriter | None = None,
    parts_file: Path | None = None,
    lister: Callable[[], list[boards.PortInfo]] | None = None,
    probe_argv: Sequence[str] | None = None,
) -> FastAPI:
    """The app; `runner` replaces the child-process job runner (for tests).

    With `token_file`, every request needs a bearer token from that file; without, the app is
    open, which is only for loopback. With `status`, the kiosk status file follows the worker.
    With `parts_file`, `GET /boards` names the boards it lists. `lister` and `probe_argv` replace
    the port listing and the probe child's command (for tests).
    """
    outbox = outbox.resolve()
    worker = remote.Worker(
        runner,
        timeout=job_timeout,
        detect_timeout=detect_timeout,
        observer=None if status is None else status.observe,
    )

    registry = boards.BoardRegistry(lister, parts_file, probe_argv=probe_argv)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        outbox.mkdir(parents=True, exist_ok=True)
        remote.recover(outbox)
        if status is not None:
            status.start()
        worker.start()
        try:
            yield
        finally:
            worker.stop()  # ends a running child, so no flashing goes on after shutdown
            registry.fail_pending("server stopped")
            if status is not None:
                status.stop()

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
        if status is not None:
            status.job_queued(job.id)  # before submit, so the worker cannot finish it first
        worker.submit(remote.job_path(outbox, job.id))
        return asdict(job)

    @app.post("/flash", status_code=201)
    def post_flash(req: FlashRequest, request: Request) -> dict[str, Any]:
        if (req.port is None) == (req.part is None):
            raise HTTPException(422, "give exactly one of port and part")
        image = _decode_image(req.image)
        try:
            target = registry.resolve_target(req.port, req.part)
        except boards.UnknownPort as e:
            raise HTTPException(422, str(e)) from None
        except ConfigError:  # the reply must not reveal where the file lives
            raise HTTPException(503, "the parts file cannot be read") from None
        submitter = str(request.scope.get("submitter", ""))
        try:
            job = remote.create_flash_job(
                outbox,
                image,
                port=target.port,
                part=target.name,  # a verified port's part name too, for the kiosk and job
                mac=target.mac,
                baud=req.baud,
                reset=req.reset,
                expect=req.expect,
                submitter=submitter,
            )
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        if status is not None:
            status.job_queued(job.id)  # before submit, so the worker cannot finish it first
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

    @app.get("/boards")
    def get_boards() -> dict[str, Any]:
        try:
            return registry.snapshot()
        except ConfigError:  # the reply must not reveal where the file lives
            raise HTTPException(503, "the parts file cannot be read") from None

    @app.post("/boards/probe", status_code=202)
    def post_probe(req: ProbeRequest | None = None) -> dict[str, Any]:
        try:
            registry.parts()  # a parts file that broke fails the board routes alike
            probe = registry.request_probe(None if req is None else req.ports, worker.submit_call)
        except boards.UnknownPort as e:
            raise HTTPException(422, str(e)) from None
        except ConfigError:
            raise HTTPException(503, "the parts file cannot be read") from None
        return {
            "id": probe.id,
            "state": probe.state,
            "ports": [r.port for r in probe.results],
            "reset": probe.reset,
            "note": boards.NOTE,
        }

    @app.get("/boards/probe/{probe_id}")
    def get_probe(probe_id: str) -> dict[str, Any]:
        probe = registry.get_probe(probe_id)
        if probe is None:
            raise HTTPException(404, "no such probe")
        return probe

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
    status_file: Path | None = None,
    parts_file: Path | None = None,
) -> None:
    """Run the server in this process until it is stopped.

    Off loopback the bench must be closed: `token_file` and both TLS files are required there,
    and a ConfigError says which is missing. The token file and the parts file must load before
    anything listens; the parts file is read again on every board list.
    """
    if token_file is not None:
        remote.load_tokens(token_file)
    if parts_file is not None:
        boards.load_parts(parts_file)
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
    status = None if status_file is None else StatusWriter(status_file, outbox, version=__version__)
    app = create_app(
        outbox,
        job_timeout,
        detect_timeout=detect_timeout,
        token_file=token_file,
        status=status,
        parts_file=parts_file,
    )
    uvicorn.run(app, host=host, port=port, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
