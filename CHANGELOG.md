# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- During a timed wait the step's instruction stays pinned above the countdown, so device output cannot scroll it away.
- A typed watch length longer than the scenario's asks for confirmation (default no), so a typo like `600` for `60` cannot lock the operator in.

## [0.3.0] - 2026-11-07

### Added

- Timed waits show a shrinking countdown, and an armed `duration` step's prompt accepts a different length in seconds for that run.
- `--unattended` runs only the steps that need no person (implies `--yes`) and `--attended` only those that do; steps that need a person without a `confirm` set `attended = true`, and results record the `mode`.
- An `attended` watch (a `duration` step with `attended = true`) ends early when the operator presses Enter; lines already received are still judged, and the detail records how long it ran.

## [0.2.0] - 2026-11-07

### Added

- `detect = false` on a board skips espflash detection, for boards it cannot identify (e.g. AVR); such a board needs `--port`.
- Per-board `build` and `flash` commands override `[commands]`, with the same placeholder checks.
- Per-board `baud` (default 115200) and `reset = "rts" | "dtr" | "none"` (default `rts`) for the serial monitor.

### Changed

- A bad placeholder or empty list in a board's own `build`/`flash` is a config error at load time.

## [0.1.0] - 2026-11-07

### Added

- `rustyfarian-bench` CLI with `run`, `list` and `detect`, pointed at a consumer via `--repo`.
- Consumer bench definitions as TOML under `bench/`: `bench.toml` (commands, boards) plus one scenario file per check.
- Session flow: build, wait for the board by chip (and optional MAC), flash, RTS hard-reset, capture serial, run steps, offer the scenario's next example.
- Step kinds `expect`, `duration` and `confirm`, with `reject`, `settle`, `optional` and `arm` modifiers.
- Strict definition checks at load time: real TOML types, positive timeouts, compiling regexes, known command placeholders, safe example names, unique board check names.
- Verdicts `pass`, `fail`, `unverified`, `skipped`, `error` and `aborted`, with distinct exit codes (0, 1, 2, 3, 130); a declined requested example makes the run incomplete (exit 3).
- Per-run logs in `bench/<board>/logs/` and one JSON line per example in `bench/<board>/results.jsonl`.
- Exclusive port locking, so two boards can bench side by side.
- `just bench <repo> <target>` and `just bench-list <repo>` to run a sibling consumer's checks from this checkout.
- CI workflow running `just verify` on push and pull request, on Python 3.12 (the floor) and 3.14.
- `just smoke-install` builds the wheel and runs it through an isolated `uvx`, as a consumer would.
- `just verify-python <version>` runs the gate on another Python in a scratch venv.
- README getting-started section: prerequisites, the pinned `uvx` command, macOS-only hardware support.
