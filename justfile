# rustyfarian-bench — guided hardware bench runner

# list recipes
_default:
    @just --list

# every Python call goes through the locked venv, so no recipe runs against a stale one
uv_run := "uv run --locked"
pip_audit := "2.10.1"
# the dev default Python (.python-version): the CI matrix entry that also runs the install smoke check
dev_python := trim(read(justfile_directory() / ".python-version"))
# a consumer repo argument, read as "$1": a sibling checkout's name, or an absolute path
consumer_repo := 'repo=$1; [[ "$repo" == /* ]] || repo="' + justfile_directory() + '/../$repo"'
# act: a local rustyfarian-ci checkout if RUSTYFARIAN_CI points at one, otherwise the pushed @v1.
ci_dir := env("RUSTYFARIAN_CI", "")
ci_repo_flag := if ci_dir != "" { '--local-repository "datenkollektiv/rustyfarian-ci@v1=' + absolute_path(ci_dir) + '"' } else { "" }

# --- dev: the Python package itself -------------------------------------------

# install what a fresh clone is missing, idempotent: the locked venv; `just setup bench-host` adds the Ansible collections
[group('dev')]
setup component="":
    {{ if component == "" { "" } else if component == "bench-host" { "" } else { error("unknown component '" + component + "'; the only one is bench-host") } }}
    uv sync --locked --all-extras
    {{ if component == "bench-host" { quote(just_executable()) + " deploy-collections" } else { "" } }}

# format, lint, type-check and test (modifies files)
[group('dev')]
pre-commit: fmt verify

# non-modifying gate: no plaintext in shared-secrets/, lint, format check, type-check, tests
[group('dev')]
verify: secrets-check lint fmt-check typecheck test

# run the verify gate on another Python in a scratch venv: `just verify-python 3.12`
[group('dev')]
verify-python version:
    UV_PYTHON="{{ version }}" UV_PROJECT_ENVIRONMENT="tmp/venv-py{{ version }}" "{{ just_executable() }}" verify

# build the wheel and run it like a consumer's uvx: --help, list a fixture, the `serve` hint
[group('dev')]
smoke-install:
    bash "{{ justfile_directory() }}/scripts/smoke-install.sh"

# run the unit tests (all extras, so the serve tests can import fastapi)
[group('dev')]
test *args:
    {{ uv_run }} --all-extras pytest {{ args }}

# lint with ruff
[group('dev')]
lint:
    {{ uv_run }} ruff check

# format with ruff
[group('dev')]
fmt:
    {{ uv_run }} ruff format

# check formatting without changing files
[group('dev')]
fmt-check:
    {{ uv_run }} ruff format --check

# strict mypy
[group('dev')]
typecheck:
    {{ uv_run }} --all-extras mypy src tests

# audit the locked dependencies, all extras, against the advisory database; needs the network, so not in verify
[group('dev')]
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    req="{{ justfile_directory() }}/tmp/audit-requirements.txt"
    mkdir -p "$(dirname "$req")"
    uv export --locked --all-extras --no-hashes --no-emit-project -o "$req" --quiet
    uvx "pip-audit@{{ pip_audit }}" -r "$req" --progress-spinner off

# run the CI jobs locally with act (needs Docker + act): all jobs, or one by id, `just act deploy-lint`
[group('dev')]
act job="":
    #!/usr/bin/env bash
    set -euo pipefail
    for j in {{ if job == "" { "verify deploy-lint" } else { job } }}; do
        act -j "$j" --pull=false -P ubuntu-latest=catthehacker/ubuntu:act-latest {{ ci_repo_flag }}
    done

# remove scratch files, build output and tool caches; keeps .venv and release/
[group('dev')]
clean:
    cd "{{ justfile_directory() }}" && rm -rf tmp dist .pytest_cache .mypy_cache .ruff_cache

# sync .venv with uv.lock, all extras; fails on a stale lock by design (`just lock` fixes it)
[group('dev')]
sync:
    uv sync --locked --all-extras

# re-lock dependencies after a pyproject.toml change (e.g. a version bump), then sync .venv to it
[group('dev')]
lock:
    uv lock
    uv sync --locked

