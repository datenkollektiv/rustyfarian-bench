---
gate: hardware
gate-note: records show `usb_id` for the attached C3, S3 and AVR boards, on macOS and on the Pi
desk-work: available
---

# Feature: USB Identity v1

A board's identity is inferred today from `results.jsonl` port names and MACs, and records show one key used for two boards.
Every result records the port's USB VID:PID and serial number, and `usb_ids` in `bench.toml` refuses a port of the wrong kind.
Requested by family-resemblance 1.6.0 check B07, whose run-count "home MAC" rule is a stopgap; see [ADR-002](../adr/002-board-usb-identity.md).

## Decisions
|                                                      Decision | Reason                                                  | Rejected Alternative                 |
|--------------------------------------------------------------:|:--------------------------------------------------------|:-------------------------------------|
|             `usb_ids = ["vid:pid", …]` allowed on every board | One concept for detected and undetected boards          | A filter beside the `usb_id` locator |
| `usb_id` stays as a one-entry alias; setting both is an error | AVR consumers keep working                              | Widen `usb_id` to a string or a list |
|                      `usb_serial` needs `usb_ids` or `usb_id` | Unchanged rule, now also filtering detected boards      | A serial number on its own           |
|      A non-matching port is skipped before the espflash probe | A wrong port is never opened                            | Probe first, compare afterwards      |
|        A port that reports no VID:PID never matches `usb_ids` | Unknown is not proof                                    | Probe unknown ports as today         |
|              A mismatching `--port` ends the run with `error` | The guard cannot be bypassed by the person it protects  | Warn and trust `--port`              |
| Results carry flat `usb_id` and `usb_serial`, null if unknown | Same format as `bench.toml`, so a pin copies over       | A nested `usb` object                |
|          IDs come from `list_ports.comports()` after the pick | No open, no reset                                       | Parse them from espflash output      |
|                   The filter applies when the board is picked | Firmware may re-enumerate under another PID after flash | Re-check after every reset           |

## Constraints
- `detect` and every existing command keep their behaviour.
- No new reset and no new port open anywhere.
- A board without `usb_ids` behaves exactly as today.
- Every existing `bench.toml`, including `usb_id`/`usb_serial` boards, loads unchanged.
- Readers of `results.jsonl` tolerate entries written before this change, without the new keys.
- `serve` jobs record the fields too, since they run `run --unattended`.
- `mypy --strict` covers tests, ruff line length 100, and no test needs a board.

## Open Questions
- [ ] Does any consumer run an S3 app on USB-OTG/TinyUSB, which re-enumerates under another PID after flash?
- [ ] Which bridges report a usable serial number (native USB-JTAG gives the MAC, CH340 none, CP2102 often `0001`)?
- [ ] Retiring the home-MAC rule in family-resemblance is a rustyfarian-ci change; who tracks it there?
- [ ] Does pyserial on the Pi (`serve`) report VID:PID for every port as on macOS?

## State
- [x] Design approved
- [ ] Core implementation
- [ ] Tests passing
- [ ] Documentation updated (README, CHANGELOG, AGENTS.md schema notes)
- [ ] Records on real boards show `usb_id` on macOS and on the Pi

## Session Log
- 2026-11-14 — Feature doc created via /feature dialog from the family-resemblance B07 request; ADR-002 written
