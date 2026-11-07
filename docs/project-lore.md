# Project Lore

This file records non-obvious technical discoveries: facts that caused surprising
failures, took significant time to debug, or would save a future developer 30+
minutes if known upfront.

Refer to `CLAUDE.md` and the `/project-lore` skill for recording guidelines.

---

## Build System

**ruff ignores `.gitignore` when the project directory is not a git repository.**
`respect-gitignore` only applies inside a git work tree, so scratch `.py` files in the gitignored `tmp/` are linted and fail `just verify`.
Fix: run `git init`, or delete scratch `.py` files from `tmp/` before running the gate.

---

## espflash

**`espflash reset` leaves a native-USB ESP32-S3 in download mode, so the app never boots after a bench flash.**
It enters the bootloader first, and its closing reset on a USB Serial/JTAG port does not return the chip to the app.
Fix: reset on the already-open pyserial port with RTS high while DTR stays low for 0.2 s, as `serial_lines._hard_reset` does.

**`ESPFLASH_SKIP_UPDATE_CHECK=1` makes every espflash call fail; the value must be `true`.**
espflash parses the variable as a clap bool and accepts only `true` or `false`.
Fix: keep `device.ESPFLASH_ENV` at `"true"`.
