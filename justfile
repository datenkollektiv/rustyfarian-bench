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

# build the wheel and run it the way a consumer's uvx does: --help, then list a fixture consumer
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

# run the unit tests
test *args:
    uv run --locked pytest {{ args }}

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
    uv run --locked mypy src tests

# re-lock dependencies after a pyproject.toml change (e.g. a version bump)
lock:
    uv lock

# probe the connected boards
detect:
    uv run --locked rustyfarian-bench detect

# run a consumer's checks from here: `just bench rustyfarian-peripherals s3 b3f` (sibling name or absolute path)
bench repo target *args:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" run "{{ target }}" {{ args }}

# list a consumer's scenarios: `just bench-list rustyfarian-ws2812`
bench-list repo:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" list
