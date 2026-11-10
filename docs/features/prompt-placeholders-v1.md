---
gate: none
desk-work: available
---

# Feature: Prompt Placeholders v1

`say` and `confirm` fill in `{port}`, `{example}`, `{board}`, `{chip}`, `{mac}` and earlier captures, so an attended step names the device to act on.
Source: `review-queue/rustyfarian-bench-prompt-placeholders-v1.md`, items 1 and 2; item 3 is `prompt-placeholders-v2`.

## Decisions
|                                                     Decision | Reason                                                           | Rejected Alternative                      |
|-------------------------------------------------------------:|:-----------------------------------------------------------------|:------------------------------------------|
|   A missing field renders as `{ip}?` and the step still runs | A prompt is guidance, not a verdict                              | Fail the step like a missing capture      |
|                        The result notes `prompt lacked {ip}` | A reviewer sees the operator was not told the value              | Marker only, nothing recorded             |
|      `{mac}` joins the reserved set for prompts and commands | Detection already knows it; one field dict serves every consumer | A prompt-only `{mac}`                     |
|   `{mac}` missing in `probe`/`run`/`teardown` fails that use | An argv cannot carry a marker; same rule as a missing capture    | Substitute an empty string                |
| Load rule as for `probe`: reserved names or earlier captures | Typos and bad braces fail before hardware is touched             | Accept any capture name, mark at run time |
|       A placeholder nested in a format spec is rejected at load | `{example:{mac}<10}` hides `mac` from the missing-value check   | Collect nested fields recursively         |

## Constraints
- Prompts, `probe`, `run` and `teardown` share one field dict built per attempt; `{mac}` is not special-cased.
- The step result's `say` is the prompt as shown, with values or markers, so results.jsonl matches the terminal.
- No new runtime dependency; `string.Formatter` from the standard library renders markers for missing fields.
- `setup` never gets `{mac}`; detection has not run yet.
- Literal braces in a prompt are written `{{` and `}}`; no shipped consumer prompt contains a brace today.
- The missing-value record is the new `StepResult.lacked` field, not `note`, which holds operator text.
- Keeping existing scenarios byte-compatible is not a constraint; a schema change may ship in a minor.

## Open Questions
- [x] Captures do not cross a mode split: is a `{ip}?` marker acceptable in an `--attended` run, or does the capturing step itself have to be attended? A marker is acceptable; no load-time rule and no attended requirement for the capturing step

## State
- [x] Design approved
- [x] `{mac}` in the reserved set; prompt placeholders validated in `config.py`
- [x] Prompt rendering with markers and the result note in `runner.py`
- [x] Tests passing (render, marker, note, `{mac}` in argv, load errors)
- [ ] rustyfarian-network router steps show MAC and IP on hardware
- [x] Documentation updated (README schema, CHANGELOG)

## Session Log
- 2026-11-10 — Feature doc created via /feature dialog from the review-queue request
- 2026-11-10 — Implemented via workflow: say/confirm placeholders, {mac}, lacked markers, tests, README and CHANGELOG
- 2026-11-10 — Decided: nested format fields are rejected, not collected; no rewrite of the placeholder scanner (reviewer finding)
