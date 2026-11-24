---
gate: none
desk-work: available
---

# Feature: Remote Board List v1

A consumer reads the identity of the boards on the remote bench without SSH, before it flashes one.
rustyfarian-rgb-clock needs a board's base MAC to enrol its DDI controller id at rustbox-fleet before the first boot.
Source: `review-queue/rustyfarian-bench-remote-board-list.md`; first step of roadmap "Host-side board pinning".

## Decisions
| Decision | Reason | Rejected Alternative |
|---:|:---|:---|
| `GET /boards` lists every attached serial port | Port, by-id path, VID:PID and USB serial come from `comports()` | `espflash board-info` on every GET |
| MAC, chip and name only when verified, with source and time | Boards are replugged; a stale MAC names the wrong board | Trust a cache keyed on the USB serial |
| Verified: a native-USB serial, or a probe with an unchanged fingerprint | The native USB-JTAG serial is the MAC; bridges' serials are not unique | A probe on every list |
| Fingerprint `(st_dev, st_ino, usb_serial, vid:pid, location)`, no ctime | A replug makes a new node; udev's chmod and ACLs move ctime without one | ctime in the fingerprint |
| Probe results cached in memory only | A restart forgets them, the safe direction after a board swap | A cache file in the outbox |
| `POST /boards/probe` answers 202; the client polls `GET /boards/probe/{id}` | A probe can queue behind a 600 s job, one port per worker task | A blocking POST |
| One queued probe per port; 30 s cooldown after it | A request flood never resets a board twice | Refuse while a probe is queued |
| A probed port must be in the current listing, else 422 | No `-` word or arbitrary path reaches espflash's argv | Any path under `/dev` |
| The probe answer says the board was reset | `board-info` resets the chip; a running app restarts | Silent reset |
| The kiosk shows idle while a probe runs | A probe is not a job and never enters the verdict history | A probe screen on the panel |
| `serve --parts <file>` names boards by base MAC, re-read on every list | Host owns its boards; the same TOML prints the box labels | The client's `bench.toml` keys |
| `parts.toml`: `[[parts]]` `name`, `mac`, `barcode`; bench name rule | The label printer's format; `barcode` is ignored by the bench | YAML (needs a parser dependency) |
| Without `--parts`, the list carries no names | `serve` keeps working on hosts without an inventory | Refuse to start |
| Client `boards [--probe]`, `just boards` and `just pi-boards` | Same URL, token and CA handling as `submit` | A raw `curl` recipe |
| `pi-provision` deploys the gitignored `deploy/ansible/parts.toml` | The bench owns the catalogue; a public repo publishes no MACs | `shared-secrets/`, a committed file |
| Any token holder sees MACs and USB serials | A MAC is an identity, not a secret; a token already flashes any board | A separate scope for the list |

## Constraints
- No change to `POST /jobs`, `GET /jobs/{id}` or the job directory format.
- A probe never opens a port while a job holds it.
- `client.py` stays stdlib-only; `remote.py` stays free of web-framework imports.
- `create_app()` without the new options behaves exactly as today.
- `mypy --strict` covers tests, ruff line length 100, and no test needs a board.

## Out of Scope
- MACs cached from job runs, an `absent` list and `--no-probe` (v2).
- A board list on the kiosk.
- Board-by-name jobs (roadmap "Host-side board pinning" v2).

## Open Questions
- [ ] Does a probe through `serve` leave a native-USB S3 running, not in download mode?
- [x] Does a replug change the device node's inode on the Pi, so a swap between two lists is caught? Yes (CH340).
- [x] Does pyserial on the Pi report VID:PID and serial for a bridge? Yes for a CH340 (`1a86:7523`), with no serial.
- [ ] Does an S3's native USB serial equal the MAC its probe reads?
- [ ] Does `board-info` re-enumerate a native-USB node, so every probe reports "port changed during the probe"?

## State
- [x] Design approved
- [x] Core implementation
- [x] Tests passing
- [x] Documentation updated (README, CHANGELOG, AGENTS.md, deploy README)
- [x] `just pi-boards --probe` names the attached bridge board from `parts.toml`
- [x] A replug on the Pi marks that port unverified

## Session Log
- 2026-11-23 — Feature doc created via /feature dialog from the rgb-clock request
- 2026-11-23 — Plan decisions recorded (async probe, dedupe and cooldown, allowlist, `deploy/ansible/parts.toml`); core, role and docs written
- 2026-11-23 — v0.9.0.dev1 on the Pi: `just pi-boards --probe` named `bench-esp32c3-mini1` (CH340) from `parts.toml`
- 2026-11-23 — Gate cleared: a replug on the Pi made the probed C3 unverified; the native-USB S3 questions stay open
- 2026-11-24 — Closed; the native-USB S3 questions moved to roadmap "Native-USB S3 on the bench"
