 ---
id: 001
title: A remote job for a missing board holds the worker until the job timeout
captured-on: 2026-11-13
doc-version: 3
status: closed
kind: defect
---

# Bug 001: A remote job for a missing board holds the worker until the job timeout

## Symptom
A `serve` job whose board is not attached prints "Please plug in the test device" and holds the single worker until `--job-timeout` (600 s) ends it.

## Suspected Cause
`device.wait_for` (`src/rustyfarian_bench/device.py:201`) loops without a deadline, which suits a local run but not an unattended shared bench.

## Linked Artefact
- `src/rustyfarian_bench/device.py:201` (`wait_for`; `wait_for_usb` has the same shape)
- `docs/features/remote-bench-v1.md` (session log 2026-11-13)
- Reproduction job `tmp/outbox/20261113-193943-72eff1/` (developer-local)

## Reproduction Confidence
high

## Severity
high

## Environment
- macOS laptop, branch `remote-bench` (uncommitted), espflash 4.6.0, no board on USB.
- Applies to any `serve` host, so the Raspberry Pi gate as well.

## Expected Behaviour
- The job fails within seconds with a clear detail such as "no esp32c3 attached".
- The worker is free for the next job at once.

## Actual Behaviour
- The child `run --unattended` waits for a plug-in nobody makes.
- The job ends after 600 s as `error`, exit code -15, detail "timed out after 600 s; last line: • Please plug in the test device".
- Every job queued behind it waits those 600 s too.

## Reproduction Steps
1. Detach every board; `just detect` reports "no candidate serial ports".
2. `just serve --port 18766`.
3. Build an image, e.g. `hal_c3_connect_async` merged with `espflash save-image --merge`.
4. `just submit rustyfarian-network c3 hal_c3_connect_async --check connect --bench http://127.0.0.1:18766 --image <abs path> --wait`.
5. Observe "Please plug in the test device" and the timeout after 600 s; `submit` exits 1.

## Suggested Fix Area
- A detection deadline in `device.wait_for`, used by unattended runs or passed by the job command in `remote.py`.
- Local attended runs keep waiting for a plug-in as today.

## Owner

## Links
- `docs/features/remote-bench-v1.md`
- `docs/adr/001-remote-bench.md`

## Session Log
- 2026-11-13 — Captured as a defect via /bug dialog after the hardware-free wire smoke
- 2026-11-14 — Fixed: `serve` passes `--detect-timeout` (default 30 s) to the job's `run`
- 2026-11-14 — Closed: an absent c6 job ended `error` after 30 s and the queued C3 job passed 3/3 on hardware
