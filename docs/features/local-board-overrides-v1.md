---
gate: none
desk-work: available
---

# Feature: Local Board Overrides v1

An optional, git-ignored `bench/bench.local.toml` sets per-machine board values, so a consumer's committed `bench.toml` carries no board MACs.
Source: `review-queue/rustyfarian-bench-local-board-overrides-v1.md`, from family check B08 (no `mac` in the committed `bench.toml`).

## Decisions
|                                                                      Decision | Reason                                                              | Rejected Alternative                       |
|------------------------------------------------------------------------------:|:--------------------------------------------------------------------|:-------------------------------------------|
|          Local keys: `mac`, `usb_id`, `usb_serial`, `baud`; others are errors | A local file must not change what is tested                         | Any key; `mac` only                        |
|              Any top-level table but `boards`, or an undefined board, is an error | A typo must not silently do nothing                                 | Ignore unknown tables and boards           |
|                                  The local file wins, merged key by key | B08 moves the MAC out of `bench.toml`; the local value is the truth here | Error on a value set in both files         |
|                The committed board must load on its own, then the merged one | Other machines have no local file; errors name the file that caused them | Validate only the merged table             |
|   Raw tables merge before `_parse_board`, which validates the merged values | One validation path for both files                                  | `dataclasses.replace` on a parsed `Board`  |
|                     `Board.mac_source` names the file when it supplied `mac` | The detection line and the wrong-MAC warning say where the pin is from | Provenance for every key; results.jsonl field |
| `submit` sends only the committed `mac` and `baud` (`Board.committed_mac`, `committed_baud`) | Local values describe this machine; the bench host has other boards | The merged values (first cut, reverted in review) |
|                       A `mac` in either file must be `aa:bb:cc:dd:ee:ff` on load | A typo or `dc:54:75:...` would make detection wait forever          | Check only in `submit` and `serve`         |
|                                           `reset` is not a local key | Reset mode follows the board's USB hardware; other wiring is another board | Allow `reset` per machine                  |
|                    A local `mac` on a `detect = false` board is accepted silently | Same as a committed one today                                       | Warn or reject                             |

## Constraints
- Without a local file, loading behaves exactly as before.
- `serve` never reads a local file: job directories hold only the server-written `bench.toml`.
- Nothing new in `results.jsonl`; the JSONL contract is unchanged.
- The scenario glob `bench/<key>/*.toml` never matches `bench/bench.local.toml`.

## Drift from the request
- `usb_serial` joins the allowed keys; it is as machine-specific as `usb_id`.
- The request's "Out of scope: remote runs change nothing" holds: `submit` sends the committed `mac` and `baud` as in v0.6.0, never local ones.

## Follow-ups outside this repo
- rustyfarian-ci `conventions/bench.md`: B08 "once the runner supports it" wording, and a check for the `bench/bench.local.toml` ignore line.
- rustyfarian-power: drop the `s3-heltec` and `s3` MACs, add the ignore line, note the per-machine file.
- Consumers bump their bench pin to the release that ships this.
- Host-side board pinning (roadmap, mid term): the bench host owns its MACs, so a remote job can pin a board.

## Open Questions
- (none)

## State
- [x] Design approved
- [x] Core implementation (`config.py` merge, `Board.mac_source`, `device.py` messages)
- [x] Tests passing (config, device, client, remote)
- [x] Documentation updated (README, AGENTS.md, CHANGELOG)
- [ ] rustyfarian-power runs with its MACs in `bench.local.toml`

## Session Log
- 2026-11-17 — Feature doc created from the review-queue request after an analysis workflow (python-pro, test-automator, code-reviewer, critic)
- 2026-11-17 — Implemented via workflow (test-automator, python-pro, code-reviewer); local file read only after bench.toml parses; `just verify` 1064 passed
- 2026-11-17 — Review fixes: MAC format checked on load, `submit` sends only the committed `mac`, `reset` kept out of the local keys
- 2026-11-17 — Second review: a local `baud` no longer reaches `submit` either
