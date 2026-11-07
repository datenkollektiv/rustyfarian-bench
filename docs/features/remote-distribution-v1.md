---
gate: none
desk-work: available
---

# Feature: Remote Distribution v1

A fresh clone of a rustyfarian consumer runs `just bench <target>` without a rustyfarian-bench checkout.

## Decisions
|                                                           Decision | Reason                                             | Rejected Alternative                                 |
|-------------------------------------------------------------------:|:---------------------------------------------------|:-----------------------------------------------------|
|          Public repo `github.com/datenkollektiv/rustyfarian-bench` | Plain `git+https`, no credentials for consumers    | Private repo; self-hosted git                        |
|                     Consumers run `uvx --from git+https://…@<tag>` | Zero manual install, cached, pinned                | `uv tool install` (drifting global state); submodule |
|                        Tag pinned inline in each consumer justfile | Each repo opts into changes on its own schedule    | Tracking `main`; shared justfile snippet             |
| `RUSTYFARIAN_BENCH=<path>` switches a consumer to a local checkout | Bench development against the working tree         | A separate dev recipe per consumer                   |
|                               CI runs `just verify` on push and PR | Same gate as local; tags that pass CI pass locally | pytest only                                          |
|                                 No compatibility policy before 1.0 | Schema still tuning; consumers pin a tag anyway    | Minor bump per schema/CLI change                     |
|                                      Releases via `/release` skill | Version bump, tag and changelog in one checklist   | Hand-made tags                                       |
|                                                      PyPI deferred | Git tags suffice for rustyfarian repos             | Publishing now                                       |

## Constraints
- A consumer needs only `just`, `uv`, `espflash` and the Rust toolchain.
- The TOML schema and CLI (`--repo`, `run`, `list`, `detect`) do not change in this feature.
- `just bench <repo> <target>` in this repo keeps working for sibling checkouts.
- Logs and `results.jsonl` land in the consumer's `bench/<board>/`, never in the uv cache.
- Python ≥ 3.12 stays the floor; `uvx` provisions it if missing.
- macOS-only for running benches; CI runs on Linux because it needs no hardware.
- Pilot consumers are `rustyfarian-ws2812` and `rustyfarian-peripherals`; the remaining rustyfarian repos follow near term.

## Open Questions
- [ ] When is tuning done enough to cut `v0.1.0`?

## State
- [x] Design approved
- [ ] GitHub remote pushed
- [ ] CI workflow running `just verify`
- [ ] `v0.1.0` released via `/release`
- [ ] ws2812 and peripherals `bench` recipes switched to pinned `uvx` with `RUSTYFARIAN_BENCH` override
- [ ] Fresh-clone check: a fresh consumer clone lists its scenarios with no sibling checkout
- [ ] README documents the remote usage and prerequisites

## Session Log
- 2026-11-07 — Feature doc created via /feature dialog
