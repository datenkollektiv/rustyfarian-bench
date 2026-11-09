---
gate: none
desk-work: available
---

# Feature: Prompt Placeholders v2

Board-level variables in `bench.toml`, for facts the log never prints, reach prompts and commands as placeholders.
Source: `review-queue/rustyfarian-bench-prompt-placeholders-v1.md`, item 3; deferred until `prompt-placeholders-v1` has run on hardware.

## Decisions
|                                 Decision | Reason                                                      | Rejected Alternative    |
|-----------------------------------------:|:------------------------------------------------------------|:------------------------|
| Deferred behind `prompt-placeholders-v1` | The log and the detection record may already cover the need | Ship both in one change |

## Constraints
- Variables must not shadow reserved names or captures; the loader rejects the collision.
- Values are strings only, validated at load time like every other scenario fact.
- No new runtime dependency.

## Open Questions
- [ ] Is a per-board table (`[boards.c6.vars]`) enough, or do scenarios need their own variables too?
- [ ] Do variables reach commands (`probe`, `run`, `setup`, `teardown`) or prompts only?
- [ ] Which consumer fact is not in the log or the detection record, beyond a router client name?

## State
- [ ] Design approved
- [ ] Core implementation
- [ ] Tests passing
- [ ] Documentation updated

## Session Log
- 2026-11-10 — Feature doc created via /feature dialog; deferred, design pending v1's hardware run
