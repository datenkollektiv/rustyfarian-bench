---
gate: hardware
gate-note: a tagged release on the Pi shows a job on the panel and the second provisioning run reports `changed=0`
desk-work: none
---

# Feature: Bench Kiosk v2

The bench Pi's panel shows the running job and the last verdicts instead of the static v1 banner.
v1 decisions carry over (`archive/bench-kiosk-v1.md`): stock `piscreen` DRM overlay, Pillow into the framebuffer, hardened `bench-display` unit.

## Decisions
|                                       Decision | Reason                                                              | Rejected Alternative                          |
|-----------------------------------------------:|:--------------------------------------------------------------------|:----------------------------------------------|
|   `serve` writes `/run/rustyfarian-bench/status.json` | Display keeps `PrivateNetwork=yes`; no auth design; socket-free tests | `GET /jobs` over TLS loopback, a unix socket |
|       The file holds only what the panel draws | It is 0644 on the Pi; no submitter, no log text                      | Dumping `asdict(job)`                         |
|      One writer under a lock, chmod before replace | POST and the worker thread both update it; `UMask=0077` makes it 0600 | `_atomic_write`'s fixed temp name unguarded   |
| `serve` parses the `▶ [n/total]` line from `run.log` | The step exists nowhere else; no change to the child runner      | A step callback in the runner                 |
|   Running view: board, example, elapsed, step, bar, 5 verdicts | Everything the bench owner needs, no firmware text on the panel | A live log tail (secrets, NVS, retention)     |
|     Idle view: banner, version, name and address, last verdicts | History visible most of the time; one renderer for both states | The v1 banner unchanged                       |
|  Elapsed time computed by the renderer from `started` | No rewrite per second                                         | `serve` rewriting the file every second       |
|       Heartbeat: rewrite every ~10 s, stale after ~30 s | Detects a hung `serve`, not only a stopped one              | Missing file as the only signal               |
| `rustyfarian-bench kiosk` with a `[kiosk]` extra | Shares the status record with `serve`, one tag, mypy and pytest     | Role file on apt `python3-pil` (version skew) |
|  Pure `layout(status, now)` behind a thin painter | Layout testable in `verify` without Pillow fonts or a framebuffer   | Golden images (fonts differ on the Pi)        |
|       Poll every 1 s, write the frame only on change | Idle costs nothing; running is one frame/s, ~20% of SPI      | Full frame every tick, partial row writes     |
|       `bench_version` to v0.7.0 first, as its own change | It is the pending roll-out; v2 then needs only its own tag | Bumping once with v2's tag                    |

## Constraints
- The TOML schema and the CLI contract stay unchanged; `kiosk` is an additive subcommand.
- A status write failure never changes a job's verdict or slows `_finish`.
- `bench-display` never joins group `bench` and never reads the outbox, the tokens or the key.
- The display unit keeps its hardening; it only gains `After=time-sync.target` for the heartbeat age check.
- The serve unit gains `RuntimeDirectory=`; that change restarts `serve`, so the rollout runs on an idle bench.
- The `[kiosk]` extra arrives only with a new tag; the install skip compares the version alone.
- `just verify` and `just deploy-lint` stay green; a second `just pi-provision` ends at `changed=0`.
- No SSH from the controller; Pi checks are scripts the user runs and pastes back.

## Open Questions
- [x] Address on the idle screen: Ansible sets `BENCH_ADDRESS` from `display_address`; the status file never holds it.
- [x] Pillow: the lock's 13.0.0 ships a cp313 manylinux aarch64 wheel and its own types, so `mypy --strict` needs no stubs.
- [x] History vs retention: retention must keep the newest 5 finished jobs, noted in its roadmap entry.
- [x] `maintenance` is reserved in schema 1: v2 never writes it, the kiosk draws it.
- [ ] SPI speed (from v1): stays 16 MHz unless the 1 s refresh proves too slow.
- [ ] Farewell at poweroff (from v1): does the DRM driver's shutdown blank the panel after "good-bye"? Check on the Pi.
- [ ] Log tail: revisit in a later version once retention and NVS settle what logs may hold.

## State
- [x] Design approved
- [x] `bench_version` v0.7.0 on the Pi (the panel shows it; the `changed=0` re-run moves to the final Pi check)
- [x] Status writer in `serve` with tests (python-pro, test-automator, plan mode)
- [x] `kiosk` subcommand, `[kiosk]` extra, display role switched to it
- [x] `just display-smoke` renders fixtures: idle, running, queued, long names, stale, stopped, maintenance
- [ ] Pi: tagged release installed, panel shows a job, second run `changed=0`
- [x] Documentation updated: `deploy/README.md` "What comes next", CHANGELOG, roadmap

## Session Log
- 2026-11-20 — Feature doc created via /feature dialog after a five-agent analysis workflow; status file, layout A, `kiosk` subcommand, draw on change
- 2026-11-20 — Updated: gate cleared (panel proven on the Pi, shows v0.7.0); decisions confirmed unchanged
- 2026-11-21 — Implemented: status writer, `Worker(observer=)`, `serve --status-file`, `kiosk` and `[kiosk]`, display role switched; four open questions resolved by the approved plan
- 2026-11-21 — Pi: `just pi-provision-dev` (v0.8.0.dev0) installed; the panel showed a live `esp32` `hal_esp32_connect_async` run (pass 3/3, seen by the user); second run `ok=62 changed=0`; `pi-accept.sh`: modes, contents, isolation and units pass, heartbeat section not yet seen
- 2026-11-21 — Two review rounds fixed: bench role refuses a `bench_version` without `--status-file`, kiosk text fits its column in pixels, history sorted by finish time, status writes close their fd, heartbeat survives a failed first write, steps read from appended log lines
