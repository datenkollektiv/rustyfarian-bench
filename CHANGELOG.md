# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `GET /boards` lists the serial ports attached to the bench host with by-id path, VID:PID and USB serial; chip, MAC and name appear only for a verified MAC, with its source and time.
- `POST /boards/probe` reads the MAC and chip of all listed ports or the named ones with `espflash board-info`, queued behind jobs and polled through `GET /boards/probe/{id}`; the answer says the board was reset, and a port probed in the last 30 s is not reset again.
- `serve --parts PATH` names the listed boards by base MAC from a `parts.toml` (`[[parts]]` with `name`, `mac`, `barcode`), read again on every board list.
- `rustyfarian-bench boards [--probe] [--port P] [--json]` and `just boards` list the boards of a remote bench, with the same URL, token and CA handling as `submit`.
- The `bench` role installs the gitignored `deploy/ansible/parts.toml` as `/etc/rustyfarian-bench/parts.toml`, checked by the bench's own loader, and passes `--parts`; `just pi-boards` lists the boards of the provisioned bench.
- A remote job's record carries `flash`: the mode (`segments` or `flat`) and the ranges written or erased.
- `POST /flash` queues a flash-only job on one bench board, named by `port` or by a verified `part`, through the same queue, outbox and `GET /jobs/{id}` as checks.
- A flash job reads the chip from the image header and passes once the app's first line shows after the reset.
- `rustyfarian-bench flash (--port P | --part NAME) IMAGE [--baud] [--reset] [--expect REGEX] [--wait]`, `just flash` and `just pi-flash` replace the SSH copy-and-flash workaround.
- A remote job's record carries `kind` (`check` or `flash`) and, for a flash job, its `port`.

### Changed

- A remote job writes the merged image by its partition table at `0x8000` (bootloader, table, trimmed `app` partitions, erased `otadata`), so NVS, `phy_init` and every other `data` partition survive the job.
- An image without a partition table is still written flat from `0x0`, and a malformed table is refused with 422.
- An older `serve` cannot read a `job.json` with `flash`: after a rollback, those jobs answer 404 and drop out of the kiosk history.

## [0.8.0] - 2026-11-21

### Added

- The bench host's Waveshare 3.5" panel shows "rustyfarian bench" and the installed version: a `display` role enables the stock `piscreen` overlay, releases the kernel console from the panel and runs a small Pillow banner service, which says "good-bye" when stopped at shutdown; `just display-smoke` renders the banner and the farewell on the controller.
- `deploy/display-probe.sh` identifies an SPI panel on the running Pi without a reboot: it loads an overlay at runtime, paints colour bars and unloads it again.
- `serve --status-file PATH` keeps a world-readable JSON status file (schema 1) for the kiosk panel: state, queue length, the running job's board, example, start and step, and the last 5 verdicts, rewritten on every job change and every 10 s; it never holds a job id, a submitter or log text, and it is off by default.
- `just pi-provision-dev` tests unreleased code on the bench host: it builds a wheel of the checkout under its `.devN` version, and the `bench` role copies and installs it instead of a release tag (`bench_wheel`).
- `rustyfarian-bench kiosk` (needs the new `rustyfarian-bench[kiosk]` extra, Pillow) draws that status file on the SPI panel's framebuffer once a second, writes a frame only when the screen changes and says "good-bye" on SIGTERM; `--once` and `--png-dir` render it without a panel.
- `kiosk --sleep-after SECONDS` (default `$BENCH_SLEEP_AFTER` or 900, 0 turns it off) draws the panel black after that long without a queued or running job or a status change; a tap on the touch panel, a new job, or `serve` going stale or stopping wakes it, and without a readable touch device it never goes dark.
- The `display` role hands the touch panel's event node to the kiosk's group with a udev rule (`display_touch_name`), lets the unit read input devices and passes `display_sleep_after` (default 900) as `--sleep-after`.
- `deploy/touch-probe.sh` shows, read-only and with the kiosk running, whether the bench Pi's touch panel is found, who may read it and which events a tap sends.

### Changed

- The bench host installs `rustyfarian-bench` v0.8.0 by default (`bench_version`).
- The bench host's panel shows the queue instead of the static banner: the running job with its board, example, elapsed time and step bar, the idle screen with version and address, and the last 5 verdicts; a stale heartbeat or a stopped `serve` shows as such.
- The `display` role runs `rustyfarian-bench kiosk` from the bench's install, which now includes the `kiosk` extra; the serve unit gets `RuntimeDirectory=rustyfarian-bench` for the status file; the role refuses a `bench_version` below v0.8.0.
- `just display-smoke` installs the package with the `kiosk` extra and renders status fixtures (idle, running, queued, long names, stale, stopped, maintenance) instead of the banner script.
- `just display-smoke` also checks that the panel goes black, that a tap on a fake touch node wakes it, and that a stop draws the farewell over the dark panel.

### Removed

