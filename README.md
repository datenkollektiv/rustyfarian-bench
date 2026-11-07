# rustyfarian-bench

Guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.

A consumer repo describes its boards and checks as TOML under `bench/`.
The runner builds the example, waits for the right board, flashes it, resets it, captures the serial output, and walks the operator through each step with an automatic verdict.

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

1. Build the example (`commands.build`), so the slow part runs before the board is needed.
2. Poll serial ports and run `espflash board-info` on each until the board's chip (and optional MAC) answers; a port another session holds is skipped, so two boards can bench side by side (pin `mac` when two boards share a chip: the lock is only held while a session reads its port).
3. Flash (`commands.flash`, with `{port}`).
4. Open the port exclusively with DTR/RTS deasserted, hard-reset into the app via RTS (esptool's USB-JTAG sequence), and read it with pyserial, reopening across USB re-enumeration; every line is echoed and written to `bench/<board>/logs/<example>-<stamp>.log`.
5. Run the steps; each step waits for Enter and drops the backlog before its clock starts (`arm = false` for a step that must read what the reset printed, e.g. the banner); a failed step offers a retry, then asks what the operator saw and records it as the step's `note`.
6. Offer the scenario's next example (e.g. the `idf` twin), then append the result to `bench/<board>/results.jsonl`.

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

A lost serial link fails a `duration` or `settle` window.
The re-enumeration caused by the reset is ignored for `RESET_GRACE` (5 s), so an unplug within the first 5 s after the reset goes unnoticed by watch windows.

## Verdicts and exit codes

| Verdict      | Meaning                                                         |
|:-------------|:----------------------------------------------------------------|
| `pass`       | every required step was observed and passed                     |
| `fail`       | a step failed, or the run stopped before the last step          |
| `unverified` | nothing failed, but a required `confirm` was skipped by `--yes` |
| `error`      | the run broke (missing tool, crash); the detail names the cause |
| `aborted`    | Ctrl-C                                                          |

`run` exits 0 when every example passed and 3 when the only shortfall is `unverified`.
It exits 1 on any fail, error or abort, 2 on a bench definition error, and 130 on Ctrl-C between examples.

## Development

```sh
just verify   # ruff, format check, mypy --strict, pytest
```
