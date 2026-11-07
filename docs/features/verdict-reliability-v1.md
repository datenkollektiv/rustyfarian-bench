---
gate: none
desk-work: available
---

# Feature: Verdict Reliability v1

A `pass` in `results.jsonl` means the check was observed and the board was connected the whole time.

## Decisions
|                                                             Decision | Reason                                               | Rejected Alternative              |
|---------------------------------------------------------------------:|:-----------------------------------------------------|:----------------------------------|
|                   Under `--yes`, `confirm` steps record `unverified` | Nobody looked, so it is neither pass nor fail        | Refuse `--yes` with confirm steps |
|        A serial disconnect inside `duration`/`settle` fails the step | Silence from an unplugged board is not "no errors"   | Marking the step inconclusive     |
|                    Placeholders and regexes are checked at load time | Fail before the slow build, not after flashing       | Checking lazily at step start     |
|             Unexpected exceptions record verdict `error` per example | Every run leaves a result explaining what happened   | Crash and record nothing          |
|    Strict TOML types: real bools, finite numbers, non-empty examples | `optional = "false"` must not mean true              | `bool()`/`float()` coercion       |
|                    New verdicts `unverified` and `error` are allowed | Nothing reads `results.jsonl` expecting a closed set | Folding both into `fail`          |
|                       Exit 3 when the only shortfall is `unverified` | CI never shows green for unobserved checks           | Exit 1; exit 0                    |
|        Declined requested examples record `skipped`; the run exits 3 | Declining the rest must not read as a full pass      | Ignoring declines; new exit 4     |
|            Ctrl-C or a missing tool ends the session, `--yes` or not | `--yes` must not build and flash after an abort      | Continuing to the next example    |
|     Outages as intervals; reset blip ignored for `RESET_GRACE` (5 s) | A board that never returns still counts              | A disconnect counter              |
| Optional confirm skipped by `--yes`: recorded, holds no verdict back | Matches what `optional` means elsewhere              | Exit 3 for an optional step       |

## Constraints
- Consumers' valid TOML keeps loading unchanged; only malformed TOML is newly rejected.
- CLI flags stay as they are.
- USB re-enumeration during the reset on open must not count as a lost connection.
- `just verify` gates every change; hardware validation is done manually afterwards.

## Open Questions
- [x] Does a session with `unverified` steps exit non-zero? Yes: exit 3.
- [ ] Is a 5 s `RESET_GRACE` right on every board? Measure the re-enumeration on hardware.

## Deferred
- Require `reject` with `duration` (a bare `duration` is only a link check).
- Warn on unknown top-level keys in scenario TOML.
- Report a `first_example` that names no example in the scenario.

## State
- [x] Design approved
- [x] `--yes` confirm → `unverified`
- [x] Disconnect fails watch windows
- [x] Load-time validation and strict types
- [x] Errors recorded as `error`
- [x] Tests passing, including the reader reconnect lifecycle
- [ ] Hardware run on each board type, including unplug/replug and intentional failures
- [x] README documents the new verdicts

## Session Log
- 2026-11-07 — Feature doc created from an external reliability review
- 2026-11-07 — Fixes 1–4 implemented TDD (161 tests); code-review follow-ups applied; hardware run pending
- 2026-11-07 — Pre-release and first public review: Ctrl-C/missing tool end the session, declined work is `skipped` (exit 3), duplicate board checks rejected
