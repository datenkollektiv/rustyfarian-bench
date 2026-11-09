---
gate: none
desk-work: available
---

# Feature: Host Probes v1

A step can be judged from a host command's output instead of serial, so the broker `confirm` questions become unattended checks.

## Decisions
|                                                Decision | Reason                                                                   | Rejected Alternative                                  |
|--------------------------------------------------------:|:-------------------------------------------------------------------------|:------------------------------------------------------|
|   Generic `probe` command step; consumers bring tooling | The bench stays protocol-agnostic with no new dependency                 | Built-in MQTT probe via paho-mqtt                     |
|         Probe stdout streams through the serial matcher | A watcher such as `mosquitto_sub` must listen while the board sends      | Run to completion then match; exit code only          |
| `probe = [...]` on the step redirects `expect`/`reject` | One source per step keeps verdict and evidence unambiguous               | `[step.probe]` sub-table; scenario-level `[[probe]]`  |
|     The subprocess implements the existing `LineSource` | One matcher; only the lost-link text changed, process exit reads as lost | A second matcher for command output                   |
|   Placeholders validated at load time like `[commands]` | Unknown names fail before any hardware is touched                        | Substitute at run time and fail mid-run               |
|                              Probe steps are unattended | No person is involved; they replace the broker `confirm` questions       | Treat them as attended                                |
|       `serial_reject` guards serial during a probe step | A panic while the broker is watched must fail the step, not vanish       | Apply `reject` to both sources; a guard step after it |
|               `attended = true` allowed on a probe step | The probe that observes an attended action runs and skips with it        | A `follows` link to the previous step                 |

## Constraints
- Standard library `subprocess` only; no new runtime dependency.
- A probe step records matched lines as evidence, fails on `reject` or timeout, and errors when the command cannot start.
- A probe step honours `optional`, `settle` and captures exactly like a serial step.
- `probe` pairs with `expect` or `duration`, never with `confirm`.
- `--unattended` runs probe steps and `--attended` skips them.
- The command runs in the consumer directory holding `bench.toml`, in its own process group.
- Keeping existing scenarios unchanged is not a constraint; a schema change may ship in a minor per `release-plan.md`.

## Open Questions
- [x] Should a serial `reject` or a lost link during a probe step fail that step, or only surface in the next serial step?
  Neither: serial lines are not judged during a probe step, and a lost serial link only prints a warning after it.
- [x] Does process-group teardown (SIGTERM, then SIGKILL after 2 s) kill `just` wrappers and their children on macOS and Linux?
  A real-`sh` test proves it for a child and a SIGTERM-ignoring child; the live `just mqtt-watch` run is the hardware proof.
- [x] Which placeholders does a probe need beyond `{port}`, `{example}` and earlier captures, for example the board's chip or name?
  `{board}` and `{chip}` are added; there is no `{mac}` (None on undetected boards) and no display name.
- [x] Which consumer proves the probe on hardware first: rustyfarian-network's C3 broker steps, or a bench fixture?
  The rustyfarian-network C3 broker steps, with no separate fixture, run by the maintainer after this ships.

## State
- [x] Design approved
- [x] `probe` parsed and validated in `config.py`
- [x] Subprocess `LineSource` with process-group teardown
- [x] Probe steps judged by `run_step`, evidence recorded, attended filter honoured
- [x] Tests passing (fake subprocess source, teardown, config errors)
- [x] rustyfarian-network broker `confirm` steps replaced and run on hardware (C6, not the C3 the gate named)
- [x] Documentation updated (README schema, CHANGELOG)
- [x] `serial_reject` and attended probes (consumer review, round 2)
- [x] README notes that a probe sees the retained replay at subscribe time

## Session Log
- 2026-11-09 — Feature doc created via /feature dialog
- 2026-11-09 — Implemented via workflow: probe step, ProbeLines, tests, README and CHANGELOG
- 2026-11-09 — Updated: consumer review (review-queue) adds `serial_reject`, attended probes and a retained-replay note
- 2026-11-09 — Implemented serial_reject, attended probes and the retained-replay note
- 2026-11-09 — Gate cleared: consumer ran `just bench c6 mqtt-scenarios --unattended`, 10/10 with four probe steps (review-queue)
