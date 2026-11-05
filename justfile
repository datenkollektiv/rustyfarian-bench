# rustyfarian-bench — guided hardware bench runner

# list recipes
default:
    @just --list

# format, lint, type-check and test (modifies files)
pre-commit: fmt verify

# non-modifying gate: lint, format check, type-check, tests
verify: lint fmt-check typecheck test

# run the unit tests
test:
    uv run --locked pytest

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
