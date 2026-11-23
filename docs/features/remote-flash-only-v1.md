---
gate: none
gate-note: "Remote flash keeps NVS" stage 1 landed (2026-11-24), so a flash-only job writes segments through `flash_image` and keeps NVS
desk-work: available
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
- [ ] Which line counts as "app up" for both esp-idf and esp-hal images, given lore's startup-reset trap?
- [ ] ESP32 classic: is the chip id readable from a merged image whose bootloader sits at 0x1000?
- [ ] Does a flash job append a row to `results.jsonl`, or record only its job result?
- [ ] Does a native-USB S3 boot the app after the flash, or stay in download mode?

## State
- [x] Design approved
- [ ] Core implementation (`POST /flash`, flash child, client `flash`, recipes)
- [ ] Tests passing (no board needed: argv, port allowlist, part resolution, boot match, timeout)
- [ ] Documentation updated (README remote bench, CHANGELOG, `serve` API note, AGENTS.md)
- [ ] `just pi-flash --part bench-esp32c3-mini1 IMAGE` boots the snapshot on the Pi, NVS intact

## Session Log
- 2026-11-23 — Feature doc created via /feature dialog: replace the SSH copy-and-flash workaround
