# ADR-001: Remote bench runs a job as a child process in a server-written repo

## Status
Accepted

## Context
A devcontainer sees no USB, so bench checks must run on a shared host that owns the boards.
The host cannot run the consumer's `just` recipes, and it takes uploads from anyone on the network.
The runner already builds, detects, flashes, resets, reads serial and judges steps, and local `run` must not change.

## Decision
- `serve` writes each job into `outbox/<id>/` as a tiny consumer repo: its own `bench/bench.toml` plus the uploaded scenario.
- The unchanged `run <example> --unattended --no-build --no-twins --detect-timeout N --unique-board` then runs as a child process there, with its output in `run.log`.
- The server owns the flash command, `espflash write-bin` of the uploaded image at `0x0`, so no client command runs on the host.
- The image is a merged flash image built by the consumer's optional `image` command, because a bare ELF gets espflash's bundled bootloader and may not boot.
- The server refuses `setup`, `teardown`, `probe` and `run` on a step an unattended run reaches, `detect = false` boards and scenarios with no unattended step; this is the security boundary, and the client check is a convenience.
- Names and chips that reach a path or an argv are allowlisted, and a refused upload leaves no directory.
- `POST /jobs` takes a JSON body with a base64 image, and `GET /jobs/{id}?offset=N` returns the log from a byte offset, so `submit --wait` polls.
- One FIFO worker serves the whole host, so a probe never touches a board mid-flash.
- A job that outlives `--job-timeout` has its process group ended, and `serve` stopping ends the running child's group.
- A job whose board is not attached fails after `--detect-timeout` (default 30 s), which `serve` passes to the child as `run --detect-timeout`, so a missing board cannot hold the worker.
- The board's optional `mac` travels with the upload, is validated as `aa:bb:cc:dd:ee:ff` and pinned in the job's `bench.toml`.
- `--unique-board` makes detection probe every port and refuse a second same-chip match, because a shared bench must not flash whichever board answers first; local `run` keeps first-match unless asked.
- With `--unique-board` an unpinned job waits while a port is busy or silent, since that port could be a second match; the deadline error names it.
- A SIGKILL of `serve` cannot run that cleanup and may orphan a flashing child; a restart marks interrupted jobs as `error`.

## Consequences
- Local `run`, `list` and `detect` behave as before; the schema gains only the optional `image` command, `run --no-twins` and `run --unique-board`.
- Judging, serial reading and results stay in one code path, so a remote verdict is a local verdict.
- Remote runs are unattended only, so a pass is never a full pass.
- A job costs one Python process, which is fine at one job at a time.
- There is no authentication, and images, which may hold Wi-Fi credentials, stay in `outbox/` forever.
- The base wheel stays `pyserial`-only; FastAPI, pydantic and uvicorn come with the `serve` extra.

## Alternatives Considered
|                                  Alternative |                   Pros | Cons                                                               | Why Rejected                                                |
|---------------------------------------------:|-----------------------:|:-------------------------------------------------------------------|:------------------------------------------------------------|
|       Call runner code in the server process |       No child process | A hung job blocks the server; a crash takes it down                | Unreliable under a shared worker                            |
| Run the client's `flash` command on the host |     Matches local runs | Consumer recipes and toolchains are absent on the Pi               | Host cannot run them, and it would run client commands      |
|                            Upload a bare ELF |     No `image` command | espflash's bundled bootloader gave an "efuse blk rev" boot failure | Boards did not boot                                         |
|           Multipart upload and log streaming |     No base64 overhead | Heavier client and server code                                     | JSON plus polling is simpler and survives re-enumeration    |
|                         One worker per board | Boards run in parallel | A board-info probe can hit a board mid-flash                       | Host-wide single worker is safe                             |
|                         stdlib `http.server` |      No new dependency | Hand-written validation, size caps and routing                     | FastAPI behind an optional extra keeps the base wheel light |
