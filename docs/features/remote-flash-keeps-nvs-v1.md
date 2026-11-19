---
gate: hardware
gate-note: a provisioned rgb-clock passes a Wi-Fi or MQTT check through `submit` on the Pi, with its NVS intact
desk-work: available
---

# Feature: Remote Flash Keeps NVS v1

Requested by rustyfarian-rgb-clock on 2026-11-14 (first bench, `boot`, `face`, `mqtt-tick`), for remote checks of provisioned firmware.
Today `serve` writes the merged image flat from 0x0, so its 0xFF padding erases NVS and otadata on every job, while a local `flash` writes segments and keeps them.
Two stages: stage 1 keeps NVS and boots the flashed app; stage 2 writes an uploaded NVS image, so a device is provisioned over the bench before its checks.

## Decisions
|                                                        Decision | Reason                                                           | Rejected Alternative                                          |
|----------------------------------------------------------------:|:-----------------------------------------------------------------|:--------------------------------------------------------------|
|   Offsets from the partition table at 0x8000 in the merged image | No new upload, no consumer change for stage 1                    | Manifest `[{offset, file}]` from `image`; offsets in `bench.toml` |
| Write bootloader, table and the `app` ranges; skip `data` partitions | NVS, phy_init and the rest survive; the table is the one source of truth | Skip only `nvs`                                      |
|                          Erase otadata on every segmented flash | The flashed slot boots; a kept pointer may boot a stale OTA slot | Keep otadata, as the request's title asks                     |
|      No table at 0x8000: flat write as today, recorded as `flat` | Existing consumers are unaffected                                | Refuse an image without a table                               |
| Job record names the mode (`flat`, `segments`) and the ranges written | A wiped-NVS failure is told apart afterwards                | Log lines only                                                |
| Stage 2: an optional base64 `nvs` field, written at the table's `nvs` offset | Smallest API change; the server already knows the offset | A generic segment upload                                   |
|                                              One doc, two stages | One design serves both; stage 2 waits for stage 1                | Two feature docs                                              |

## Constraints
- The `serve` API stays backwards compatible: stage 2 adds one optional field, nothing else changes.
- The TOML schema is unchanged.
- A merged image without a table at 0x8000 flashes flat, as today.
- The 16 MiB image limit holds; the NVS image counts against the same request budget.
- Local `run` and the consumer's `flash` command are untouched.
- Only `data` partitions are skipped or erased; every `app` range the image covers is written.
- The NVS image is never logged and is deleted once the job finished; the record keeps its size and offset.
- espflash on the Pi stays the pinned binary (4.6.0 in `roles/tools`); segment writes and the otadata erase use it.

## Open Questions
- [ ] How the NVS image reaches `submit`: `--nvs PATH` only, or a `[commands] nvs` entry like `image`, which touches the TOML schema.
- [ ] ESP32 classic puts the bootloader at 0x1000: confirm the merged image still starts at 0x0 and the table parse holds for every chip in `CHIPS`.
- [ ] espflash 4.6.0: several `write-bin` calls per job and `erase-region` for otadata; measure the added flash time.
- [ ] Trim trailing 0xFF from the app segment to shorten the write, or write the full partition range.

## State
- [x] Design approved
- [ ] Stage 1: `serve` splits the merged image by its partition table, erases otadata, records the mode
- [ ] Stage 2: `submit` uploads an NVS image, `serve` writes it at the `nvs` offset
- [ ] Tests passing (table parser on fixture images, flat fallback, record fields, NVS handling)
- [ ] Documentation updated (README remote bench, CHANGELOG, `serve` API note)
- [ ] rgb-clock: a provisioned device passes a Wi-Fi or MQTT check through `submit` on the Pi

## Session Log
- 2026-11-20 — Feature doc created via /feature dialog from the rgb-clock request; otadata reset and the in-image partition table chosen over the request's manifest
