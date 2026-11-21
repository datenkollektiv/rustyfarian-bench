#!/usr/bin/env bash
set -euo pipefail
# display-smoke.sh — render each kiosk screen (idle, running, queued, long names, stale, stopped,
# maintenance) and the farewell into tmp/display/ from fixture status files, with a fake framebuffer
# plus PNGs, so a layout change is seen before it reaches the Pi. Run through `just display-smoke`;
# needs the network for Pillow until it is cached.
# Usage: deploy/display-smoke.sh (no arguments)

root=$(cd "$(dirname "$0")/.." && pwd)
dir="$root/tmp/display"
# the package with the kiosk extra in its own tool venv on the Pi's Python, as the Pi installs it;
# kept under tmp/display, so neither .venv nor ~/.local is touched; --reinstall-package picks up src/ edits and reuses the cached Pillow wheel
UV_TOOL_DIR="$dir/tools" UV_TOOL_BIN_DIR="$dir/bin" uv tool install --quiet --reinstall-package rustyfarian-bench --python 3.13 \
    "rustyfarian-bench[kiosk] @ $(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$root")"
bench="$dir/bin/rustyfarian-bench"
python="$dir/tools/rustyfarian-bench/bin/python"
# the panel's geometry, as fbtft reports it: the layout's size at 16 bits per pixel
read -r width height < <("$python" -c 'from rustyfarian_bench.kiosk_layout import HEIGHT, WIDTH; print(WIDTH, HEIGHT)')
stride=$((width * 2))
mkdir -p "$dir/sys/fb1" "$dir/dev" "$dir/status" "$dir/png"
rm -f "$dir"/*.png "$dir"/*.fb "$dir"/*.log "$dir"/status/*.json "$dir"/png/*.png
: > "$dir/dev/fb1"   # the kiosk opens the node without O_CREAT, as it must on the Pi
printf 'ili9486drmfb\n' > "$dir/sys/fb1/name"
printf '%s,%s\n' "$width" "$height" > "$dir/sys/fb1/virtual_size"
printf '16\n' > "$dir/sys/fb1/bits_per_pixel"
printf '%s\n' "$stride" > "$dir/sys/fb1/stride"
# one status file per screen, as `serve --status-file` writes it (schema 1), timed relative to now;
# "stopped" is the missing file
"$python" - "$dir/status" <<'EOF'
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

out, now = Path(sys.argv[1]), datetime.now(UTC)

def ago(seconds):
    return (now - timedelta(seconds=seconds)).isoformat(timespec="seconds")

def record(state="idle", updated=0, queued=0, running=None, history=()):
    return {"schema": 1, "state": state, "updated": ago(updated), "version": "0.0.0-smoke",
            "queued": queued, "running": running, "history": list(history)}

def verdict(verdict, board, example, finished):
    return {"verdict": verdict, "board": board, "example": example, "finished": ago(finished)}

history = [
    verdict("pass", "c3-mini-1", "hal_c3_connect_async", 20),
    verdict("fail", "s3-devkit", "wifi_scan", 7 * 60),
    verdict("error", "c3-mini-1", "hal_c3_mqtt", 3 * 3600),
    verdict("unverified", "c6-devkit", "ws2812_rainbow", 26 * 3600),
    verdict("pass", "s3-devkit", "blinky", 3 * 86400),
]
running = {"board": "c3-mini-1", "example": "hal_c3_connect_async", "started": ago(95),
           "step": 2, "total": 4}
long_board, long_example = "esp32-c3-super-mini-on-the-pogo-rig", "hal_c3_connect_async_with_retries_and_backoff"
cases = {
    "idle": record(history=history),
    "running": record("running", running=running, history=history),
    # before the first step line, with more jobs waiting
    "queued": record("running", queued=2, running={**running, "step": None, "total": None},
                     history=history),
    "long-names": record("running", running={"board": long_board, "example": long_example,
                                             "started": ago(3725), "step": 11, "total": 12},
                         history=[verdict("pass", long_board, long_example, 10)] + history[:4]),
    "stale": record(updated=5 * 60, history=history),
    "maintenance": record("maintenance", history=history),
}
for name, data in cases.items():
    (out / f"{name}.json").write_text(json.dumps(data) + "\n")
EOF
export BENCH_SYSFS_GRAPHICS="$dir/sys" BENCH_DEV_DIR="$dir/dev" BENCH_ADDRESS=bench.example:8443
# case:the screen the kiosk must say it drew
cases=(idle:idle running:running queued:running long-names:running stale:stale stopped:stopped maintenance:maintenance)
names=()
for case in "${cases[@]}"; do
    name=${case%%:*} view=${case#*:}
    names+=("$name")
    if ! "$bench" kiosk --once --status-file "$dir/status/$name.json" --png-dir "$dir/png" 2> "$dir/$name.log"; then
        cat "$dir/$name.log" >&2; echo "$name: the kiosk failed" >&2; exit 1
    fi
    grep -q "drawn on fb1: $view\$" "$dir/$name.log" || { cat "$dir/$name.log" >&2; echo "$name: expected the $view screen" >&2; exit 1; }
    [ -s "$dir/png/kiosk.png" ] || { echo "$name: no PNG was written" >&2; exit 1; }
    mv "$dir/png/kiosk.png" "$dir/$name.png"
    cp "$dir/dev/fb1" "$dir/$name.fb"
done
"$python" - "$dir" "$((stride * height))" "${names[@]}" <<'EOF'
import sys
from pathlib import Path
root, size = Path(sys.argv[1]), int(sys.argv[2])
frames = {name: (root / f"{name}.fb").read_bytes() for name in sys.argv[3:]}
for name, data in frames.items():
    assert len(data) == size, f"{name}: {len(data)} bytes, expected one RGB565 frame of {size}"
    assert any(data), f"{name}: nothing was drawn"
assert frames["idle"] != frames["running"], "idle and running draw the same frame"
assert frames["stale"] != frames["idle"], "stale and idle draw the same frame"
# queued and long-names share the running view, but their records differ
assert len(set(frames.values())) == len(frames), "two different screens drew the same frame"
print(f"ok -- {len(frames)} screens drawn; see tmp/display/*.png (Pillow's fallback font without DejaVu)")
EOF
# the stop path, as at shutdown: the kiosk draws the farewell over the idle screen on SIGTERM
# the fixtures were timed up front: refresh the idle one so seven slow cases cannot make it stale
"$python" - "$dir/status/idle.json" <<'EOF'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
path.write_text(json.dumps(data) + "\n")
EOF
"$bench" kiosk --status-file "$dir/status/idle.json" --png-dir "$dir/png" 2> "$dir/service.log" &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT   # no orphaned kiosk on Ctrl-C or an early exit
for _ in $(seq 50); do grep -q 'drawn on fb1: idle$' "$dir/service.log" && break; sleep 0.2; done
if ! grep -q 'drawn on fb1: idle$' "$dir/service.log"; then
    kill "$pid" 2>/dev/null || true; cat "$dir/service.log" >&2; echo "the kiosk never drew the idle screen" >&2; exit 1
fi
cp "$dir/dev/fb1" "$dir/before-stop.fb"
kill -TERM "$pid"
wait "$pid" || { cat "$dir/service.log" >&2; echo "the kiosk did not stop cleanly" >&2; exit 1; }
trap - EXIT
[ -s "$dir/png/kiosk-farewell.png" ] || { echo "no farewell PNG was written" >&2; exit 1; }
mv "$dir/png/kiosk-farewell.png" "$dir/farewell.png"
if cmp -s "$dir/before-stop.fb" "$dir/dev/fb1"; then echo "no farewell was drawn on stop" >&2; exit 1; fi
echo "ok -- a stop draws the farewell; see tmp/display/farewell.png"
