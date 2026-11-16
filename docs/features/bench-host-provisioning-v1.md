---
gate: none
desk-work: available
---

# Feature: Bench Host Provisioning v1

A brand-new Raspberry Pi 5 becomes the remote bench host from git alone: cloud-init makes it reachable, Ansible does the rest.
Walkthrough and rationale live in `deploy/README.md`; the split is recorded in ADR-003.

## Decisions
|                                       Decision | Reason                                                       | Rejected Alternative            |
|-----------------------------------------------:|:-------------------------------------------------------------|:--------------------------------|
|                     Code in `deploy/` here     | Versioned with the `serve` flags it calls                    | Separate or private infra repo  |
|          Ansible pushed from the controller    | No secrets on the Pi; a push to `main` cannot hit a job      | `ansible-pull`, cloud-init only |
| cloud-init seed committed, rendered by `pi-seed` | First boot reproducible from git; see ADR-003              | Imager customisation by hand    |
|          Stock image written by hand in Imager | A recipe picking `/dev/diskN` can erase the wrong disk       | `dd` in a recipe                |
|      Raspberry Pi OS Lite 64-bit, Trixie       | Current release, cloud-init and netplan built in             | Desktop image, Bookworm         |
|  `ansible-core` pinned and run through `uvx`   | Repo pins the version, no global install                     | Homebrew Ansible                |
|   Private CA on the controller, `community.crypto` | Works on the LAN; CA key never leaves the laptop         | Tailscale, Let's Encrypt DNS-01 |
| SOPS + age convention v2 in `shared-secrets/` | Team convention; key from the developer's `SOPS_AGE_KEY_CMD` | ansible-vault, plain directory  |
| `.sops.yaml`, `.envrc`, `shared-secrets/`, `secrets/` gitignored | Public repo publishes no ciphertext or recipients | Committed `shared-secrets/` |
| Server key generated on the Pi, CSR signed on the controller | The key never travels                         | Key issued on the controller    |
|  uv and espflash as pinned aarch64 binaries + sha256 | Reproducible, fast, reviewed on bump                   | `cargo install`, `curl \| sh`   |
| `rustyfarian-bench` from tag `bench_version`   | Pi runs released code; upgrade is one variable               | Commit SHA, copied wheel        |
|   Tool root-owned under `/opt/rustyfarian-bench` | The service cannot rewrite its own code                    | `uv tool install` as `bench`    |
|   Secrets in `/etc/rustyfarian-bench`, `root:bench 0640` | `serve` allows group access for its own group      | Files owned by `bench`          |
|       systemd `KillMode=mixed`, hardened unit  | `serve` ends a job's process group before the rest           | Default `control-group` kill    |
|   No restart while a job runs, unless forced   | A restart kills a running flash                              | Restart on every change         |
|   `brltty` and `ModemManager` removed          | Both grab USB-serial ports mid-run                           | udev ignore rules               |

## Constraints
- Nothing secret is committed, not even encrypted: no inventory, token, key or Wi-Fi PSK.
- Recipes never write a decrypted secret to disk; the cloud-init seed on the card is the one exception.
- `.sops.yaml` lists only one key for now.
- `just verify` stays offline and Python-only; `deploy-lint` is a separate recipe.
- The bench name has several labels, not `.local` or `.internal`, with a static DHCP lease.
- Rerunning `just pi-provision` on a provisioned Pi changes nothing.
- The local `run` command, the TOML schema and the `serve` API stay unchanged.

## Open Questions
- [x] Does Trixie Lite read the seed from `bootfs`? Yes on paper: cloud-init 25.2, NoCloud from `/boot/firmware`; the Pi run confirms.
- [x] espflash on aarch64? `espflash-aarch64-unknown-linux-gnu.zip`, needs glibc 2.34 only, no libudev.
- [x] Does the Pi 5 power a board without a hub? One C3 (CH340) runs on a plain USB 2 port; several boards untested.
- [ ] Where is the encrypted `shared-secrets/` backed up, since git never holds it?
- [ ] Supply chain after the gate: commit-SHA pins for the tag, Galaxy collections and CI actions, hashed deps for `uv tool install`.
- [ ] Wi-Fi PSK stays plaintext on the FAT boot partition; hash it or remove `network-config` after first boot?
- [ ] Tighter unit after the gate: `DevicePolicy=closed` with tty `DeviceAllow`, `SystemCallFilter`, checked with `systemd-analyze security`.

## State
- [x] Design approved
- [x] `.sops.yaml`, `.envrc` and the gitignore entries in place (local only)
- [x] Secret recipes: `secrets-edit`, `decrypt`, `encrypt`, `secrets-check` (in `verify`), `secrets-updatekeys`, `pi-ca-init`, `pi-ca-cert`
- [x] cloud-init templates and `pi-seed`
- [x] Playbook: `base`, `tools`, `bench` roles, controller CA play
- [x] Recipes: `pi-provision`, `pi-idempotent`, `pi-check`, `deploy-lint` (green, also as a CI job)
- [x] `just doctor [--deep]`: prerequisites with fix hints, shellchecked in `deploy-lint`; deep key check passed on the real key
- [x] `pi-ca-init` and `secrets-edit` run against the real age key
- [x] Fresh Pi 5 provisioned, `pi-check` green; second run `changed=0` (over Ethernet)
- [x] `submit --wait` verdict from the Pi: `hal_c3_connect_async` 3/3 (closes the remote bench v1 Pi gate)
- [x] Documentation updated (`deploy/README.md`, ADR-003, CHANGELOG)

## Session Log
- 2026-11-15 — Designed, v0.6.0 released, implemented with three reviews applied, SOPS convention v2 adopted, `just doctor` added
- 2026-11-16 — First Pi 5 run: seed, provision (fixes: fail_msg, handlers, locale, Wi-Fi idempotence, host keys), `pi-submit` passed 3/3
