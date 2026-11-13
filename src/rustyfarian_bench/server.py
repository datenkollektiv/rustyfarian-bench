"""`rustyfarian-bench serve`: the HTTP face of the remote bench (needs the `serve` extra).

Handlers are plain `def`s, so FastAPI runs them in its thread pool and file work never blocks
the event loop. Jobs run one at a time on the `remote.Worker` thread, so this must stay a
single process: the queue lives in memory.
"""

import base64
import binascii
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query
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


def _read_log(log: Path, offset: int, done: bool) -> tuple[str, int]:
    """The log from byte `offset`; until the job is done only up to the last line break."""
    try:
        with log.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
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
) -> FastAPI:
    """The app; `runner` replaces the child-process job runner (for tests)."""
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

    @app.post("/jobs", status_code=201)
    def post_job(req: SubmitRequest) -> dict[str, Any]:
        if len(req.scenario.encode("utf-8", errors="replace")) > MAX_SCENARIO:
            raise HTTPException(413, f"scenario: more than {MAX_SCENARIO} bytes")
        image = _decode_image(req.image)
        meta = {"board": req.board.model_dump(), "check": req.check, "example": req.example}
        try:
            job = remote.create_job(outbox, meta, req.scenario, image)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        worker.submit(outbox / job.id)
        return asdict(job)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, offset: int = Query(0, ge=0)) -> dict[str, Any]:
        # job.json first: a job seen as done has a final log, so the chunk below is complete.
        job = remote.read_job(outbox, job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        log, end = _read_log(outbox / job_id / "run.log", offset, job.status == remote.DONE)
        return {"job": asdict(job), "offset": end, "log": log}

    return app


def serve(
    host: str,
    port: int,
    outbox: Path,
    job_timeout: float,
    detect_timeout: float | None = remote.DETECT_TIMEOUT,
) -> None:
    """Run the server in this process until it is stopped."""
    uvicorn.run(
        create_app(outbox, job_timeout, detect_timeout=detect_timeout), host=host, port=port
    )