# --- ci: one recipe per GitHub Actions job; the job runs only this ------------

# the Verify job for one matrix Python; on the dev default also the install smoke check: `just ci-verify 3.12`
[group('ci')]
ci-verify python: (verify-python python)
    {{ if python == dev_python { quote(just_executable()) + " smoke-install" } else { "" } }}

# the Deploy lint job
[group('ci')]
ci-deploy-lint: deploy-lint

# --- bench: run consumers' checks from this checkout --------------------------

# serve the remote bench from here, outbox in tmp/: `just serve --port 8001`
[group('bench')]
serve *args:
    {{ uv_run }} --extra serve rustyfarian-bench serve --outbox tmp/outbox {{ args }}

# probe the connected boards
[group('bench')]
detect:
    {{ uv_run }} rustyfarian-bench detect

# run a consumer's checks from here: `just bench rustyfarian-peripherals s3 b3f` (sibling name or absolute path)
[group('bench')]
[positional-arguments]
bench repo target *args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ consumer_repo }}
    shift
    {{ uv_run }} rustyfarian-bench --repo "$repo" run "$@"

# list a consumer's scenarios: `just bench-list rustyfarian-ws2812`
[group('bench')]
[positional-arguments]
bench-list repo:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ consumer_repo }}
    {{ uv_run }} rustyfarian-bench --repo "$repo" list

# submit a consumer's check to a remote bench (URL from $RUSTYFARIAN_BENCH_URL or --bench): `just submit rustyfarian-network c3 hal_c3_connect_async --wait`
[group('bench')]
[positional-arguments]
submit repo board example *args:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ consumer_repo }}
    shift
    {{ uv_run }} rustyfarian-bench --repo "$repo" submit "$@"

# --- bench host (deploy/README.md): pins, then recipes -------------------------

ansible_core := "2.22.0"
ansible_lint := "26.11.0"
cryptography := "50.0.2"
yamllint := "1.38.0"
check_jsonschema := "0.39.0"
shellcheck := "0.11.0.1"
# cloud-init's own JSON schemas at the version Raspberry Pi OS Trixie ships
cloud_init_schemas := "https://raw.githubusercontent.com/canonical/cloud-init/25.2/cloudinit/config/schemas"
ansible_dir := justfile_directory() / "deploy" / "ansible"
shared_secrets := justfile_directory() / "shared-secrets"
# recipe arguments arrive as "$1" and are checked before any use, never pasted into shell code
secret_file_check := '[[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*\.(env|json|ya?ml)$ ]] || { echo "expected a file name like pi.env or bench.yaml, not: $1" >&2; exit 2; }'
# serve's NAME and NAME_MAX (src/rustyfarian_bench/remote.py)
token_name_check := '[[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$ ]] || { echo "a token name matches [A-Za-z0-9][A-Za-z0-9_-]*, 80 characters at most, not: $1" >&2; exit 2; }'
# ansible-core 2.22 needs Python 3.13+ on the controller; cryptography signs the server certificate
ansible := "uvx --python 3.13 --from ansible-core==" + ansible_core + " --with cryptography==" + cryptography

# check the bench host prerequisites and say how to fix each; `just doctor --deep` also decrypts (may prompt for the age key)
[group('bench-host')]
[positional-arguments]
doctor *args:
    @JUST="{{ just_executable() }}" ANSIBLE="{{ ansible }}" bash "{{ justfile_directory() }}/deploy/doctor.sh" "$@"

# install the pinned Ansible collections into deploy/ansible/.collections
[group('bench-host')]
deploy-collections:
    cd "{{ ansible_dir }}" && {{ ansible }} ansible-galaxy collection install -r requirements.yml -p .collections

# lint deploy/ and scripts/: yamllint, shellcheck, syntax check, ansible-lint, and the cloud-init seed rendered from fixture values
[group('bench-host')]
deploy-lint: deploy-collections
    ANSIBLE="{{ ansible }}" ANSIBLE_CORE="{{ ansible_core }}" ANSIBLE_LINT="{{ ansible_lint }}" \
        YAMLLINT="{{ yamllint }}" SHELLCHECK="{{ shellcheck }}" CHECK_JSONSCHEMA="{{ check_jsonschema }}" \
        CLOUD_INIT_SCHEMAS="{{ cloud_init_schemas }}" bash "{{ justfile_directory() }}/deploy/lint.sh"

