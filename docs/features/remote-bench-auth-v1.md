---
gate: hardware
gate-note: a `submit` from a devcontainer reaches the Pi over TLS with a token and ends in a verdict
desk-work: available
---

# Feature: Remote Bench Auth v1

Rust development runs in a firewalled devcontainer that sees no USB; the boards hang on a Pi running `serve`.
Today anyone who reaches the port can flash a board and read every job, so `serve` learns to require a bearer token over TLS, and `submit` picks the token up from the environment the devcontainer template already forwards.

## Decisions
|                                                          Decision | Reason                                                                       | Rejected Alternative                                  |
|------------------------------------------------------------------:|:-----------------------------------------------------------------------------|:------------------------------------------------------|
|                    Static bearer token in `Authorization: Bearer` | One opaque string; rides `remoteEnv ${localEnv:X:}` as the Claude token does | mTLS client certs: key material in env, no revocation |
|                      Token check as the outermost ASGI middleware | 401 before the body is read; 401 wins over 413                               | FastAPI `Depends`: body parsed before auth            |
|                         `hmac.compare_digest` against every token | Constant time on a shared LAN                                                | `==`                                                  |
|                           Token file is TOML, `name = "<secret>"` | `tomllib` is in use already; names reuse `NAME`                              | One `name token` per line                             |
|              Token file re-read per request; unreadable means 503 | Add or revoke without a restart that kills a running job                     | Load once at start                                    |
|           `Job.submitter` records the token name, never the token | Per-job attribution, per-person revocation                                   | Anonymous jobs                                        |
|     Off loopback, `serve` needs `--token-file` and both TLS flags | The tool keeps the bench closed, not the README                              | Warning only                                          |
|         TLS via `--ssl-certfile`/`--ssl-keyfile` to `uvicorn.run` | Two kwargs; no second daemon on the Pi                                       | Caddy or nginx in front                               |
|      Client refuses a token over `http://` to a non-loopback host | A sniffed token flashes boards and reads images                              | Warn and allow                                        |
|                               Loopback requests skip HTTP proxies | A proxy would read the token; its loopback is not ours                       | Honour `HTTP_PROXY` for loopback                      |
|      Client reads `RUSTYFARIAN_BENCH_TOKEN`; `""` counts as unset | `${localEnv:X:}` yields `""` when the host var is unset                      | `--token` flag, which lands in shell history          |
|                     `--bench` defaults to `RUSTYFARIAN_BENCH_URL` | The daily command loses its one flag; the URL is not secret                  | Required flag                                         |
| `RUSTYFARIAN_BENCH_CA` feeds `ssl.create_default_context(cafile)` | uv's CPython ignores the OS trust store; a private CA needs it               | OS-level trust                                        |
|                                     No `--insecure` on the client | A rogue host must not harvest tokens or images                               | Verification toggle                                   |
|   Private CA (mkcert) now; Tailscale and Let's Encrypt documented | Works on the LAN without a domain                                            | Tailnet-only bench with an open app                   |
|                                         No token expiry or scopes | One team, one bench; revocation is deleting one line                         | Roles, expiring tokens                                |
|                             Any token reads and submits every job | One team; `submitter` is attribution, not access                             | Per-token job ownership                               |
|           Revoking stops new requests, not queued or running jobs | A running flash is not interrupted mid-write                                 | Cancel the revoked token's jobs                       |
|                  Token file must not be accessible to other users | A world-readable file hands out every token                                  | Trust the README's `chmod 600`                        |

## Constraints
- `client.py` stays stdlib-only: `urllib`, `ssl`, `os`, `json`.
- `remote.py` stays free of web-framework imports; token loading lives there because it is stdlib.
- `create_app()` without a token file behaves exactly as today, so every existing test passes unchanged.
- An `outbox/<id>/job.json` written before this change, without `submitter`, still loads and recovers.
- The token never appears under `outbox/<id>/`, in `run.log` or in any warning text.
- `serve` keeps its `127.0.0.1` default; only an explicit non-loopback `--host` requires tokens and TLS.
- The devcontainer receives the token only through env (`remoteEnv`, Codespaces secrets); no host file is mounted.
- The bench hostname must pass the sandbox firewall grammar: multi-label, no IP literal, not `.local` or `.internal`.
- `mypy --strict` covers tests, ruff line length 100, and no test needs a board or a network.

## Open Questions
- [ ] Which DNS name will the Pi have, and does it resolve inside the sandbox when the firewall starts?
- [ ] Will the Pi get a static DHCP lease, since the firewall's ipset pins the resolved IP at container start?
- [ ] Does `${localEnv:RUSTYFARIAN_BENCH_TOKEN:}` yield an empty string, not the literal, in the JetBrains devcontainer flow?

## State
- [x] Design approved
- [x] Server: token file, middleware, `submitter`, TLS flags, off-loopback refusal
- [x] Client: token and CA from env, URL default, plain-http refusal
- [x] Tests passing
- [x] Documentation updated (README, ADR-001, CHANGELOG, roadmap)
- [x] Laptop smoke: `submit` over TLS with a private CA and a token, `submitter` on the job, 401 and CA hints seen
- [ ] `submit` from a devcontainer against the Pi over TLS

## Session Log
- 2026-11-14 — Feature doc created via /feature dialog after a four-approach judge panel (bearer 59, mTLS 42, Tailscale 40, HMAC 38 of 75)
- 2026-11-14 — Implemented test-first (62 new tests); laptop TLS smoke found Python's strict X.509 needs key identifier extensions on the server certificate (lore)
- 2026-11-14 — Review fixes: token dropped on redirects, ASCII-only tokens, missing TLS file exits 2, `serve` tests check the app enforces tokens
- 2026-11-14 — Review P1: `urlopen` sent a loopback request through `HTTP_PROXY`, token included; loopback now skips proxies (lore)
- 2026-11-14 — Review P2: a token with a CR or LF reached `http.client`, whose error quoted it; the client now refuses it first
- 2026-11-14 — PR #12 review: access boundary documented, `_submitter` scans every token, token file refused when others can read it
