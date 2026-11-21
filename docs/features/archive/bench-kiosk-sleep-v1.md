---
gate: none
desk-work: available
---

# Feature: Bench Kiosk Sleep v1

The panel goes dark 15 minutes after the last job, and a touch or a new job wakes it.
v1 draws a black frame; switching the backlight itself is `bench-kiosk-backlight-v1.md`.

## Decisions
|                                          Decision | Reason                                                              | Rejected Alternative                       |
|--------------------------------------------------:|:--------------------------------------------------------------------|:-------------------------------------------|
|                 Dark means a black frame from `kiosk` | The Waveshare (A) backlight has no software switch                  | Waiting for the backlight hardware         |
|        Sleep and wake go through one `Screen` seam | The backlight switch plugs in later without touching the timer     | Black frame hard-wired in the loop         |
|   Idle clock is monotonic, restarted when the status record changes | The Pi has no RTC; the file only says that something changed | Wall-clock finish times from the status file |
|   The clock starts at the first tick after the framebuffer is found | `run()` may wait minutes for the panel            | Process start                              |
|           A queued or running job keeps it awake | A job on the panel is never hidden                                 | Sleeping during long jobs                  |
|   A stale or stopped `serve` wakes the panel once | The outage is seen, then the panel sleeps again               | Lit for the whole outage, sleeping through it |
|           A touch wakes for another 15 minutes | One timeout to explain and configure                               | A short 1-minute peek after a touch        |
|      A touch while awake restarts the clock, no redraw | Nothing on the panel changed                            | Redrawing on every touch                   |
|                     A waking touch only wakes | Later touch actions must not fire on a dark panel                  | Passing the first touch through            |
|  Pure `track()` and `asleep(idle, busy, mono, after)` | Testable in `verify` like `layout(status, now)`, in `kiosk_sleep` | Timer state spread across the loop |
|   Touch read as raw `input_event` with `struct` | Taps only, not coordinates; no new dependency                       | `python-evdev` (C extension, new package)  |
|   A touch is `BTN_TOUCH` value 1 or `SYN_DROPPED` | Pressure noise must not keep it lit; an overflow may drop the press | Any event                             |
|          Touch device found by name in sysfs | Its event number depends on probe order, like the framebuffer      | A hard-wired `/dev/input/event0`           |
|  A udev rule with `GROUP:=`/`MODE:=` hands only the touch node to the display user | `99-com.rules` runs later and would set group `input` | Group `input` for the display user |
|  No usable touch device: never dark, one warning | Nobody could wake it; rescan every 5 s, the clock restarts on return | Sleeping on the timer alone      |
|          Touch read non-blocking inside the 1 s poll | Keeps the loop shape; a wake takes at most 1 s               | Reworking `wait()` around `select`         |
|  `kiosk --sleep-after SECONDS`, 0 turns sleep off | Smoke tests and benches without touch can opt out           | A fixed 15 minutes                         |
|               `--once` never sleeps; the farewell is always drawn | A black panel at poweroff would look asleep    | Leaving the black frame on stop            |
|  `display_sleep_after: 900`, `display_touch_name: "ADS7846 Touchscreen"` | One name feeds the udev rule and `BENCH_TOUCH_MATCH` | Two variables for one device |

## Constraints
- The TOML schema and the CLI contract stay unchanged; `--sleep-after` is additive.
- Status schema 1, `status.py` and `server.py` do not change.
- No new runtime dependency beyond the `[kiosk]` extra.
- The display unit keeps its hardening; `DeviceAllow=char-input r` admits every input device, so only the udev rule (`0640`) narrows reads to the touch node.
- A touch never does anything but wake and reset the timer.
- `just display-smoke` covers sleep and wake; `just verify` stays green.
- A second `just pi-provision` ends at `changed=0`.
- No SSH from the controller; Pi checks are scripts the user runs and pastes back.

## Open Questions
- [x] Pi: `ADS7846 Touchscreen` is `event5` at boot; untouched 0 events, taps 9 `BTN_TOUCH` 1 with the GPIO 17 IRQ rising.
- [x] Pi: `99-com.rules` sets `GROUP="input", MODE="0660"` after us; with `:=` the node is `640 root:bench-display`.
- [x] A stale or stopped `serve` wakes the panel once, then it sleeps again.
- [x] Ansible variables: `display_sleep_after: 900` and `display_touch_name: "ADS7846 Touchscreen"`.

## State
- [x] Design approved
- [x] Pi: touch device present and its events seen (probe script)
- [x] `kiosk_sleep`, `Screen` seam and touch reader with tests
- [x] Display role: udev rule for the touch device, unit access, `--sleep-after`
- [x] `just display-smoke` renders asleep and woken
- [x] Pi: dark after 15 idle minutes, a touch wakes it (seen by the user)
- [x] Pi: second `just pi-provision-dev` run `ok=65 changed=0`
- [x] Pi: after a reboot `event5` is still `crw-r----- root bench-display`
- [x] Pi: a job wakes the dark panel (`esp32` `hal_esp32_connect_async`, seen by the user)
- [x] Documentation updated: `deploy/README.md`, CHANGELOG, roadmap
- [x] Lore "Bench Display": `99-com` override and `DeviceAllow` (`SYN_DROPPED` never seen, not recorded)

## Session Log
- 2026-11-21 — Feature doc created via /feature dialog; black frame now, backlight split into `bench-kiosk-backlight-v1.md`
- 2026-11-21 — Implemented on `bench-kiosk`: monotonic idle clock, touch filter, fail-open, farewell when dark; laptop gates green, Pi probe pending
- 2026-11-21 — Pi on v0.8.0.dev1: `touch-probe.sh` passed (event5, `:=` needed, no ghost events); 15-minute sleep and `changed=0` still to see
- 2026-11-21 — Pi: the panel went dark and a touch woke it (seen by the user); job wake, `changed=0` and reboot still open
- 2026-11-21 — Pi: second provision run `ok=65 changed=0`; job wake and reboot still open
- 2026-11-21 — Pi: touch node `0640 root:bench-display` after a reboot; only the job wake is still open
- 2026-11-21 — Pi: a submitted job woke the dark panel; all acceptance checks done