# render each kiosk screen (idle, running, queued, long names, stale, stopped, maintenance) and the farewell into tmp/display/ from fixture status files (fake framebuffer plus PNGs), no Pi needed; needs the network for Pillow until it is cached
[group('bench-host')]
display-smoke:
    bash "{{ justfile_directory() }}/deploy/display-smoke.sh"

# create the bench CA once, straight into shared-secrets/bench.yaml
[group('bench-host')]
pi-ca-init: deploy-collections
    cd "{{ ansible_dir }}" && {{ ansible }} ansible-playbook -i localhost, ca-init.yml

# write the public bench CA certificate for RUSTYFARIAN_BENCH_CA: `just pi-ca-cert ~/.config/rustyfarian-bench/bench-ca.pem`
[group('bench-host')]
[positional-arguments]
pi-ca-cert out:
    #!/usr/bin/env bash
    set -euo pipefail
    out="$1"
    mkdir -p "$(dirname "$out")"
    # through a temp file, so a failed decrypt leaves no empty certificate behind
    sops --decrypt --extract '["bench_ca_cert"]' "{{ shared_secrets }}/bench.yaml" > "$out.tmp"
    mv "$out.tmp" "$out"
    printf 'export RUSTYFARIAN_BENCH_CA=%q\n' "$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"

# write the cloud-init seed onto a freshly written card: `just pi-seed /Volumes/bootfs`
[group('bench-host')]
[positional-arguments]
pi-seed bootfs: deploy-collections
    #!/usr/bin/env bash
    set -euo pipefail
    [ -d "$1" ] || { echo "$1 is not a directory; is the card's bootfs mounted?" >&2; exit 1; }
    export PI_BOOTFS="$(cd "$1" && pwd)"   # absolute before the cd below
    cd "{{ ansible_dir }}"
    sops exec-env "{{ shared_secrets }}/pi.env" '{{ ansible }} ansible-playbook -i localhost, seed.yml'

# provision the bench host or upgrade `bench_version`, no OS upgrade: `just pi-provision --check --diff`, `-e bench_force_restart=true`
[group('bench-host')]
[positional-arguments]
pi-provision *args: deploy-lint
    cd "{{ ansible_dir }}" && {{ ansible }} ansible-playbook site.yml "$@"

