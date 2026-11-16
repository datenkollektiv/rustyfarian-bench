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

**A merged image built from a bare ELF does not boot on the rustyfarian consumers.**
Their `flash.sh` passes a cached IDF v5.3.3 `--bootloader` and `--ignore-app-descriptor`, while espflash 4.x bundles a v5.5.1 bootloader that fails with "efuse blk rev".
Fix: let the consumer build the remote image through its own `image` command (`espflash save-image --merge` with the same arguments), never from an ELF path alone.

---

## Serial Monitoring

**After a reset, old-boot output can survive in three places: the line queue, the reader's partial line, and the serial driver's input buffer.**
`drain()` and `buf = b""` clear only what the reader has already taken, while a USB-serial bridge (CH340, FT232) keeps unread bytes across the reset.
A native-USB chip loses them when the port re-enumerates.
Read after the pulse, stale bytes pass for the new boot and can satisfy an `expect`.
Fix: clear all three, calling `reset_input_buffer()` right before the pulse, as `SerialLines._pulse` does.

**A shared "reset done" `Event` lets a pulse that was already running answer a reset requested after it started.**
The startup pulse (or one on reconnect) sets the event, and a step's `reset()` that cleared it a moment earlier returns as if its own reset had happened.
Fix: number the requests and serve only those a pulse claimed before it began.
Claim and withdraw under one lock, so a timed-out request is never pulsed late and a claimed one is never reported as failed.

**Every open of a USB Serial/JTAG port rebooted the chip, even with `dtr = rts = False` set before `open()`.**
macOS asserts DTR and RTS on open, and pyserial then applies DTR before RTS, so asking for both low passes through RTS high with DTR low, the reset condition.
A reopen after a deep-sleep wake therefore cold-booted the board and discarded its retained state; every log also started with two ROM banners.
Fix: set `dtr = True, rts = False` before open and drop DTR after it (ignoring `EINVAL`/`ENOTTY` like pyserial).
Measured on an ESP32-S3 with pyserial 3.5 on macOS: 4/4 opens reset before, 0/5 after (2026-11-09).
This holds only for the ESP's RTS/DTR interlock: on a DTR auto-reset board (Arduino-style capacitor) raising DTR is the reset, so `reset = "dtr"` boards keep DTR low on open.
pyserial does not promise glitch-free modem lines during open, and macOS asserts DTR itself, so a DTR auto-reset board can still reboot when the port opens.

---

## Host Probes

**`os.killpg(pgid, 0)` raises `PermissionError`, not `ProcessLookupError`, on macOS once a process group holds only zombies.**
A probe's shell dies on SIGTERM while its unreaped child is still a group member, so the existence check hits EPERM and the teardown crashed with `Operation not permitted`.
Linux answers ESRCH only after every member is reaped.
Fix: treat `PermissionError` like `ProcessLookupError` in every `killpg` call of `ProbeLines._end_group` and `_group_gone`.
Always signal the group even when the leader has already exited, since children keep the pgid alive.

## Regex Patterns

**Finding `(?P=name)` by text search misreads regex syntax: inside `[...]`, after `\`, or in a verbose comment it is literal text.**
Verbose mode can come from any leading global flag group (`(?i)(?x)` is valid) or a scoped `(?x:...)` that `(?-x:...)` switches off again.
Python accepts global flag groups only at the very start of a pattern and rejects them anywhere else.
Fix: scan with `config._scan`, which tracks escapes, classes, comments and the verbose state per group.

---

## Test Infrastructure

**`from . import server` returns the package attribute when one exists, so faking a missing module via `sys.modules` alone does nothing.**
An earlier test that imported `rustyfarian_bench.server` left `rustyfarian_bench.server` set, so the "extra is absent" test started a real `uvicorn.run` and hung the suite.
Fix: delete both the `sys.modules` entry and the package attribute, and patch `uvicorn.run` to raise, as `tests/test_cli.py::_no_server` does.

---

## Remote Bench Server

**An `Exception` raised from ASGI middleware while FastAPI reads the request body becomes a 400, not your status code.**
FastAPI wraps body parsing in `except Exception` and reports any error there as a malformed body.
Fix: the streamed-size limit raises `server._TooLarge`, a `BaseException`, which `_BodyLimit` turns into a 413.

**A private-CA certificate without key identifier extensions fails the `submit` client with "Missing Authority Key Identifier" even though the CA is trusted.**
Python 3.13+ sets `VERIFY_X509_STRICT` in `ssl.create_default_context()`, so a leaf made with a bare `openssl x509 -req` (no extension file) is rejected; `curl` against the same server is happy.
Fix: issue the server certificate with `subjectKeyIdentifier = hash`, `authorityKeyIdentifier = keyid, issuer` and a `subjectAltName`, as mkcert does; seen 2026-11-14 on Python 3.14 with the TLS smoke in `tmp/tls/`.

**`urllib.request.urlopen` sends an `http://127.0.0.1` request through `HTTP_PROXY`, bearer token included, in the clear.**
`proxy_bypass()` exempts loopback only when `NO_PROXY` names it, and on macOS `getproxies()` falls back to the system proxy settings.
`add_unredirected_header` does not help: it only keeps the header off redirects, and the first request already goes to the proxy.
Fix: `client._opener` adds `ProxyHandler({})` for a loopback URL, which replaces the default handler in `build_opener`; found in review 2026-11-14.
The empty handler itself never shows up in `opener.handlers`, since `add_handler` skips a handler without `<scheme>_open` methods.

---

## Bench Host Provisioning

**`community.sops.sops_encrypt` into a directory that does not exist fails with sops' "error loading config: no matching creation rules found".**
The message points at `.sops.yaml`, but the rules are fine: the module runs sops with the target directory as its working directory, and that directory is missing.
The same `.sops.yaml` matches the same path once the directory exists.
Fix: create the directory first, as `deploy/ansible/ca-init.yml` does; found with sops 3.13.3 and community.sops 2.5.1 on 2026-11-15.

**`ansible-playbook -i localhost,` discovers an interpreter for `localhost` instead of using the one Ansible runs in.**
Implicit localhost uses `ansible_playbook_python`, uvx's Python with `cryptography` and PyYAML; an explicit `localhost` found Homebrew's Python 3.15 without them.
Fix: the controller playbooks set `ansible_python_interpreter: "{{ ansible_playbook_python }}"`, and so does the delegated signing task in the `bench` role.

**ansible-core 2.19+ renders an `assert`'s `fail_msg` even when the assertion passes.**
A `fail_msg` that reads a field of a skipped task's result (`bench_busy.stdout_lines`) fails the play with "object of type 'dict' has no attribute", although the condition guarded it with `default([])`.
Fix: give every registered field in `fail_msg` the same `default(...)` as in `that`; found on the first Pi run 2026-11-16.

**A handler queued in a run that fails later never runs, and the next run never queues it again.**
The first Pi run wrote `/etc/locale.gen` and the sshd drop-in, then failed in the `bench` role before the handlers ran; later runs found both files in place and reported `ok`.
Fix: `force_handlers = true` in `deploy/ansible/ansible.cfg`, and the locale is now checked against `locale -a` instead of a handler; found 2026-11-16.
