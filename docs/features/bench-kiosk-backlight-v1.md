---
gate: hardware
gate-note: The board revision on the panel's back and a GPIO 22 probe decide which backlight switch exists.
desk-work: available
---

# Feature: Bench Kiosk Backlight v1

When the kiosk sleeps, the panel's backlight turns off, so the bench gives off no light at night.
It builds on the `Screen` seam from `bench-kiosk-sleep-v1.md`, which draws a black frame until this lands.

## Decisions
|                                      Decision | Reason                                                                  | Rejected Alternative                    |
|----------------------------------------------:|:------------------------------------------------------------------------|:----------------------------------------|
|           Switch the backlight, never the module | Module power also feeds the touch controller, so a touch could not wake | A GPIO or relay on the panel's supply   |
|  Try GPIO 22, then the Rev C jumper, then a transistor | Cheapest first; solder only when software and jumper fail         | Going straight to the transistor change |
|       Kernel owns the pin via a `gpio-backlight` node | `kiosk` writes `bl_power` in sysfs; no GPIO chip access in the unit | `libgpiod` or `pinctrl` from `kiosk`    |
|        `display_backlight_gpio`, unset by default | Benches without the switch keep the black frame from sleep v1          | Requiring the switch on every bench     |
|                         On/off only, no dimming | Sleep needs dark and bright, nothing in between                      | PWM brightness levels                   |

## Constraints
- No change to the panel that stops touch, the DRM driver or the 16 MHz SPI link from working.
- The display unit gets write access to that one backlight's `bl_power` only, granted by a udev rule.
- The backlight is on whenever `kiosk` is not running, so a crashed kiosk never leaves a dark panel.
- A hardware change is documented with a photo or a wiring note in `deploy/README.md`.
- No SSH from the controller; Pi checks are scripts the user runs and pastes back.

## Open Questions
- [ ] Which revision is the bench panel, and does its back carry a backlight/PWM solder jumper?
- [ ] Does GPIO 22 switch the backlight with the DRM variant (`pinctrl set 22 op dl`, then `dh`)?
- [ ] If Rev C: which GPIO does the jumper connect, and is it free of SPI and touch?
- [ ] If a transistor change: which spare GPIO, which transistor, and where does it tap the 3.3 V line?
- [ ] Does the DRM `ili9486` driver pick up a `backlight` phandle and blank on its own?

## State
- [ ] Design approved
- [ ] Panel back inspected: revision and jumper recorded
- [ ] `deploy/backlight-probe.sh`: GPIO 22 off/on with a saved log, run on the Pi
- [ ] Switch chosen and, if needed, the hardware change made
- [ ] Display role: overlay or `gpio-backlight` node, udev rule, `display_backlight_gpio`
- [ ] `kiosk`: `Screen` turns the backlight off on sleep and on before the first woken frame
- [ ] Pi: dark at night with the backlight off, touch still wakes, second run `changed=0`
- [ ] Documentation updated: `deploy/README.md`, CHANGELOG, roadmap

## Session Log
- 2026-11-21 — Feature doc created via /feature dialog; split from sleep v1, gated on the panel's backlight wiring
