# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `probe = ["cmd", ...]` on an `expect` or `duration` step judges a host command's stdout instead of the serial lines, so a broker check no longer needs a `confirm`.
  The command runs in the consumer directory in its own process group, is ended with SIGTERM and then SIGKILL, and may use `{port}`, `{example}`, `{board}`, `{chip}` and earlier captures.
  Probe steps are unattended, and a command that exits early reads as a lost link; POSIX only.
- `serial_reject = 'regex'` on a `probe` step judges the serial lines while the probe runs, so a board panic fails the step instead of going unnoticed.
- `attended = true` is allowed on a `probe` step: it runs with `--attended` and is skipped by `--unattended`, and Enter ends an attended probe `duration` early.
- Scenario `setup` and `teardown` command lists run once per example (setup before the build, teardown always, also after Ctrl-C), with `hook_timeout` (default 60 s) per command.
  A failed setup is an `error` before anything is built or flashed; a failing teardown command is a warning.
- `run = ["cmd", ...]` on an `expect` or `duration` step runs a host command after the reset and before judging, with `run_timeout` (default 30 s); a failing run fails the step.
  Each command's argv, exit code, duration and output tails are recorded, with a log beside the serial log.
- `say` and `confirm` fill in `{port}`, `{example}`, `{board}`, `{chip}`, `{mac}` and earlier captures, so an attended step names the device to act on.
  A missing value renders as `{ip}?` and the step still runs; the result records it in `lacked` and the runner warns once per step.
- `{mac}` joins the placeholders of `probe`, `run` and `teardown` (not `setup`); when the MAC is unknown the use fails like a missing capture, and a teardown command is skipped.

### Changed

- `results.jsonl` gains `setup` and `teardown` per example and `run` per step; the new fields are always present and the old keys are unchanged.
- A literal brace in `say` or `confirm` now needs doubling (`{{`, `}}`); a stray brace or an unknown name is a config error at load time.
- `results.jsonl` gains `steps[].lacked`, always present, a list of the prompt values that were missing.

### Fixed

- A `run` step with `duration` no longer judges serial lines left over from earlier steps; the queue is cleared right before the command runs, and the board's reaction to it is still judged.
- A placeholder nested in a format spec, such as `{example:{mac}<10}`, is rejected at load time; it used to slip past the missing-value check and abort the example at run time.
- `serial_reject` now judges a serial line the board prints while the probe is starting; the backlog is cleared before the probe spawns, not after.

## [0.4.0] - 2026-11-09

### Fixed

- Opening or reopening the port no longer reboots a USB Serial/JTAG chip: the reader clears RTS before DTR, so a woken deep-sleep board keeps its retained state and each log starts with one boot instead of two.
  A `reset = "dtr"` board never gets DTR from the reader outside its reset pulse, since DTR is its reset line.

### Added

- A board with `detect = false` is found by its USB identity, `usb_id = "vid:pid"` (plus `usb_serial` to tell two identical bridges apart), so it needs no `--port`; several matching ports stop the run and ask which one.
- A named group in an `expect` pattern, `(?P<pole>SOUTH|NORTH)`, captures the matched text; a later step refers to it as `(?P=pole)`, so a check can start on either side and then ask for the other one.
  Captures are recorded per step, and a reference with no earlier capturing step is a config error at load time.
- `reset = true` on a step resets the board once the step starts (after Enter when armed), for a check the firmware runs at boot, such as a calibration window; a retry resets again.
  A board with `reset = "none"` cannot use it (config error at load time).
- Each step result records its `evidence`: the line every `expect` pattern matched, so a reviewer can check the values behind a pass.

### Changed

- During a timed wait the step's instruction stays pinned above the countdown, so device output cannot scroll it away.
- A typed watch length longer than the scenario's asks for confirmation (default no), so a typo like `600` for `60` cannot lock the operator in.
- "Retry this step?" and "Continue with the next step?" mute the device echo like the other prompts, so a chatty device cannot bury the question.

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
- `just bench <repo> <target>` runs a sibling consumer's checks from this checkout.
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
