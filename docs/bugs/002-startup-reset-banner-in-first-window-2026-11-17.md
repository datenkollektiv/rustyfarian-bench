---
id: 002
title: The startup reset's boot banner lands inside the first watch window
captured-on: 2026-11-17
doc-version: 2
status: open-defect
kind: defect
---

# Bug 002: The startup reset's boot banner lands inside the first watch window

## Symptom
A watch-only window that is the first selected step after the startup reset fails on `rst:0x`/`ESP-ROM:`, the banner of the reset the runner itself asked for.

## Suspected Cause
`SerialLines.__enter__` returns once the port is open, before the startup pulse, so the window's single drain runs before the chip reboots.

## Linked Artefact
- `src/rustyfarian_bench/serial_lines.py:97-100` (`__enter__` waits only for `_opened`).
- `serial_lines.py:239-244`: `_mark_connected()` sets `_opened` before `_pulse()`; `_hard_reset` alone takes 0.4 s.
- `src/rustyfarian_bench/runner.py:421`: the runner starts the first step as soon as `__enter__` returns.
- `src/rustyfarian_bench/steps.py:140-143`: a watch-only window drains the backlog once, at its start.
- Source: `review-queue/rustyfarian-bench-startup-reset-banner-race-v1.md`, from `rustyfarian-ws2812` (check B12).

## Reproduction Confidence
medium — the source trace holds on `main` (7897fb0); no hardware run shows it yet.

## Severity
medium

## Environment
- Any board with `reset = "rts"` or `"dtr"`; every ESP chip prints `rst:0x`, C3/C6/S3 also print `ESP-ROM:`.
- UART-bridge boards keep the ROM lines reliably; native USB boards may lose them while re-enumerating, which only hides the bug.
- Full, `--unattended` and `--attended` runs alike; `--attended` filtering makes an attended watch the first step.

## Expected Behaviour
A window after the startup reset judges only what happens after the boot, so `reject = 'rst:0x'` fails only on a reboot inside the window.

## Actual Behaviour
The startup boot banner arrives after the window's drain and fails the window on the reset the runner requested.

## Reproduction Steps
1. Write a scenario whose first step is `duration = 10` with `reject = 'rst:0x|ESP-ROM:'` and `arm = false`.
2. Run it on a UART-bridge ESP board with `reset = "rts"` or `"dtr"`.
3. The window fails on the ROM banner within its first second.

## Suggested Fix Area
Fixed in `serial_lines.py`, awaiting the hardware check:
- `__enter__` waits for the startup pulse (bounded by `PULSE_WAIT`), then until the link has been quiet for `BOOT_QUIET` (1 s), at most `BOOT_SETTLE` (`RESET_GRACE`) after the pulse.
- Quiet counts only while the port is open, and a reconnect restarts it, so native USB re-enumeration is not taken for a finished boot.
- No drain in `__enter__`: an `expect` step still finds the banner; a watch-only window's own drain removes it.
- Cost: about 1.2 s per run with a reset, at most `BOOT_SETTLE` for an app that logs without pause.
- Tests: `tests/test_serial_lines.py` (bug 002 block) and the `tests/conftest.py` fixture that keeps the wait short.

## Workaround
Consumers leave `reject_reboot` out of an affected window, with a comment (`rustyfarian-ci/conventions/bench.md`, `reject_reboot` rule); `rustyfarian-ws2812` carries 14 such exemptions.

## Owner
maintainer

## Links
- `docs/project-lore.md` "Serial Monitoring" (stale bytes and numbered reset requests).
- `rustyfarian-ci/conventions/bench.md`: the `reject_reboot` exemption rule, retired or narrowed once this ships.

## Session Log
- 2026-11-17 — Promoted from the review queue to a defect via /bug dialog; source trace re-checked on `main` 7897fb0
- 2026-11-17 — Fixed via workflow (test-automator, python-pro, two reviews); stays open until the hardware check (`continue-here.md`, local) passes
