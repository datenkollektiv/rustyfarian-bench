---
gate: none
desk-work: none
---

# Feature: Bench Kiosk v1

The Waveshare 3.5" panel on the bench Pi 5 shows "rustyfarian bench" and the installed version instead of a white screen.
v1 is first light for the roadmap's bench kiosk; the job view is v2 and reuses the renderer.

## Decisions
|                                                 Decision | Reason                                                                | Rejected Alternative                           |
|---------------------------------------------------------:|:----------------------------------------------------------------------|:-----------------------------------------------|
|            Stock `piscreen` overlay, `drm` variant, 16 MHz | Mainline driver written for this panel; the probe drove it with both  | fbtft variant (staging), Waveshare's dtbo, LCD-show |
|                     Variant switchable by `display_drm` | Both proven on the bench Pi; a flip is one variable, no code change   | Hard-wiring one driver                         |
|                 Pillow script draws into the framebuffer | ~60 lines and two apt packages; the Lite image stays Lite             | Chromium in a Wayland kiosk, console text      |
|                  Script and unit in a new `display` role | v1 needs no bench code; v2 decides on a package subcommand            | A `[kiosk]` extra of rustyfarian-bench         |
|          Own system user in group `video`, hardened unit | Needs the framebuffer only, never the outbox or the secrets           | Running as `bench` or root                     |
|          Version is `bench_version`, carried in the unit | After the bench role ran it is the installed one, or the play stopped | `uv tool list` re-read, a version file         |
|         A udev rule releases the console from the panel | Headless, the panel is fb0; fbcon shows the login and blanks it later | `ExecStartPre=+`, `fbcon=map:` on the cmdline  |
|                Framebuffer found by driver name in sysfs | Its index depends on probe order next to the HDMI framebuffer         | A hard-wired `/dev/fb1`                        |
|          Rotation per driver: DRM 180, fbtft 90 | Each default was upside down; DRM counts from 0, fbtft from 270       | A fixed rotation                               |
|                         Reboot reported, never performed | One policy for the playbook; a reboot ends a running job              | A guarded or flagged reboot                    |
|               No `hdmi_*` lines from the Waveshare guide | They serve fbcp mirroring; the banner is drawn on the panel           | Copying the wiki's config.txt block            |

## Constraints
- A second `just pi-provision` is `changed=0`: config.txt block, packages, rule, unit and service are idempotent.
- No package that pulls in brltty, ModemManager or a desktop.
- The role touches nothing a job uses: no serial port, no serve restart, no reboot.
- Nothing secret on the panel or in the role.

## Open Questions
- [x] Which panel: the probe drove it with the stock `piscreen` overlay, colours right, so an (A) or compatible.
- [x] Which variant: both showed the same reversed bars; "drm stays white" was the probe misreading a sentence.
- [x] Rotation: DRM at 90 stood the banner on its side (portrait at 90 and 270); 180 for DRM, 90 for fbtft.
- [ ] Farewell at poweroff: the DRM driver's shutdown may blank the panel after "good-bye"; check on the Pi which one stays.
- [ ] SPI speed: 16 MHz worked in the probe; try higher only if the v2 refresh needs it.
- [x] Banner and version only in v1; a line with the bench name and address can come with the job view.
- [x] mypy scope: ruff check and format only; no Pillow dev dependency.
- [x] No ADR; this table carries the reasoning.

## State
- [x] Design approved
- [x] `display` role: config.txt block, packages, user, udev rule, script, unit; wired into `site.yml`
- [x] `just deploy-lint` and `just verify` green; `just display-smoke` renders the banner locally
- [x] Pi: `just pi-provision`, reboot, banner shows the installed version; second run `changed=0`
- [x] Documentation updated: `deploy/README.md`, CHANGELOG (roadmap after the Pi run)

## Session Log
- 2026-11-20 — Feature doc created via /feature dialog; stock `piscreen` overlay and Pillow renderer chosen, Chromium kiosk deferred to v2; `deploy/display-probe.sh` run: both `piscreen` variants drive the panel, upside down at the overlay's default 270 (so rotate=90); `display` role implemented, lint and smoke render green; first Pi run: banner readable, DRM rotation corrected from 90 to 180; second run `changed=0`, gate closed
- 2026-11-20 — Farewell: a stop (shutdown, reboot, restart) draws "good-bye" over the banner; `just display-smoke` sends SIGTERM and checks the frame changed; not yet seen on the Pi
