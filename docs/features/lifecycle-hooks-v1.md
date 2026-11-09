---
gate: none
desk-work: available
---

# Feature: Lifecycle Hooks v1

A scenario starts and restores its host side, and a step can act on the host before it judges, so the C6 MQTT check needs a person only for physical actions.
Source: `review-queue/rustyfarian-bench-lifecycle-hooks-v1.md`; builds on `host-probes-v1`.

## Decisions
|                                                    Decision | Reason                                                                | Rejected Alternative                  |
|------------------------------------------------------------:|:----------------------------------------------------------------------|:--------------------------------------|
|               Scenario `setup` and `teardown` command lists | The broker must be up before the flash and restored after any outcome | Operator runs the recipes by hand     |
|                                  Hooks run once per example | Each result row is self-contained and `{example}` resolves            | Once per scenario                     |
|         Step `run` is synchronous with `run_timeout` (30 s) | A hang fails fast and the step judges a known state                   | Background actions (`run_background`) |
|               With a `probe`, the probe starts before `run` | A subscriber must be up to see what the action publishes              | Run first, probe second               |
| A failed `run` fails the step; a failed setup is an `error` | Step failures are retryable, setup failures stop before the flash     | Treat both the same                   |
|             Placeholders and validation shared with `probe` | One `_command` check, one substitution path                           | A second placeholder scheme           |

## Constraints
- Teardown runs after pass, fail, error, a failed setup and Ctrl-C; a second Ctrl-C skips the rest.
- A failing teardown command is a warning in the result, never a verdict change.
- A setup command that exits non-zero, hangs or cannot start is verdict `error` before anything is built or flashed.
- Setup may use only `{example}` and `{board}`; `run` and teardown also get `{port}`, `{chip}` and captures where they exist.
- Every command records argv, exit code, duration and a stdout/stderr tail in the result and in a log beside the serial log.
- No new runtime dependency; stdlib `subprocess` with the process-group teardown from `host-probes-v1`.
- A step with `run` and no `confirm` or `attended` is unattended.

## Open Questions
- [x] Should a later step be able to refer to `run` stdout, for example through a `run_expect` with named groups?
  No in v1: `run` output is only recorded as a tail, so use a `probe` to judge output.
- [x] Should the port be detected before setup so `{port}` works there, for a hub power cycle before the flash?
  No: detection stays after the build, so `{port}` is not available in setup.
- [x] Does an example the mode skips entirely run setup and teardown?
  No: an example, the mode drops entirely runs no hooks.
- [x] Does any consumer parse `results.jsonl` strictly, so the new `setup`, `teardown` and `run` fields need a check first?
  The schema change is additive, and the fields are always present; the consumer confirms in its reply.

## State
- [x] Design approved
- [x] `setup`, `teardown` and `run` parsed and validated in `config.py`
- [x] Command runner with timeout, evidence record and log
- [x] Hooks wired into `_run_example`, teardown in `finally`
- [x] Tests passing (fake spawner, Ctrl-C path, setup failure before flash)
- [x] rustyfarian-network C6-H2 T1 to T4 unattended on hardware (10/10, local broker)
- [ ] Consumer's `--attended` run reported: a `serial_reject` match, an attended probe, teardown after a failure
- [x] Documentation updated (README schema, CHANGELOG)

## Session Log
- 2026-11-09 — Feature doc created via /feature dialog from the review-queue request
- 2026-11-09 — Implemented via workflow: setup, teardown, run, CommandRecord, tests, README and CHANGELOG
- 2026-11-09 — Gate cleared by the consumer's C6 run; README `setup` example switched to `just mqtt-pub bench-ping 1` (review-queue finding)
