# AGENTS.md

> Use this file as the fast-path operating guide for AI coding agents.
> Prefer repository truth over assumptions — check the files referenced below.

## Project Overview

`rustyfarian-bench` is a Python CLI that runs guided, rerunnable hardware bench tests for the rustyfarian ESP32 crates.
Consumer repos describe boards and checks as TOML under `bench/`; this tool builds, flashes, reads serial output and judges each step.

## Architecture

- `cli.py` — `run`, `list`, `detect` subcommands (entry point `rustyfarian-bench`).
- `config.py` — loads `bench/bench.toml` plus `bench/<board>/*.toml` scenarios.
- `device.py` — polls serial ports and asks `espflash board-info` which chip answers.
- `serial_lines.py` — background pyserial reader that reopens the port across USB re-enumeration.
- `steps.py` — judges `expect` / `duration` / `confirm` steps against the line stream.
- `runner.py` — one session: build, find board, flash, reset, guide steps, append to `results.jsonl`.

## Development Workflow

- `just verify` — ruff lint, format check, `mypy --strict`, pytest; the non-modifying gate.
- `just pre-commit` — `ruff format`, then `verify`.
- `just detect` — probe the connected boards.
- Dependencies are managed with uv (`uv.lock` committed, Python pinned in `.python-version`); never use pip or a hand-activated venv.

## Key Conventions

- Never use `espflash monitor`: without a TTY it can leave a USB-JTAG chip in the flash stub.
- Build and flash commands come from the consumer's `bench.toml` (`commands.build`, `commands.flash` with `{port}`), not from this repo.
- `mypy --strict` covers `tests/` too, and ruff line length is 100.
- Unit tests never need a board: fake serial ports and `espflash` subprocess calls.

## Coding Principles

- **State assumptions** before starting. If a task has multiple valid interpretations, present them rather than picking silently.
- **Simplicity first.** Minimum code that solves the problem. No features beyond what was asked. No abstractions for single-use code. No error handling for impossible scenarios.
- **Surgical changes.** Touch only what the task requires. Do not improve adjacent code, comments, or formatting. Every changed line should trace directly to the user's request.
- When your changes create orphans (unused imports, variables, functions), remove them. Do not remove pre-existing dead code unless asked.
- **Recorded knowledge is load-bearing — verify before acting on it.** README notes, comments, and any project lore/knowledge file capture point-in-time facts that quietly drift. Treat a recorded claim — especially one that steers automated behaviour — as a hypothesis to confirm against the current code, not as ground truth.

## Important Files

- `README.md` — session flow, consumer layout, step kinds and modifiers.
- `justfile` and `pyproject.toml` — the gate and the tool configuration.
- `docs/project-lore.md` — point-in-time debugging discoveries; verify entries against current code before acting on them.
