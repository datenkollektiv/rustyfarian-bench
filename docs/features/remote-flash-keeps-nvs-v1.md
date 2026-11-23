---
gate: hardware
gate-note: stage 1 cleared by the Pi's NVS marker check; stage 2 needs an uploaded NVS image to provision a board through `submit` on the Pi
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
|       A malformed table (magic present) is refused with 422 | A parser bug cannot fall back to a flat write that wipes NVS     | Fall back to flat                                             |
| The writer is `python -m rustyfarian_bench.flash_image`, the job's one flash argv | `POST /flash` reuses it; the plan is re-derived from the image | A hidden CLI subcommand; a plan file written at submit |
|          One write of `0x0`–`0x9000` covers bootloader and table | File offset is flash address on every chip; the gap is unused    | A write per chip's bootloader address                         |
| An `app` range drops trailing `0xFF`, keeps 64 bytes past the last other byte, rounds up to 4 KiB; an empty slot is skipped | The app's checksum and SHA-256 digest may end in `0xFF`; bytes past the app end are never read | Write the full partition; parse the app header |
|           Every espflash call but the last uses `--after no-reset` | No half-written app boots between calls; the last resets as today | A hard reset after every call                                |

## Constraints
- The `serve` API stays backwards compatible: stage 2 adds one optional field, nothing else changes.
- The TOML schema is unchanged.
- A merged image without a table at 0x8000 flashes flat, as today.
- The 16 MiB image limit holds; the NVS image counts against the same request budget.
- Local `run` and the consumer's `flash` command are untouched.
- Only `data` partitions are skipped or erased; every `app` range the image covers is written.
- The NVS image is never logged and is deleted once the job finished; the record keeps its size and offset.
- espflash on the Pi stays the pinned binary (4.6.0 in `roles/tools`); segment writes and the otadata erase use it.
- With otadata erased, the bootloader boots `factory`, else `ota_0`; an image that holds `factory` and `ota_0` but carries its app only in `ota_0` boots a stale `factory` (espflash fills the first app slot, so its images never do).
- Typed `bootloader`/`partition_table` entries (newer ESP-IDF) may sit below `0x9000`; the first write covers them.

## Open Questions
- [ ] How the NVS image reaches `submit`: `--nvs PATH` only, or a `[commands] nvs` entry like `image`, which touches the TOML schema.
- [x] ESP32 classic puts the bootloader at 0x1000: espflash 4.6.0's `save-image --merge` pads with `0xFF` from file offset 0, so file offset is flash address on every chip (bootloader `0x1000` on esp32/s2, `0x2000` on c5/p4, `0x0` elsewhere) and the table is at `0x8000`.
- [x] espflash 4.6.0: `write-bin ADDR FILE` per range and `erase-region ADDR SIZE` for otadata (both 4 KiB aligned); on the Pi's C3 (CH340) the split write took 39 s against 37 s flat for the same app, about 2–3 s per extra call.
- [x] Trim trailing 0xFF: yes, with a 64-byte tail kept, rounded up to 4 KiB and clamped to the partition; an all-`0xFF` slot (empty `ota_1`) is not written.

## State
- [x] Design approved
- [x] Stage 1: `serve` splits the merged image by its partition table, erases otadata, records the mode
- [x] Stage 1 on the Pi: a split write boots and passes (`hal_c3_connect_async` 3/3 on the C3, v0.9.0.dev2)
- [x] Stage 1 on the Pi: a random marker in NVS (0x9000–0xf000) is byte-identical after a `pi-submit` job (`tmp/nvs-keep-check-pi.sh`)
- [ ] Stage 2: `submit` uploads an NVS image, `serve` writes it at the `nvs` offset
- [ ] Tests passing (table parser on fixture images, flat fallback, record fields, NVS handling) — stage 1 parts pass
- [ ] Documentation updated (README remote bench, CHANGELOG, `serve` API note) — stage 1 done
- [ ] Stage 2 on the Pi: an uploaded NVS image provisions a board through `submit`

## Session Log
- 2026-11-20 — Feature doc created via /feature dialog from the rgb-clock request; otadata reset and the in-image partition table chosen over the request's manifest
- 2026-11-23 — Stage 1 implemented on branch `remote-flash-keeps-nvs` (off `remote-board-list`): `flash_image.py` writer, `flash` job field, 422 for a malformed table; Pi gate open
- 2026-11-24 — v0.9.0.dev2 on the Pi: job 20261123-233026-583403 wrote 0x0+0x9000 and 0x10000 (`--after no-reset` on the first), booted from 0x10000 and passed 3/3; a random NVS marker written before a second job read back identical after it ("NVS KEPT"); the marker check replaces the rgb-clock gate for stage 1 (rgb-clock has no bench onboarding yet)