# build a wheel of this checkout into tmp/dist-dev and print its path; its version must be a .devN one
[group('bench-host')]
dev-wheel:
    #!/usr/bin/env bash
    set -euo pipefail
    dist="{{ justfile_directory() }}/tmp/dist-dev"
    mkdir -p "$dist"
    rm -f "$dist"/*.whl
    uv build --wheel --quiet --out-dir "$dist" "{{ justfile_directory() }}" >&2
    wheel=$(ls "$dist"/*.whl)
    version=$(basename "$wheel" | cut -d- -f2)
    case "$version" in
        *.dev[0-9]*) echo "$wheel" ;;
        *) echo "dev-wheel wants a .devN version in pyproject.toml, not $version" >&2; exit 1 ;;
    esac

# provision the bench host with this checkout instead of a release tag, as its .devN version; run it twice for the changed=0 check
[group('bench-host')]
[positional-arguments]
pi-provision-dev *args: deploy-lint
    #!/usr/bin/env bash
    set -euo pipefail
    wheel=$("{{ just_executable() }}" --justfile "{{ justfile() }}" dev-wheel)
    version=$(basename "$wheel" | cut -d- -f2)
    cd "{{ ansible_dir }}"
    # JSON, not key=value, so a checkout path with a space stays one value
    vars=$(printf '{"bench_wheel": "%s", "bench_version": "v%s"}' "$wheel" "$version")
    {{ ansible }} ansible-playbook site.yml -e "$vars" "$@"

# provision with `apt full-upgrade`, refused while a job is unfinished; reports a pending reboot
[confirm("Run apt full-upgrade on the bench host?")]
[group('bench-host')]
[positional-arguments]
pi-upgrade *args: deploy-lint
    cd "{{ ansible_dir }}" && {{ ansible }} ansible-playbook site.yml -e base_full_upgrade=true "$@"

# apply the provisioning twice (it changes the host, unlike --check); the second run must change nothing
[group('bench-host')]
pi-idempotent: deploy-lint
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ ansible_dir }}"
    {{ ansible }} ansible-playbook site.yml
    out=$(ANSIBLE_NOCOLOR=1 {{ ansible }} ansible-playbook site.yml 2>&1) \
        || { echo "$out"; exit 1; }
    recap=$(sed -n '/PLAY RECAP/,$p' <<< "$out")
    echo "$recap"
    # every host line must be clean, not just one of them
    grep -q 'ok=' <<< "$recap" && ! grep -Eq '(changed|unreachable|failed)=[1-9]' <<< "$recap" \
        || { echo "the second run changed something" >&2; exit 1; }

# check the live bench (401 without a token, 404 with one, then detect) while no job runs: `just pi-check [token-name]`
[group('bench-host')]
[positional-arguments]
pi-check token="":
    @JUST="{{ just_executable() }}" ANSIBLE="{{ ansible }}" bash "{{ justfile_directory() }}/deploy/bench-client.sh" check "$@"

# submit to the provisioned bench with URL, CA and token from inventory and bench.yaml: `just pi-submit rustyfarian-network c3 hal_c3_connect_async --wait`
[group('bench-host')]
[positional-arguments]
pi-submit repo board example *args:
    @JUST="{{ just_executable() }}" ANSIBLE="{{ ansible }}" bash "{{ justfile_directory() }}/deploy/bench-client.sh" submit "$@"

# --- secrets: SOPS + age files in shared-secrets/ ------------------------------

# edit an encrypted file in $EDITOR; sops decrypts to a temp file and re-encrypts on save: `just secrets-edit bench.yaml`
[group('secrets')]
[positional-arguments]
secrets-edit file:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ secret_file_check }}
    mkdir -p -m 0700 "{{ shared_secrets }}"
    # 200 is sops' "File has not changed": an edit that changes nothing is fine
    sops "{{ shared_secrets }}/$1" || [ $? -eq 200 ]

# decrypt a file in shared-secrets/ IN PLACE: it is plaintext until `just encrypt <file>`
[group('secrets')]
[positional-arguments]
decrypt file:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ secret_file_check }}
    sops --decrypt --in-place "{{ shared_secrets }}/$1"
    echo "shared-secrets/$1 is PLAINTEXT now: edit it, then run 'just encrypt $1'" >&2

# encrypt a file in shared-secrets/ in place for the recipients in .sops.yaml (also the first time)
[group('secrets')]
[positional-arguments]
encrypt file:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ secret_file_check }}
    sops --encrypt --in-place "{{ shared_secrets }}/$1"

# fail on any file in shared-secrets/ that is not SOPS-encrypted or in a format this cannot check (needs no age key)
[group('secrets')]
secrets-check:
    bash "{{ justfile_directory() }}/deploy/secrets-check.sh" "{{ shared_secrets }}"

# re-encrypt every file in shared-secrets/ for the recipients now in .sops.yaml
[group('secrets')]
secrets-updatekeys:
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s nullglob
    files=("{{ shared_secrets }}"/*.env "{{ shared_secrets }}"/*.json "{{ shared_secrets }}"/*.yaml "{{ shared_secrets }}"/*.yml)
    [[ ${#files[@]} -gt 0 ]] || { echo "no secret files in shared-secrets/" >&2; exit 1; }
    for f in "${files[@]}"; do sops updatekeys --yes "$f"; done

# add a generated bearer token to shared-secrets/bench.yaml, via a pipe, never argv: `just bench-token-add laptop`
[group('secrets')]
[positional-arguments]
bench-token-add name:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ token_name_check }}
    openssl rand -hex 32 | sed 's/.*/"&"/' \
        | sops set --value-stdin "{{ shared_secrets }}/bench.yaml" "[\"bench_tokens\"][\"$1\"]"
