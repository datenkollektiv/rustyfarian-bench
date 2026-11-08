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

---

## Serial Monitoring

**After a reset, old-boot output can survive in three places: the line queue, the reader's partial line, and the serial driver's input buffer.**
`drain()` and `buf = b""` clear only what the reader has already taken, while a USB-serial bridge (CH340, FT232) keeps unread bytes across the reset; a native-USB chip loses them when the port re-enumerates.
Read after the pulse, stale bytes pass for the new boot and can satisfy an `expect`.
Fix: clear all three, calling `reset_input_buffer()` right before the pulse, as `SerialLines._pulse` does.

**A shared "reset done" `Event` lets a pulse that was already running answer a reset requested after it started.**
The startup pulse (or one on reconnect) sets the event, and a step's `reset()` that cleared it a moment earlier returns as if its own reset had happened.
Fix: number the requests and serve only those a pulse claimed before it began; claim and withdraw under one lock, so a timed-out request is never pulsed late and a claimed one is never reported as failed.

---

## Regex Patterns

**Finding `(?P=name)` by text search misreads regex syntax: inside `[...]`, after `\`, or in a verbose comment it is literal text.**
Verbose mode can come from any leading global flag group (`(?i)(?x)` is valid) or a scoped `(?x:...)` that `(?-x:...)` switches off again.
Python accepts global flag groups only at the very start of a pattern and rejects them anywhere else.
Fix: scan with `config._scan`, which tracks escapes, classes, comments and the verbose state per group.