- The `display` role's banner script `/usr/local/bin/rustyfarian-bench-display` and the apt package `python3-pil`; the role removes both from a provisioned Pi.

## [0.7.0] - 2026-11-20

### Added

- An optional, git-ignored `bench/bench.local.toml` sets per-machine board values (`mac`, `usb_id`, `usb_serial`, `baud`), so a consumer no longer commits a MAC that is wrong on every other machine; the detection line and the wrong-board warning name the pin; `submit` never sends a value from it.
- `deploy/` provisions a Raspberry Pi 5 as the remote bench host: a cloud-init seed (`just pi-seed`) and an Ansible playbook (`just pi-provision`) that installs `serve` as a hardened systemd service with TLS and tokens.
- Bench host secrets (CA, tokens, Wi-Fi) live SOPS-encrypted in the gitignored `shared-secrets/`, handled by `just secrets-edit`, `decrypt`, `encrypt`, `secrets-updatekeys`, `pi-ca-init` and `pi-ca-cert`.
- `just verify` runs `secrets-check` first, which fails on any unencrypted file in `shared-secrets/`.
- `just doctor` checks every bench host prerequisite and names the fix for each gap; `--deep` also decrypts to check the key and the secrets.
- `just pi-upgrade` adds `apt full-upgrade` to a provisioning run, refused while a job is unfinished, as is a bumped `espflash` or `uv`; `pi-provision` leaves OS packages to `unattended-upgrades`.
- `just pi-check` and `just pi-idempotent` check a provisioned bench; `just deploy-lint` lints `deploy/`, also as a separate CI workflow.
- `just audit` runs pip-audit over the locked dependencies with all extras; it needs the network, so it is not part of `verify`.

### Changed

- A board `mac` in `bench.toml` must have the form `aa:bb:cc:dd:ee:ff`, so a typo is a load error instead of a detection that waits forever.
- The justfile follows the family recipe contract: private `_default`, `just setup [bench-host]`, `just act [job]` (a job id, no act arguments) and `ci-verify`/`ci-deploy-lint` as the one recipe each CI job runs; `smoke-install` and `secrets-check` run from scripts.

### Fixed

- A watch window that is the first step no longer sees the banner of the startup reset: the run waits for the boot to settle (about 1 s), so `reject` can include `rst:0x|ESP-ROM:` in every window (bug 002).

## [0.6.0] - 2026-11-15

### Added

- `serve` (needs the `rustyfarian-bench[serve]` extra) runs a shared bench host that takes uploaded images and scenarios over HTTP.
- `serve` runs the jobs one at a time with `run --unattended` and keeps each job in `outbox/<id>/`.
- Without the `serve` extra, `serve` prints the install hint and exits 2.
- `submit <board> <example> --bench URL` builds locally, uploads the image and scenario, and with `--wait` polls the log and exits like `run`; `--image PATH` skips the build and `--check NAME` picks a check.
- `submit` carries the board's `mac` to the server, which pins it in the job's `bench.toml`; a malformed `mac` is refused before the build.
- An optional `image` command in `bench.toml` (under `[commands]` or per board, with `{example}` and `{image}`) writes the merged flash image `submit` uploads.
- `run --no-twins` runs only the named example, not its twins.
- `run --detect-timeout SECONDS` ends the wait for a missing board with an `error`; without it `run` still waits for a plug-in.
- `serve --detect-timeout SECONDS` (default 30) passes that limit to every job.
- `serve` refuses scenarios with `setup`, `teardown`, a `probe` or `run` on an unattended step, a `detect = false` board, or no unattended step.
- `run --unique-board` refuses to guess between boards of the same chip: with two attached and no `mac` or `--port` it ends with an `error`, and a busy or silent port blocks the decision until `--detect-timeout`; `serve` runs every job with it.
- `just serve` and `just submit <repo> <board> <example>` recipes; `just smoke-install` also checks the `serve` hint of the base wheel.
- `serve --token-file PATH` requires a bearer token from that TOML file (`name = "token"`) on every request, re-read per request so a token is added or revoked without a restart; each job records the token's name as `submitter`.
- `serve` refuses a token file other users have access to, and a token that is not printable ASCII or has leading or trailing spaces.
- `serve --ssl-certfile PATH --ssl-keyfile PATH` terminates TLS in uvicorn; off loopback, `serve` refuses to start without the token file and both TLS files.
- `submit` sends `RUSTYFARIAN_BENCH_TOKEN` as a bearer token, trusts the CA in `RUSTYFARIAN_BENCH_CA`, and takes the bench URL from `RUSTYFARIAN_BENCH_URL` when `--bench` is not given; with a token set it refuses a plain `http://` bench off loopback.
- `submit` sends requests to a loopback bench directly, ignoring `HTTP_PROXY` and the system proxy, so a proxy never sees the token.
- `submit` refuses a token that is not printable ASCII (a stray newline, carriage return or other control character) and exits 2 without showing it.

## [0.5.0] - 2026-11-10

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
