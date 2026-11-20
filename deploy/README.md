# Bench host: a Raspberry Pi from a blank SD card

> **Status: running.** A Pi 5 provisioned this way passed its first remote check on 2026-11-16.

This directory turns a brand-new Raspberry Pi 5 into the shared host of the [remote bench](../README.md#remote-bench).
Everything after "write the stock image to the card" is in git: cloud-init gets the Pi onto the network, Ansible does the rest.
Rerunning the same command upgrades the bench, and no step is done by hand on the Pi.

## Why

Rust development for the rustyfarian crates runs in a firewalled devcontainer that sees no USB.
The boards hang on a Pi instead: `submit` builds and uploads an image, `serve` on the Pi flashes it, reads the serial log and judges the steps.
That Pi must be trustworthy and boring: same setup after a reflash, same setup on a second Pi, no "what did I change in 2024?".

An earlier Pi in the same network — the [3.5" kiosk](https://devops.datenkollektiv.de/raspberry-pi-a-box-full-of-opportunities.html) running [avrdude-kiosk](https://github.com/datenkollektiv/avrdude-kiosk) — was set up by hand, following a list of shell steps.
It works, but nobody can say exactly what is on it, and redoing it means redoing the list.
The bench host is the same idea done declaratively.

## The big picture

```mermaid
flowchart LR
    subgraph laptop["Controller (laptop)"]
        seed["just pi-seed"]
        prov["just pi-provision"]
        secrets[("shared-secrets/<br/>SOPS + age: CA key, tokens")]
    end
    subgraph pi["Raspberry Pi 5 — bench.fritz.box"]
        ci["cloud-init<br/>(first boot)"]
        serve["rustyfarian-bench serve<br/>systemd, :8443 TLS"]
        boards["ESP32 boards<br/>USB"]
    end
    dev["devcontainer<br/>just submit …"]

    seed -- "user-data, network-config<br/>on the boot partition" --> ci
    prov -- "ssh + Ansible" --> serve
    secrets -. "decrypted in memory" .-> seed
    secrets -. "decrypted in memory" .-> prov
    dev -- "HTTPS + bearer token" --> serve
    serve -- "espflash, serial" --> boards
```

The work is split at one clear line:

| Stage      | Tool                       | Owns                                                |
|:-----------|:---------------------------|:----------------------------------------------------|
| Card       | Raspberry Pi Imager        | the stock OS image, nothing else                    |
| First boot | cloud-init (from git)      | hostname, admin user, SSH key, network              |
| Everything | Ansible (push, from git)   | packages, tools, service, secrets, firewall         |

cloud-init runs once; Ansible runs as often as you like.
So cloud-init does the minimum to make the Pi reachable, and every decision that may change lives in Ansible.

## Hardware

- Raspberry Pi 5 with the official 27 W USB-C supply.
- A powered USB hub for the boards: the Pi 5 limits USB current without the 5 A supply, and a board that browns out mid-flash looks like a firmware bug.
- Ethernet preferred; Wi-Fi works through `network-config`.
- Waveshare 3.5" display (A): shows the bench banner with the installed version, see the `display` role; the job view is [what comes next](#what-comes-next).

## Layout

```
deploy/
  README.md                     # this file
  .yamllint                     # used by `just deploy-lint`
  doctor.sh                     # `just doctor`: every prerequisite, with a fix hint
  lint.sh                       # `just deploy-lint`
  sops.example.yaml             # template for the repo root's .sops.yaml (gitignored)
  bench-client.sh               # `just pi-check` and `just pi-submit`: URL, CA and token for the live bench
  display-probe.sh              # run on the Pi: which SPI panel is it? loads an overlay at runtime, no reboot
  cloud-init/
    user-data.j2                # hostname, admin user, SSH pubkey, no password login, SSH on
    network-config.j2           # netplan: Ethernet DHCP, optional Wi-Fi
    meta-data.j2                # instance-id, fresh per seed
  ansible/
    ansible.cfg
    requirements.yml            # community.crypto, community.sops, pinned
    inventory.example.yml       # copy to inventory.yml (gitignored)
    group_vars/all.yml          # bench port, where bench.yaml lives
    seed.yml                    # controller: renders cloud-init onto the boot partition
    ca-init.yml                 # controller: creates the CA into shared-secrets/bench.yaml
    site.yml                    # the bench host; certificate signing runs on the controller
    tests/seed-fixture.env      # made-up PI_* values for `just deploy-lint`
    roles/
      base/                     # OS updates, SSH hardening, firewall, USB-serial hygiene
      tools/                    # uv and espflash as pinned, checksummed binaries
      bench/                    # bench user, rustyfarian-bench, secrets, systemd unit
      display/                  # the Waveshare 3.5": overlay, console release, banner service
```

## Prerequisites on the controller

- `just` and `uv` — Ansible itself is not installed; the recipes run a pinned `ansible-core` through `uvx`.
- `sops` 3.10 or newer and an age key for the [secrets](#secrets); `direnv` is optional.
- Raspberry Pi Imager 2.0 or newer (older versions cannot write cloud-init for Trixie and fail silently).
- An SSH key pair.
- A DNS name for the Pi with a static DHCP lease in the router, such as `bench.fritz.box`.
  It must have several labels and must not end in `.local` or `.internal`, because the devcontainer firewall allowlists names only of that shape.

## Ask the doctor

```bash
just doctor           # tools, age key source, SOPS files, inventory, then DNS, SSH and the bench port
just doctor --deep    # also decrypts: your key is a recipient, tokens and seed values are valid
```

Each check prints ✓ or ✗, and every ✗ names the command or setting that fixes it.
The plain run never decrypts, so it never prompts for the age key; `--deep` may ask gopass once.
It changes nothing on the Pi; the first SSH login records the Pi's host key, as `ssh` would.
Run it before each step below: what is still missing tells you where you are.

## Secrets

Secrets follow the datenkollektiv SOPS + age convention v2: encrypted files, an age key sops finds itself, and recipes that hand decrypted values to a process, never to a file.
One deviation: the convention commits `shared-secrets/`, but this repo is public, so every SOPS path stays gitignored, the encrypted ones too:

| Path              | Holds                                                           |
|:------------------|:----------------------------------------------------------------|
| `.sops.yaml`      | the age recipients and which fields of which file get encrypted |
| `.envrc`          | direnv: warns when no age key source is set, nothing more       |
| `shared-secrets/` | SOPS-encrypted files                                            |

The age key never appears in the repo.
Set it up once per developer, in your own `~/.zshenv`, so every shell and `just` without direnv see it:

```bash
# sops runs this only when it decrypts; grep keeps the key line of a full age key file
export SOPS_AGE_KEY_CMD="sh -c 'gopass show <your gopass entry> | grep ^AGE-SECRET-KEY-'"
```

`SOPS_AGE_KEY_FILE`, or sops' default `~/Library/Application Support/sops/age/keys.txt`, works as well.
Do not use `gopass show -o`: it returns the age key file's first line, a comment, and sops reports "Did not find keys".

Two encrypted files hold everything the bench needs:

| File                        | Encrypted                     | Holds                                         |
|:----------------------------|:------------------------------|:----------------------------------------------|
| `shared-secrets/pi.env`     | the whole file                | `PI_*` values for `pi-seed`, Wi-Fi PSK too    |
| `shared-secrets/bench.yaml` | `bench_tokens`, `bench_ca_key` | bearer tokens, CA key; the CA cert stays readable |

With `encrypted_regex`, `bench.yaml` keeps its structure in plaintext: a diff shows that the token `devcontainer` changed, never its value.
Create `.sops.yaml` once from the committed template and put in your age public key, the output of `age-keygen -y <your key file>`:

```bash
cp deploy/sops.example.yaml .sops.yaml
$EDITOR .sops.yaml
```

Adding a person later is one more key in `.sops.yaml` and `just secrets-updatekeys`.

How the recipes reach the values without a plaintext file:

- `pi-seed` runs under `sops exec-env shared-secrets/pi.env`, so the values exist only in its environment.
- The playbook reads `bench.yaml` with the `community.sops.sops` lookup, decrypted in the Ansible process.
- `just secrets-edit <file>` opens a file in `sops`, which re-encrypts it on save; prefer it for a quick change.
- `just decrypt <file>` and `just encrypt <file>` work in place, and `encrypt` also creates a file from `KEY=value` lines.
- `just pi-ca-init` creates the CA once and writes it straight into `bench.yaml` through `sops`.
- `just pi-ca-cert <path>` writes the public CA certificate, for `RUSTYFARIAN_BENCH_CA`.

`just secrets-check`, part of `just verify`, fails on any unencrypted file in `shared-secrets/`, so a `decrypt` is never left behind unnoticed.

Three short-lived exceptions remain:

- `sops` holds the decrypted file in a temp file while `secrets-edit` has it open in the editor;
- `just decrypt` leaves the file in plaintext until `just encrypt`, guarded by `secrets-check`;
- Ansible's `template` module renders `tokens.toml` locally before it copies it to the Pi.

`pipelining` in `ansible.cfg` keeps the CA key, which the signing task gets as an argument, off the controller's `~/.ansible/tmp`.

`shared-secrets/` lives only on this laptop, since it is never committed.
Back it up next to the age key, encrypted as it is: losing it means a new CA and a new token for every client.

## Step 1 — Write the stock image

Write **Raspberry Pi OS Lite (64-bit)**, the current Debian 13 "Trixie" based release, with Raspberry Pi Imager.
Imager lists it under *Choose OS → Raspberry Pi OS (other)*; the top entry is the desktop image.
Press *Skip customisation* when Imager offers it: the settings come from git in the next step.

Writing the raw image is the one manual step on purpose.
A recipe that picks `/dev/diskN` on a Mac is one typo away from erasing the wrong disk.

## Step 2 — Seed cloud-init

Re-insert the card; macOS mounts the boot partition as `/Volumes/bootfs`.

The seed values live in `shared-secrets/pi.env`, created once with `just secrets-edit pi.env`:

```bash
PI_HOSTNAME=bench
PI_USER=admin
PI_SSH_PUBKEY=ssh-ed25519 AAAA… you@laptop
# optional, only without Ethernet
PI_WIFI_COUNTRY=DE
PI_WIFI_SSID=iot
PI_WIFI_PSK=…
```

Values are read raw, as `sops exec-env` passes them: no quotes, even around the SSH key's spaces.
`PI_TIMEZONE` (default `Europe/Berlin`) and `PI_LOCALE` (default `en_GB.UTF-8`) are optional too.

```bash
just pi-seed /Volumes/bootfs
```

`pi-seed` renders the templates in `cloud-init/` and writes `user-data`, `network-config` and `meta-data` onto the boot partition, replacing the ones Imager left there.
It refuses a directory without `cmdline.txt` and `config.txt`, so a wrong path cannot overwrite some other disk.
It also appends `ds=nocloud;i=<instance-id>` to `cmdline.txt`, as Imager does: without it cloud-init drops its datasource cache on every boot.
The result:

- the hostname, time zone and locale are set;
- the admin user replaces the default `pi` user, with your public key and no password;
- SSH is enabled, password login is off;
- the network comes up through netplan, Ethernet with DHCP and Wi-Fi if you set it.

The admin user keeps the image's passwordless `sudo`, which Ansible's `become` relies on; SSH keys are its only way in.
After provisioning, `sshd` allows three tries per connection, so with several keys in your agent, name the Pi's key in `~/.ssh/config`:

```
Host bench.rustbox
    User admin
    IdentityFile ~/.ssh/<the Pi's key>
    IdentitiesOnly yes
```

The Wi-Fi PSK is plaintext only on the card, which cloud-init needs; the boot partition is readable by anyone holding the card.
Boot the Pi and wait until `ssh admin@bench.fritz.box` lets you in.

## Step 3 — Provision with Ansible

Before the first run, create the CA and the tokens:

```bash
just pi-ca-init                     # once: CA key and certificate into shared-secrets/bench.yaml
just bench-token-add laptop         # one generated token per person or machine
```

Then point Ansible at the Pi and run it:

```bash
cp deploy/ansible/inventory.example.yml deploy/ansible/inventory.yml   # once; gitignored
$EDITOR deploy/ansible/inventory.yml                                    # host, user, bench_fqdn, LAN CIDR
just pi-upgrade
```

`pi-provision` and `pi-upgrade` run `just deploy-lint` first, then `site.yml`; extra arguments go to `ansible-playbook`.
`pi-upgrade` is `pi-provision` plus `apt full-upgrade`, so a fresh image starts current; later runs leave the OS packages to `unattended-upgrades` unless you upgrade on purpose.
Both refresh the apt lists when they are older than an hour, so a fresh or long-off image does not fail on package versions the mirror has dropped.
`just pi-provision --check --diff` previews drift on a provisioned Pi without changing it; on a fresh one it stops early, since nothing it reads exists yet.
Ansible needs Python 3.13 or newer on the controller: `uvx` fetches it, nothing to install.

```yaml
# shared-secrets/bench.yaml, as `sops` shows it while editing
bench_tokens:
  laptop: "…"
  devcontainer: "…"
bench_ca_key: |
  -----BEGIN PRIVATE KEY-----
  …
bench_ca_cert: |
  -----BEGIN CERTIFICATE-----
  …
```

### The private CA

The bench speaks TLS with a certificate from the private CA in `bench.yaml`.
The server key is generated on the Pi and never leaves it.
Its certificate signing request is signed on the controller (`delegate_to: localhost`) with `community.crypto`, the CA key decrypted only in memory.
Python 3.13 verifies certificates strictly, so the certificate carries a `subjectAltName` and both Subject and Authority Key Identifiers; without them `submit` fails with "Missing Authority Key Identifier".
The certificate lasts 825 days and is reissued when the name changes or less than 30 days remain.

### The roles

**`base`** — a calm, locked-down host:

- `systemd-timesyncd` keeping the clock right, which TLS and apt need; every run waits up to two minutes for its sync before apt refreshes the lists, and fails if NTP stays out of reach;
- `apt full-upgrade` with `just pi-upgrade` only, and `unattended-upgrades` for Debian's daily security and point-release fixes; a pending reboot is reported, never done by the play;
- the Wi-Fi from `PI_WIFI_SSID` and `PI_WIFI_PSK` in `pi.env`, taking over the profile cloud-init made, so a PSK change needs no reflash;
  an empty `PI_WIFI_SSID` leaves the profile alone, and a new SSID adds a profile next to the old one, which you delete with `nmcli connection delete`;
- `sshd` with keys only and no root login;
- `nftables` dropping inbound traffic except SSH, DHCP replies and the bench port from `bench_allowed_cidrs`;
- `brltty` and `ModemManager` kept off: the Lite image has neither, but a later desktop or kiosk package could pull in these grabbers of USB-serial ports.

**`tools`** — uv and espflash as pinned GitHub release binaries for `aarch64-unknown-linux-gnu`.
Each download is checked against a sha256 in the role's defaults, so a version bump is a reviewed change of two lines.
espflash stays at 4.6.0, the version of the laptop end-to-end run, until the Pi has passed; 4.7.0 is the next bump.
Compiling espflash on the Pi would need a Rust toolchain and many minutes; the prebuilt binary needs neither.

**`bench`** — the service itself:

- a system user `bench` in `dialout`, with `/srv/bench` as home and the outbox in `/srv/bench/outbox`;
- `rustyfarian-bench[serve]` installed with `uv tool install` from the git tag in `bench_version`, root-owned under `/opt/rustyfarian-bench`, so the service cannot rewrite its own code;
- `/etc/rustyfarian-bench/` with `cert.pem`, `key.pem` and `tokens.toml`, the last rendered from `bench_tokens` with `no_log`, all owned `root:bench`, mode `0640`;
- a systemd unit running:

```bash
rustyfarian-bench serve --host 0.0.0.0 --port 8443 \
    --outbox /srv/bench/outbox \
    --job-timeout 600 --detect-timeout 30 \
    --token-file /etc/rustyfarian-bench/tokens.toml \
    --ssl-certfile /etc/rustyfarian-bench/cert.pem \
    --ssl-keyfile /etc/rustyfarian-bench/key.pem
```

The unit's choices, and why:

| Setting                                            | Why                                                                   |
|:---------------------------------------------------|:----------------------------------------------------------------------|
| `User=bench`, `SupplementaryGroups=dialout`        | serial ports without root                                             |
| `KillMode=mixed`, `TimeoutStopSec=30`              | `serve` gets SIGTERM first and ends a running job's process group     |
| `Restart=on-failure`                               | a crash restarts; a restart marks the interrupted job as `error`      |
| `StartLimitBurst=5` in 300 s                       | a bad token file ends `serve` at start; no endless restart loop       |
| `Environment=HOME=/srv/bench`                      | espflash may keep its config there, the one writable place            |
| `ProtectSystem=strict`, `ReadWritePaths=/srv/bench`| the service writes only its outbox                                    |
| `NoNewPrivileges`, `PrivateTmp`, `ProtectHome`     | cheap hardening                                                       |
| no capabilities, `Protect*`, `Restrict*`, `UMask=0077` | a network service that runs uploaded images gets nothing it does not need |
| no `PrivateDevices`                                | boards re-enumerate after a flash, and new `/dev/tty*` must be visible|

`serve` sets `ESPFLASH_SKIP_UPDATE_CHECK=true` for espflash itself, so the unit does not.
`serve` re-reads the token file on every request, so changing tokens never restarts the service.

The play refuses four changes while any job's `job.json` is not `done`, unless `-e bench_force_restart=true` is given:

- a restart of `serve`, due only when the unit, the key, the certificate or `bench_version` changed, because it ends running and queued jobs as `error`;
- a new `bench_version`, which replaces the code the jobs run from;
- `pi-upgrade`, because `serve` runs on the system Python that `apt full-upgrade` may replace;
- a bumped `espflash`, which every flash calls (`uv` is guarded with it).

The jobs are read before the first role runs and each refusal comes before its change, so a refused run leaves the Python, the tools, the bench code and `serve` itself as the job found them.
Base packages, firewall, sshd and the token file are still brought up to date.
A refused restart is remembered in `/etc/rustyfarian-bench/restart-pending`, so the next run retries it once the jobs are done, even when nothing else changed.
Two gaps stay open until `serve` gets a maintenance mode (see the roadmap):

- the read is a snapshot, so a job submitted during the run still meets the upgrade, the tool or the restart;
- `unattended-upgrades` installs Debian's fixes daily without this guard, accepted since neither `serve` nor espflash comes from apt and a job runs in a fresh child process, so a Python patch release lands under a job at worst.

**`display`** — the Waveshare 3.5" panel, first light for the kiosk:

- `dtoverlay=piscreen,drm,speed=16000000,rotate=180` in a managed block at the end of `/boot/firmware/config.txt`: the stock Raspberry Pi overlay drives this panel through the mainline DRM driver, found with `deploy/display-probe.sh` (`display_drm: false` picks the fbtft driver, also proven, which counts its rotation from 270 and wants 90); the overlay loads at the next reboot, which the run reports and never does;
- a udev rule that releases the kernel console from the panel: headless, the panel is framebuffer 0, so fbcon would show the login prompt on it and blank it after ten minutes (`echo 1 > /sys/class/vtconsole/vtconN/bind` gives the console back);
- a system user `bench-display`, whose hardened unit `rustyfarian-bench-display.service` gets the `video` group and the framebuffer device and nothing else, no network included;
- `/usr/local/bin/rustyfarian-bench-display`, a Pillow script: it waits for the panel's framebuffer, draws "rustyfarian bench" and `bench_version`, and sleeps until stopped, when it draws "good-bye" (so the panel says so at shutdown); a bench upgrade changes the unit and restarts it with the new number.

`just display-smoke` renders the banner on the controller into `tmp/display/banner.png` and a fake framebuffer, with the Pi's Pillow version and its fallback font where DejaVu is absent, so a layout change is seen before it reaches the Pi.

## Step 4 — Check it

```bash
just pi-idempotent        # applies the play twice; the second run must change nothing
just pi-check             # the first token in bench.yaml; `just pi-check laptop` names one
```

`pi-check` runs from the controller, over TLS verified against the CA in `bench.yaml`:

- a request without a token answers `401`;
- a request with the named token for an unknown job answers `404`;
- `rustyfarian-bench detect` on the Pi, over SSH, lists the attached boards.

The token reaches `curl` through a pipe, never through its arguments.
`detect` probes every port, so run `pi-check` while no job runs.

After the first reboot the panel shows the banner with `bench_version`; if it stays white, `journalctl -u rustyfarian-bench-display` on the Pi says whether the framebuffer was found.

Then the real test, a consumer's check run on the Pi:

```bash
just pi-submit rustyfarian-network c3 hal_c3_connect_async --wait
```

That builds the image through the consumer's `image` command in its `bench.toml`; rustyfarian-network's `just save-image` is a working example.
A consumer without one needs a prebuilt merged image instead:

```bash
just pi-submit rustyfarian-network c3 hal_c3_connect_async --wait --image /path/to/hal_c3_connect_async.bin
```

The image must be merged with the consumer's own bootloader (`espflash save-image --merge` with its flash arguments); a bare ELF gets espflash's bootloader, which may not boot, see the [remote bench](../README.md#remote-bench).
The first Pi passed this way: three steps, Wi-Fi link up, a DHCP lease, 20 s without a panic.

`pi-submit` wraps `just submit`: the URL comes from the inventory, the CA and the first token from `bench.yaml`, and the token reaches only the `submit` process.
A client without this checkout, such as a devcontainer, sets `RUSTYFARIAN_BENCH_URL`, `RUSTYFARIAN_BENCH_CA` (from `just pi-ca-cert <path>`) and `RUSTYFARIAN_BENCH_TOKEN` itself.

A verdict coming back is the gate: a job travelled from the laptop to the Pi, through `espflash` to a board and back as a judged result.

## Day 2

| Task                    | How                                                                  |
|:------------------------|:---------------------------------------------------------------------|
| Upgrade the bench       | bump `bench_version` (a release tag), `just pi-provision`            |
| Roll back the bench     | set `bench_version` to the previous tag, `just pi-provision`         |
| Upgrade the OS          | `just pi-upgrade`                                                    |
| Reboot after it         | when reported: `sudo reboot` on the Pi once no job runs, then `just pi-check` |
| Check for drift         | `just pi-provision --check --diff` previews; `just pi-idempotent` applies twice |
| Upgrade espflash or uv  | bump version and sha256 in `roles/tools/defaults`, `just pi-provision` |
| Add or revoke a token   | `just secrets-edit bench.yaml`, `just pi-provision`; no restart      |
| Change the Wi-Fi        | `just secrets-edit pi.env`, `just pi-provision`, over Ethernet        |
| Add a person to SOPS    | their age key in `.sops.yaml`, `just secrets-updatekeys`             |
| Rebuild the Pi          | Steps 1–4 again; the CA and tokens survive in `shared-secrets/`      |
| Read the logs           | `journalctl -u rustyfarian-bench -f`, jobs in `/srv/bench/outbox/`   |
| Clear old jobs          | by hand: images may hold Wi-Fi credentials and the outbox is kept forever |
| Panel white or stale    | `journalctl -u rustyfarian-bench-display`; `display-probe.sh` loads the overlay live |

`pi-upgrade`, a tool bump and a restart are refused while a job is unfinished; see the `bench` role above.
Change the Wi-Fi over Ethernet: the link changes mid-run.

## Design decisions

| Decision                                         | Rejected alternative                | Why                                                            |
|:-------------------------------------------------|:------------------------------------|:---------------------------------------------------------------|
| Ansible pushed from the controller               | `ansible-pull` on the Pi            | no secrets on the Pi; a push to `main` cannot reconfigure the bench mid-job |
| cloud-init only for reachability                 | cloud-init does everything          | cloud-init runs once; Ansible reruns and upgrades              |
| Stock image written by hand                      | a recipe that writes the disk       | one wrong `/dev/diskN` erases a disk                           |
| Private CA on the controller                     | Tailscale, Let's Encrypt DNS-01     | works on the LAN without a domain or a tailnet                 |
| SOPS + age, the datenkollektiv convention        | ansible-vault, a plain directory    | one tool and one key across our repos; diffable, encrypted at rest |
| Secret paths gitignored, even encrypted          | committed `shared-secrets/`         | a public repo publishes no ciphertext and no recipient list    |
| Server key generated on the Pi                   | key issued on the controller        | the key never travels                                          |
| Pinned, checksummed release binaries             | `cargo install`, `curl \| sh`       | reproducible, fast, reviewed on bump                           |
| `ansible-core` through `uvx`                     | a global Ansible install            | the version is pinned by the repo, not by the laptop           |
| `rustyfarian-bench` from a release tag           | a commit SHA, a copied wheel        | the Pi runs released code; upgrading is one variable           |
| stock `piscreen` overlay, DRM variant            | the fbtft variant, Waveshare's dtbo | both drive the panel; DRM is mainline and a KMS device for a later compositor |

## What comes next

The panel becomes a kiosk showing the bench's queue: the running job, its board and its live log tail, and the last verdicts.
That needs a read-only job list in `serve`, which it does not have yet; the banner script of the `display` role then polls it and redraws, instead of a browser.
A Wayland compositor on the panel stays possible, since the DRM variant makes it a KMS device; `fbcon=map:` and Chromium were weighed and left aside, see `docs/features/bench-kiosk-v1.md`.
