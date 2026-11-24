---
gate: hardware
gate-note: `just pi-flash --part bench-esp32c3-mini1 IMAGE` boots the snapshot on the Pi with NVS intact
desk-work: none
---

# Feature: Remote Flash Only v1

A consumer flashes a software snapshot onto one board attached to the remote bench, without a check.
Today every `POST /jobs` needs a check, an example and a scenario, so a plain flash means `scp` to the Pi and espflash over SSH.
That workaround bypasses the job queue, the port lock and the kiosk.

## Decisions
| Decision | Reason | Rejected Alternative |
|---:|:---|:---|
| Job passes once the boot banner is seen after flash and reset | Catches a dead image without a scenario | Pass on espflash exit 0; a timed log window |
| The serial log stays with the job, like a check's `run.log` | The snapshot's boot is readable afterwards | Discard serial output |
| Target by `--port` or `--part <parts.toml name>` | No consumer repo needed; the names come from `just pi-boards` | The consumer's `bench.toml` board key |
| A `--part` name resolves to a port through the verified board list | A replugged, unverified board is never flashed by name | Trust a cached name-to-port map |
| Chip from the image header; espflash checks it against the board | No flag for the common case | A mandatory `--chip` |
| Defaults: 115200 baud, RTS reset, boot = first app line after the bootloader | Matches the bench boards today | Board facts in `parts.toml` |
| `--baud`, `--reset` and `--expect REGEX` override the defaults | Odd boards and custom banners still work | No overrides |
| Lands after NVS stage 1 and uses its segmented writer | A provisioned device keeps its NVS from day one | Ship first with the flat write |
| New `POST /flash`; same queue, outbox and `GET /jobs/{id}` | `SubmitRequest` stays unchanged | A `kind` field on `POST /jobs` |
| Client `rustyfarian-bench flash`, `just flash` and `just pi-flash` | Same URL, token and CA handling as `submit` and `boards` | `submit --flash-only`, which needs a repo and an example |
| The kiosk shows a flash job like any other job | The panel tells who holds the board | Hide it like a probe |
| A flash job is a generated one-step scenario run by the unchanged `run` child with `--port` | Reuses detection, the bug 002 settle, `results.jsonl` and the kiosk's step follower | A separate flash-and-monitor child |
| App up = the first line that is neither ROM nor 2nd-stage bootloader output (`remote.APP_UP`) | Covers esp-idf (`cpu_start:`) and esp-hal (`INFO - …`) without a per-framework banner | One banner regex per framework |
| The step has `arm = false`, with a crash `reject` for 2 s after the match | Arming drains the backlog that holds the banner; a crash loop must not pass | Arm like a check step; pass on the match alone |
| A verified MAC on the target port is pinned in the job's `bench.toml` | A board swapped while the job waits fails detection | Record a fingerprint in the job |
| A flash job needs a merged image with a partition table | A bare app image carries the same header at 0x0, and a flat write would replace the bootloader | Flash a bare app image flat |
| App up never matches a crash or a ROM boot-failure line | An `expect` match wins over `reject` on the same line, and a blank-flash board must not pass | Rely on `reject` alone |

## Constraints
- `POST /jobs`, `GET /jobs/{id}` and the TOML schema stay backwards compatible.
- A flash job holds the port through the same worker as checks and probes.
- A `--port` must be in the current `GET /boards` listing, else 422, as for probes.
- The 16 MiB image limit and the request body budget hold.
- `client.py` stays stdlib-only; `remote.py` stays free of web-framework imports.
- Local `run` and the consumer's `[commands] flash` are untouched.

## Out of Scope
- A local flash-only command; the consumer's own `flash` covers that.
- Flashing several boards in one job.
- Uploading an NVS image; that is NVS stage 2.

## Open Questions
- [x] Which line counts as "app up"? The first line with text that is not a crash, ROM or bootloader output. It was checked against a real C3 esp-hal log, whose first match is `INFO - [app] start …`. `arm = false` keeps the banner, and the ROM lines of the startup reset never match.
- [x] ESP32 classic: is the chip id readable from a merged image? Yes. `chip_of` tries 0x0, 0x1000 and 0x2000, and accepts a header only at its family's offset.
- [x] Does a flash job append a row to `results.jsonl`? Yes, in the job's own `bench/<key>/`. `_run_job` takes its verdict from that row.
- [ ] Does a native-USB S3 boot the app after the flash, or stay in download mode?

## State
- [x] Design approved
- [x] Core implementation (`POST /flash`, flash child, client `flash`, recipes)
- [x] Tests passing (no board needed: argv, port allowlist, part resolution, boot match, timeout)
- [x] Documentation updated (README remote bench, CHANGELOG, `serve` API note, AGENTS.md)
- [ ] `just pi-flash --part bench-esp32c3-mini1 IMAGE` boots the snapshot on the Pi, NVS intact

## Session Log
- 2026-11-23 — Feature doc created via /feature dialog: replace the SSH copy-and-flash workaround
- 2026-11-24 — Implemented in `83c03eb`; open questions settled except the native-USB S3; Pi gate open
