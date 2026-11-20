---
gate: hardware
gate-note: the bench Pi 5 shows the banner after `just pi-provision` and a reboot
desk-work: available
---

# Feature: Bench Kiosk v1

The Waveshare 3.5" panel on the bench Pi 5 shows "rustyfarian bench" and the installed version instead of a white screen.
v1 is first light for the roadmap's bench kiosk; the job view is v2 and reuses the renderer.

## Decisions
|                                                 Decision | Reason                                                                | Rejected Alternative                           |
|---------------------------------------------------------:|:----------------------------------------------------------------------|:-----------------------------------------------|
|           Stock `piscreen` overlay, fbtft variant, 16 MHz | Ships with the Pi 5 kernel; the probe drove the panel with it         | Its `drm` variant, Waveshare's dtbo, LCD-show   |
|              `drm` variant left to v2, with a modeset | A raw framebuffer write runs no modeset, so the panel stayed white     | Doing the modeset ioctl in the v1 script       |
|                 Pillow script draws into the framebuffer | ~60 lines and two apt packages; the Lite image stays Lite             | Chromium in a Wayland kiosk, console text      |
|                  Script and unit in a new `display` role | v1 needs no bench code; v2 decides on a package subcommand            | A `[kiosk]` extra of rustyfarian-bench         |
|          Own system user in group `video`, hardened unit | Needs the framebuffer only, never the outbox or the secrets           | Running as `bench` or root                     |
|            Version from `uv tool list` after the install | `bench_version` lies when the install was refused under jobs          | Templating `bench_version`, a `--version` flag |
| Version in a root-owned file, unit restarted by a handler | The service needs neither uv nor `/opt`                              | The service calling uv at boot                 |
|                Framebuffer found by driver name in sysfs | Its index depends on probe order next to the HDMI framebuffer         | A hard-wired `/dev/fb1`                        |
|                 Rotation is a role variable, default 180 | The probe's image at rotate 0 was upside down in the case             | A fixed rotation                               |
|                         Reboot reported, never performed | One policy for the playbook; a reboot ends a running job              | A guarded or flagged reboot                    |
|               No `hdmi_*` lines from the Waveshare guide | They serve fbcp mirroring; the banner is drawn on the panel           | Copying the wiki's config.txt block            |

## Constraints
- A second `just pi-provision` is `changed=0`: config.txt block, packages, unit and version file are idempotent.
- No package that pulls in brltty, ModemManager or a desktop.
- The role touches nothing a job uses: no serial port, no serve restart, no reboot.
- Nothing secret on the panel or in the role.

## Open Questions
- [x] Which panel: the probe drove it with the stock `piscreen` overlay, colours right, so an (A) or compatible.
- [x] Which variant: fbtft showed the bars, `drm` stayed white (framebuffer present, no modeset).
- [x] Rotation: the probe at rotate 0 was upside down on the fbtft variant, so `rotate=180`.
- [ ] SPI speed: 16 MHz worked in the probe; try higher only if the v2 refresh needs it.
- [ ] A third line with the bench name and address, or banner and version only?
- [ ] mypy scope for the script: add it with Pillow as a dev dependency, or ruff only?
- [ ] ADR-004 for the display stack, or is the decisions table enough?

## State
- [x] Design approved
- [ ] `display` role: config.txt block, packages, user, script, unit, version file; wired into `site.yml`
- [ ] `just deploy-lint` and `just verify` green
- [ ] Pi: `just pi-provision`, reboot, banner shows the installed version; second run `changed=0`
- [ ] Documentation updated: `deploy/README.md` hardware and "What comes next", CHANGELOG, roadmap

## Session Log
- 2026-11-20 — Feature doc created via /feature dialog; stock `piscreen` overlay and Pillow renderer chosen, Chromium kiosk deferred to v2; `deploy/display-probe.sh` run: fbtft `piscreen` drives the panel (upside down at rotate 0), `drm` stays white
