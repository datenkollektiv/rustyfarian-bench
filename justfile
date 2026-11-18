# rustyfarian-bench — guided hardware bench runner

# list recipes
default:
    @just --list

# --- dev: the Python package itself -------------------------------------------

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
    #!/usr/bin/env bash
    set -euo pipefail
    dist="{{ justfile_directory() }}/tmp/dist"
    rm -rf "$dist"
    uv build --wheel --out-dir "$dist"
    wheel=("$dist"/rustyfarian_bench-*.whl)
    [ -f "${wheel[0]}" ] || { echo "no wheel built in $dist" >&2; exit 1; }
    cd "$dist"   # outside the project, so uvx cannot pick up the checkout
    uvx --isolated --from "${wheel[0]}" rustyfarian-bench --help
    out=$(uvx --isolated --from "${wheel[0]}" rustyfarian-bench \
        --repo "{{ justfile_directory() }}/tests/fixtures/consumer" list)
    echo "$out"
    grep -qE "^s3/smoke +smoke_example$" <<< "$out"
    # the base wheel has no web stack: `serve` must name the extra and exit 2
    rc=0
    out=$(uvx --isolated --from "${wheel[0]}" rustyfarian-bench serve 2>&1) || rc=$?
    echo "$out"
    [ "$rc" -eq 2 ] || { echo "serve exited $rc, expected 2" >&2; exit 1; }
    grep -qF "rustyfarian-bench[serve]" <<< "$out"

# run the unit tests (all extras, so the serve tests can import fastapi)
[group('dev')]
test *args:
    uv run --locked --all-extras pytest {{ args }}

# lint with ruff
[group('dev')]
lint:
    uv run --locked ruff check

# format with ruff
[group('dev')]
fmt:
    uv run --locked ruff format

# check formatting without changing files
[group('dev')]
fmt-check:
    uv run --locked ruff format --check

# strict mypy
[group('dev')]
typecheck:
    uv run --locked --all-extras mypy src tests

# run the CI workflow locally via act (requires Docker + act): `just act` or `just act --matrix python:3.12`
[group('dev')]
act *args:
    act -j verify --pull=false -P ubuntu-latest=catthehacker/ubuntu:act-latest {{ args }}

# remove scratch files, build output and tool caches; keeps .venv and release/
[group('dev')]
clean:
    cd "{{ justfile_directory() }}" && rm -rf tmp dist .pytest_cache .mypy_cache .ruff_cache

# re-lock dependencies after a pyproject.toml change (e.g. a version bump), then sync .venv to it
[group('dev')]
lock:
    uv lock
    uv sync --locked

# --- bench: run consumers' checks from this checkout --------------------------

# serve the remote bench from here, outbox in tmp/: `just serve --port 8001`
[group('bench')]
serve *args:
    uv run --locked --extra serve rustyfarian-bench serve --outbox tmp/outbox {{ args }}

# probe the connected boards
[group('bench')]
detect:
    uv run --locked rustyfarian-bench detect

# run a consumer's checks from here: `just bench rustyfarian-peripherals s3 b3f` (sibling name or absolute path)
[group('bench')]
[positional-arguments]
bench repo target *args:
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$1; [[ "$repo" == /* ]] || repo="{{ justfile_directory() }}/../$repo"
    shift
    uv run --locked rustyfarian-bench --repo "$repo" run "$@"

# list a consumer's scenarios: `just bench-list rustyfarian-ws2812`
[group('bench')]
[positional-arguments]
bench-list repo:
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$1; [[ "$repo" == /* ]] || repo="{{ justfile_directory() }}/../$repo"
    uv run --locked rustyfarian-bench --repo "$repo" list

# submit a consumer's check to a remote bench (URL from $RUSTYFARIAN_BENCH_URL or --bench): `just submit rustyfarian-network c3 hal_c3_connect_async --wait`
[group('bench')]
[positional-arguments]
submit repo board example *args:
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$1; [[ "$repo" == /* ]] || repo="{{ justfile_directory() }}/../$repo"
    shift
    uv run --locked rustyfarian-bench --repo "$repo" submit "$@"

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

# lint deploy/: yamllint, shellcheck, syntax check, ansible-lint, and the cloud-init seed rendered from fixture values
[group('bench-host')]
deploy-lint: deploy-collections
    ANSIBLE="{{ ansible }}" ANSIBLE_CORE="{{ ansible_core }}" ANSIBLE_LINT="{{ ansible_lint }}" \
        YAMLLINT="{{ yamllint }}" SHELLCHECK="{{ shellcheck }}" CHECK_JSONSCHEMA="{{ check_jsonschema }}" \
        CLOUD_INIT_SCHEMAS="{{ cloud_init_schemas }}" bash "{{ justfile_directory() }}/deploy/lint.sh"

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

# provision with `apt full-upgrade`, refused while a job is unfinished; reports a pending reboot
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
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s nullglob dotglob
    bad=0
    for f in "{{ shared_secrets }}"/*; do
        n=${f##*/}
        if [ ! -f "$f" ]; then
            echo "UNEXPECTED: shared-secrets/$n is not a regular file" >&2; bad=1; continue
        fi
        # encrypted dotenv carries a sops_version= line, JSON a top-level "sops" object, YAML a sops: key
        case "$n" in
            *.env) grep -q '^sops_version=' "$f" ;;
            *.json) grep -q '"sops"[[:space:]]*:' "$f" ;;
            *.yaml | *.yml) grep -q '^sops:' "$f" ;;
            *) echo "UNSUPPORTED: shared-secrets/$n: only .env, .json, .yaml and .yml can be checked" >&2
               bad=1; continue ;;
        esac || { echo "PLAINTEXT: shared-secrets/$n is not encrypted; run 'just encrypt $n'" >&2; bad=1; }
    done
    exit "$bad"

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
