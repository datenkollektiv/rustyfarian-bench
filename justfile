# rustyfarian-bench — guided hardware bench runner

# list recipes
default:
    @just --list

# format, lint, type-check and test (modifies files)
pre-commit: fmt verify

# non-modifying gate: lint, format check, type-check, tests
verify: lint fmt-check typecheck test

# run the verify gate on another Python in a scratch venv: `just verify-python 3.12`
verify-python version:
    UV_PYTHON="{{ version }}" UV_PROJECT_ENVIRONMENT="tmp/venv-py{{ version }}" "{{ just_executable() }}" verify

# build the wheel and run it like a consumer's uvx: --help, list a fixture, the `serve` hint
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
test *args:
    uv run --locked --all-extras pytest {{ args }}

# lint with ruff
lint:
    uv run --locked ruff check

# format with ruff
fmt:
    uv run --locked ruff format

# check formatting without changing files
fmt-check:
    uv run --locked ruff format --check

# strict mypy
typecheck:
    uv run --locked --all-extras mypy src tests

# run the CI workflow locally via act (requires Docker + act): `just act` or `just act --matrix python:3.12`
act *args:
    act -j verify --pull=false -P ubuntu-latest=catthehacker/ubuntu:act-latest {{ args }}

# remove scratch files, build output and tool caches; keeps .venv and release/
clean:
    cd "{{ justfile_directory() }}" && rm -rf tmp dist .pytest_cache .mypy_cache .ruff_cache

# re-lock dependencies after a pyproject.toml change (e.g. a version bump)
lock:
    uv lock

# serve the remote bench from here, outbox in tmp/: `just serve --port 8001`
serve *args:
    uv run --locked --extra serve rustyfarian-bench serve --outbox tmp/outbox {{ args }}

# probe the connected boards
detect:
    uv run --locked rustyfarian-bench detect

# run a consumer's checks from here: `just bench rustyfarian-peripherals s3 b3f` (sibling name or absolute path)
bench repo target *args:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" run "{{ target }}" {{ args }}

# list a consumer's scenarios: `just bench-list rustyfarian-ws2812`
bench-list repo:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" list

# submit a consumer's check to a remote bench: `just submit rustyfarian-network c3 hal_c3_connect_async --bench http://pi.local:8000 --wait`
submit repo board example *args:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" submit "{{ board }}" "{{ example }}" {{ args }}
