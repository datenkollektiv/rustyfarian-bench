# rustyfarian-bench — guided hardware bench runner

# list recipes
default:
    @just --list

# format, lint, type-check and test (modifies files)
pre-commit: fmt verify

# non-modifying gate: lint, format check, type-check, tests
verify: lint fmt-check typecheck test

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

# probe the connected boards
detect:
    uv run --locked rustyfarian-bench detect

# run a consumer's checks from here: `just bench rustyfarian-peripherals s3 b3f` (sibling name or absolute path)
bench repo target *args:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" run "{{ target }}" {{ args }}

# list a consumer's scenarios: `just bench-list rustyfarian-ws2812`
bench-list repo:
    uv run --locked rustyfarian-bench --repo "{{ if repo =~ '^/' { repo } else { justfile_directory() / '..' / repo } }}" list
