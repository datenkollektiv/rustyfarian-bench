# ADR-003: Ansible pushes the bench host; cloud-init only bootstraps it

## Status
Accepted

## Context
The remote bench (ADR-001) needs a host with the boards attached: a Raspberry Pi 5 in the IoT network.
That host holds bearer tokens and a TLS key, flashes boards for everyone, and must be rebuildable without memory of past manual steps.
An earlier kiosk Pi was set up by hand from a list of shell steps, and nobody can say exactly what is on it.
Current Raspberry Pi OS (Debian 13 Trixie) reads cloud-init files from its boot partition on first boot.

## Decision
- Provisioning lives in `deploy/` of this repo, next to the `serve` flags it calls.
- The stock OS image is written by hand with Raspberry Pi Imager, without customisation.
- A committed cloud-init seed, rendered by `just pi-seed` onto the boot partition, only makes the Pi reachable: hostname, admin user, SSH key, network.
- Everything else is an Ansible playbook pushed from the controller by `just pi-provision`, rerunnable and idempotent, which is also the upgrade path.
- `ansible-core` is pinned in the justfile and run through `uvx`, so no global Ansible is needed.
- Secrets follow the datenkollektiv SOPS + age convention v2: encrypted files in `shared-secrets/`, the age key found by sops from the developer's own `SOPS_AGE_KEY_CMD`, never from the repo.
- `just secrets-check` in `verify` fails on any unencrypted file in `shared-secrets/`.
- `.sops.yaml`, `.envrc`, `shared-secrets/` and `secrets/` are gitignored, so this public repo publishes no ciphertext and no recipient list.
- Recipes and the playbook decrypt in memory (`sops exec-env`, the `community.sops.sops` lookup) and never write a decrypted secret to disk.
- The private CA lives in `shared-secrets/bench.yaml`; the server key is generated on the Pi and its CSR signed on the controller.
- The Pi installs `rustyfarian-bench[serve]` from the release tag in `bench_version`, root-owned, and runs `serve` as a hardened systemd service under a `bench` user.
- The playbook does not restart `serve` while a job is running unless forced.

## Consequences
- A Pi is rebuilt by repeating four steps; the CA and tokens survive in `shared-secrets/`, so clients keep working.
- `shared-secrets/` exists only where it was created, since git never holds it; losing it without a backup means a new CA and new tokens for every client.
- Sharing the bench's secrets with a second person needs their age key in `.sops.yaml`, `sops updatekeys` and a copy outside git.
- Writing the image stays manual, so the path is not fully unattended.
- A push to `main` never reconfigures the bench by itself; someone must run `pi-provision`.
- The playbook is not covered by `just verify`; `just deploy-lint` checks it separately.

## Alternatives Considered
|                       Alternative |                       Pros | Cons                                                | Why Rejected                          |
|----------------------------------:|---------------------------:|:----------------------------------------------------|:--------------------------------------|
|          `ansible-pull` on the Pi |            Pure pull-GitOps | Secrets or vault password on the Pi                 | A push to `main` could hit a running job |
|  cloud-init does all provisioning |                   One file | Runs once; any change means re-flashing             | No rerun, no upgrade path             |
|     Imager customisation by hand  |             Simplest start | First boot not reproducible from git                | Breaks "from scratch via git"         |
|        A recipe writes the SD card |         Fully unattended | One wrong `/dev/diskN` erases a disk                | Risk outweighs one manual step        |
|       ansible-vault in the repo   |      Secrets next to code | Public repo holds ciphertext; a vault password to share | SOPS + age is the team convention  |
| Committed `shared-secrets/`       |   Backed up and shared by git | Ciphertext and recipients public forever        | Repo is public                        |
|        Separate provisioning repo |       Clean separation    | `serve` flag changes need two repos                 | Drift between repos                   |
