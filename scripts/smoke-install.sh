#!/usr/bin/env bash
set -euo pipefail
# smoke-install.sh — build the wheel and run it like a consumer's uvx: --help, list a fixture
# consumer, and the `serve` and `kiosk` hints of the base wheel. Run through `just smoke-install`.
# Usage: scripts/smoke-install.sh (no arguments)

root=$(cd "$(dirname "$0")/.." && pwd)
dist="$root/tmp/dist"
cd "$root"
rm -rf "$dist"
uv build --wheel --out-dir "$dist"
wheel=("$dist"/rustyfarian_bench-*.whl)
[ -f "${wheel[0]}" ] || { echo "no wheel built in $dist" >&2; exit 1; }
cd "$dist"   # outside the project, so uvx cannot pick up the checkout
uvx --isolated --from "${wheel[0]}" rustyfarian-bench --help
out=$(uvx --isolated --from "${wheel[0]}" rustyfarian-bench --repo "$root/tests/fixtures/consumer" list)
echo "$out"
grep -qE "^s3/smoke +smoke_example$" <<< "$out"
# the base wheel has no web stack: `serve` must name the extra and exit 2
rc=0
out=$(uvx --isolated --from "${wheel[0]}" rustyfarian-bench serve 2>&1) || rc=$?
echo "$out"
[ "$rc" -eq 2 ] || { echo "serve exited $rc, expected 2" >&2; exit 1; }
grep -qF "rustyfarian-bench[serve]" <<< "$out"
# likewise `kiosk` without Pillow
rc=0
out=$(uvx --isolated --from "${wheel[0]}" rustyfarian-bench kiosk 2>&1) || rc=$?
echo "$out"
[ "$rc" -eq 2 ] || { echo "kiosk exited $rc, expected 2" >&2; exit 1; }
grep -qF "rustyfarian-bench[kiosk]" <<< "$out"
