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

Directly:

```sh
uv run --project ../rustyfarian-bench rustyfarian-bench --repo . run hal_s3_b3f
uv run --project ../rustyfarian-bench rustyfarian-bench --repo . list
uv run --project ../rustyfarian-bench rustyfarian-bench detect
```

## Session flow

1. Build the example (`commands.build`), so the slow part runs before the board is needed.
2. Poll serial ports every 0.5 s and run `espflash board-info` on each until the board's chip (and optional MAC) answers.
3. Flash (`commands.flash`, with `{port}`), then `espflash reset`.
4. Read the port with pyserial, reopening it across USB re-enumeration; every line is echoed and written to `bench/<board>/logs/<example>-<stamp>.log`.
5. Run the steps; a failed step offers a retry.
6. Offer the scenario's next example (e.g. the `idf` twin), then append the result to `bench/<board>/results.jsonl`.

The runner never uses `espflash monitor`: without a TTY it can leave a USB-JTAG chip in the flash stub.

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

| Key        | Passes when                                                    |
|:-----------|:---------------------------------------------------------------|
| `expect`   | the regexes match in order within `timeout` (default 30 s)     |
| `duration` | `reject` never matches for that many seconds                   |
| `confirm`  | the operator answers yes                                       |

Modifiers: `reject` (fails the step on match), `settle` (keep watching `reject` after the last `expect`), `optional` (a miss is a warning).
`expect` steps also see lines printed before the step started (e.g. the boot banner); `duration` steps only judge new lines.

## Development

```sh
just verify   # ruff, format check, mypy --strict, pytest
```
