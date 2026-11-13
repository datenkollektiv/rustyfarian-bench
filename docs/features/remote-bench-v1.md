---
gate: hardware
gate-note: rustyfarian-network wifi-connect scenario passes via `submit --wait` against a bench server
desk-work: available
---

# Feature: Remote Bench v1

A shared bench host (Raspberry Pi in the IoT network) runs uploaded firmware against uploaded scenarios, so development can happen in a devcontainer that sees no USB.
Walking skeleton: `submit` uploads and waits, `serve` flashes, judges and stores the result; queue UI, cancel and services come later.

## Decisions
|                                                      Decision | Reason                                                            | Rejected Alternative                         |
|--------------------------------------------------------------:|:------------------------------------------------------------------|:---------------------------------------------|
|                         Backend lives in this repo as `serve` | Reuses `steps.py`, `serial_lines.py`, `device.py`                 | Separate repo duplicating step judging       |
|                      FastAPI + uvicorn behind a `serve` extra | Validation and size caps for free; base wheel stays pyserial-only | stdlib `http.server`                         |
| Upload: image + scenario TOML + board metadata, one JSON body | Image is base64 in JSON; one request, no multipart parsing        | Multipart upload                             |
|                      Optional `image` command in `bench.toml` | Consumer owns bootloader and partition args, like `build`         | Bare ELF: espflash's bootloader may not boot |
|                 Client `submit <board> <example> --bench URL` | Builds locally, uploads, one metadata shape for every repo        | Hand-rolled `curl` per consumer              |
|               `submit --wait` polls `GET /jobs/{id}?offset=N` | Survives re-enumeration; no streaming server code                 | Streaming the log over one response          |
|          `POST /jobs` returns an id, `GET /jobs/{id}` reports | Reflash re-enumeration outlives one long request                  | Synchronous run inside one HTTP request      |
|                                     One host-wide FIFO worker | A parallel job could probe a board mid-flash                      | Per-board locks, parallel jobs               |
|                       Board found by automatic chip detection | `mac` pin is carried; an unpinned job refuses to guess            | Pi-side `bench.toml` pinning port and MAC    |
|   Job = server-written repo in `outbox/<id>/`, `run` as child | Local `run` unchanged; the dir is self-contained                  | Calling runner code in the server process    |
|                  v1 runs `--unattended --no-twins` steps only | Nobody stands at the shared bench                                 | Relaying `confirm` prompts over HTTP         |
|             v1 refuses `setup`, `teardown`, `probe` and `run` | Consumer `just` recipes are not on the Pi                         | Shipping consumer source to the Pi           |

## Constraints
- The local `run` command and the TOML schema stay unchanged for existing consumer repos.
- No authentication: anyone in the IoT network can flash a board (proof of concept only).
- The client needs only HTTP access to the bench host, no USB.
- The bench host needs `espflash` and `uv`; boards attach to it over USB.
- Only `espflash`-detectable ESP32 boards; AVR (`detect = false`) is out of v1.
- `outbox/` is kept forever in v1; no retention.
- Tests fake serial ports, `espflash` and HTTP; `just verify` covers the new modules.

## Open Questions
- [x] Which metadata fields beyond chip, board, example and mode does the job need? Board key, chip, name, baud and reset, plus an optional MAC pin; no USB fields.
- [x] Does the scenario upload carry the consumer `bench.toml` board entry? No, the server writes its own `bench.toml` from that metadata.

## State
- [x] Design approved
- [x] `serve` backend: job API, single worker, `outbox/` store
- [x] `submit` client: build, `image` command, upload, `--wait`
- [x] Tests passing
- [x] End-to-end on this laptop (`serve` in a shell or `screen`)
- [ ] rustyfarian-network wifi-connect via `submit` against the Pi
- [x] Documentation updated (README, ADR for the remote split, CHANGELOG)

## Session Log
- 2026-11-12 — Feature doc created via /feature dialog
- 2026-11-12 — Walking skeleton implemented; hardware end-to-end and the Pi gate still open
- 2026-11-13 — Hardware-free wire smoke on this laptop (merged C3 image via `--image`, no board): upload, worker, log polling and the 600 s job timeout work; a job for a missing board waits for a plug-in and holds the worker until the timeout (bug 001)
- 2026-11-14 — Laptop end-to-end passed: `submit --image` of a merged `hal_c3_connect_async` image (IDF v6.1 bootloader), connect check 3/3 on a C3-MINI-1 found by chip detection
- 2026-11-14 — Bug 001 fixed: `serve --detect-timeout` (default 30 s) reaches the job's `run`, so a missing board fails the job instead of holding the worker
- 2026-11-14 — Review fixes: the detect deadline is checked before every probe, and a job whose `run` exit code contradicts its recorded verdict ends as `error`
- 2026-11-14 — Leftover processes of a job are ended whenever its `run` exits, so descendants of an exited child no longer keep the board's port
- 2026-11-14 — MAC pin carried by `submit`, and `run --unique-board` (always passed by `serve`) refuses to guess between same-chip boards; found when a job took the C3-MINI-1 of two attached C3s
