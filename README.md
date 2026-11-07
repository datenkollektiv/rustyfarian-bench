# rustyfarian-bench

<p align="center">
  <img
    src="docs/steampunk-meets-future-on-testing-bench.png"
    alt="Ride inspector — guided bench checks. A steampunk fairground (no_std / esp-hal) and a
    neon test console (std / esp-idf), with the rustyfarian ride inspector mascot checking an
    ESP32 on the bench: build, flash, reset, watch, verdict. Every ride checked before opening."
    width="720">
</p>

[![Verify](https://github.com/datenkollektiv/rustyfarian-bench/actions/workflows/verify.yml/badge.svg)](https://github.com/datenkollektiv/rustyfarian-bench/actions/workflows/verify.yml)
[![Release](https://img.shields.io/github/v/tag/datenkollektiv/rustyfarian-bench?label=release&sort=semver)](https://github.com/datenkollektiv/rustyfarian-bench/tags)
[![License](https://img.shields.io/badge/license-MIT%2FApache--2.0-blue.svg)](LICENSE-MIT)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.14-blue.svg)](pyproject.toml)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy.readthedocs.io/)

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.

A consumer repo describes its boards and checks as TOML under `bench/`.
The runner builds the example, waits for the right board, flashes it, resets it, captures the serial output, and walks the operator through each step with an automatic verdict.

## Getting started

Prerequisites: [`just`](https://github.com/casey/just), [`uv`](https://docs.astral.sh/uv/), [`espflash`](https://github.com/esp-rs/espflash) and the consumer's Rust toolchain.
Python ≥ 3.12 is enough; `uvx` provisions it if missing.
Hardware runs are tested on macOS only; Linux serial ports are detected but untested on a bench, and Windows is unsupported.

Run a pinned release from a consumer repo, with no rustyfarian-bench checkout:

```sh
uvx --from git+https://github.com/datenkollektiv/rustyfarian-bench@v0.1.0 rustyfarian-bench --repo . list
uvx --from git+https://github.com/datenkollektiv/rustyfarian-bench@v0.1.0 rustyfarian-bench --repo . run s3
```

Pin the tag in the consumer's `bench` recipe; bumping it is how a consumer opts into a new release.

## Usage

From a consumer repo (e.g. `rustyfarian-peripherals`):

```sh
just bench hal_s3_b3f     # one example, then offers the scenario's next examples
just bench s3/rotary      # one check on one board
just bench s3             # every check for a board
```

From this repo, against a sibling checkout:

```sh
just bench rustyfarian-peripherals s3 b3f   # <repo> <target> [flags]
just bench rustyfarian-ws2812 s3
just bench-list rustyfarian-ws2812
```

Directly:

```sh
uv run --project ../rustyfarian-bench rustyfarian-bench --repo . run hal_s3_b3f
uv run --project ../rustyfarian-bench rustyfarian-bench --repo . list
uv run --project ../rustyfarian-bench rustyfarian-bench detect
```

## Session flow

1. Build the example (`commands.build`, or the board's own `build`), so the slow part runs before the board is needed.
2. Poll serial ports and run `espflash board-info` on each until the board's chip (and optional MAC) answers; a port another session holds is skipped, so two boards can bench side by side (pin `mac` when two boards share a chip: the lock is only held while a session reads its port).
   A board with `detect = false` (e.g. an AVR, which espflash cannot identify) never runs espflash; it always needs `--port` (a lone attached port is no proof of which board it is), and the run stops without it.
3. Flash (`commands.flash`, or the board's own `flash`, with `{port}`).
4. Open the port exclusively with DTR/RTS deasserted, reset into the app, and read it with pyserial, reopening across USB re-enumeration. The reset is an RTS pulse (esptool's USB-JTAG sequence) by default; `reset = "dtr"` pulses DTR for auto-reset bridges (Arduino Nano, CH340/FT232) and `"none"` skips it; `baud` is per board. Every line is echoed and written to `bench/<board>/logs/<example>-<stamp>.log`.
5. Run the steps; each step waits for Enter and drops the backlog before its clock starts (`arm = false` for a step that must read what the reset printed, e.g. the banner); a failed step offers a retry, then asks what the operator saw and records it as the step's `note`.
   Timed waits show a shrinking countdown on a terminal; an armed `duration` step's prompt shows its length, where Enter keeps it and a typed number of seconds changes it for this run (the step detail records `(scenario: N s)`).
6. Offer the scenario's next example (e.g. the `idf` twin), then append the result to `bench/<board>/results.jsonl`; an example you asked for (everything but the twins of a named example) that you decline is recorded as `skipped`.

The runner never uses `espflash monitor` (without a TTY it leaves a USB-JTAG chip in the flash stub) or `espflash reset` (it enters the bootloader first and leaves a native-USB S3 in download mode).

## Consumer layout

```
bench/
├── bench.toml        commands + boards
└── s3/
    ├── b3f.toml      one scenario
    ├── logs/         captured output
    └── results.jsonl one JSON line per example run
```

`bench/bench.toml`:

```toml
[commands]
build = ["just", "build-example", "{example}"]
flash = ["just", "flash", "{example}", "{port}"]

[boards.s3]
chip = "esp32s3"
name = "CrowPanel 1.28\" HMI (ESP32-S3)"
# mac = "dc:54:75:..."   # pin one physical board
# checks = ["pulse", "rainbow"]   # run these first, in this order; the rest follow by name

[boards.avr]
chip = "atmega328p"
name = "Arduino Nano"
detect = false                                  # espflash cannot identify it; --port is required
reset = "dtr"                                    # auto-reset bridge; default "rts", or "none"
# baud = 115200
build = ["just", "build-avr-bin", "{example}"]  # per-board commands override [commands]
flash = ["just", "flash-avr-bin", "{example}", "{port}"]
```

A scenario:

```toml
examples = ["hal_s3_b3f", "idf_s3_b3f"]   # run in order; the shared steps apply to each

[[step]]
say = "Press the knob button 3 times"
expect = ['count=1\)', "released", 'count=2\)', "released", 'count=3\)', "released"]
reject = 'count=[4-9]'
settle = 2
```

## Step kinds

Each step has `say` and exactly one of:

| Key        | Passes when                                                               |
|:-----------|:--------------------------------------------------------------------------|
| `expect`   | the regexes match in order within `timeout` (default 30 s)                |
| `duration` | `reject` never matches and the serial link stays up for that many seconds |
| `confirm`  | the operator answers yes                                                  |

Modifiers: `reject` (fails the step on match), `settle` (keep watching `reject` after the last `expect`), `optional` (a miss is a warning).
`expect` steps also see lines printed before the step started (e.g. the boot banner); `duration` steps only judge new lines.

The definition is checked strictly when it loads, before anything is built:
- Booleans and numbers must be real TOML values, so `optional = "false"` is an error rather than true.
- `timeout` and `duration` must be greater than 0, and `settle` must be 0 or more.
- Regexes must compile.
- Commands may use only `{example}`, plus `{port}` in `flash`.
- `examples` must not be empty, and the names must not contain `/` or `..`.
- A board's `checks` must not list a name twice.

A lost serial link fails a `duration` or `settle` window.
The re-enumeration caused by the reset is ignored for `RESET_GRACE` (5 s; none with `reset = "none"`), so an unplug within the first 5 s after the reset goes unnoticed by watch windows.

## Verdicts and exit codes

| Verdict      | Meaning                                                                                          |
|:-------------|:-------------------------------------------------------------------------------------------------|
| `pass`       | every required step was observed and passed                                                      |
| `fail`       | a step failed, or the run stopped before the last step                                           |
| `unverified` | nothing failed, but a required `confirm` was skipped by `--yes`                                  |
| `error`      | the run broke (missing tool, crash); the detail names the cause; a missing tool ends the session |
| `aborted`    | Ctrl-C; the session ends, later examples do not start                                            |
| `skipped`    | the operator declined a requested example; makes the run incomplete                              |

`run` exits 0 when every example passed and 3 when the only shortfall is `unverified` or skipped work.
It exits 1 on any fail, error or abort, 2 on a bench definition error, and 130 on Ctrl-C between examples.

## Development

```sh
just verify               # ruff, format check, mypy --strict, pytest
just verify-python 3.12   # the same gate on the declared Python floor
just smoke-install        # build the wheel and run it through an isolated uvx
```
