#!/usr/bin/env bash
set -euo pipefail
# display-probe.sh — tell which SPI panel sits on the bench Pi, on the running system, without a reboot.
# Usage: sudo bash display-probe.sh [--hold SECONDS] [--keep] [--force] [OVERLAY[,param=value...]...]
#
# Nothing on the panel is readable over software: the controller sits behind a write-only shift
# register. So this loads a display overlay at runtime (config.txt untouched), paints four colour
# bars with a black square in the framebuffer's top-left corner, asks what the panel shows, and
# unloads the overlay again. Default candidates: the stock `piscreen` overlay, fbtft variant first,
# then its `drm` variant. Name other overlays to try them in order, e.g. waveshare35b once its
# .dtbo sits in /boot/firmware/overlays/. (A), (B) and (C) are Waveshare's model letters, printed
# on the board. A `drm` candidate can stay white although it drives the panel: a raw write into a
# DRM framebuffer runs no modeset, and the modeset is what initialises the panel (bench Pi, 2026-11-20).
#   --hold N   do not ask; keep each pattern N seconds (without a terminal the default is 20)
#   --keep     leave the first overlay that loads in place, until the next reboot
#   --force    run although serve has unfinished jobs
# Copy it to the Pi and run it there: nothing persists, the next reboot forgets it all.
# For a dry run elsewhere, SYSFS_GRAPHICS, DEV_DIR and BENCH_OUTBOX override /sys/class/graphics,
# /dev and /srv/bench/outbox (bench_outbox in deploy/ansible/roles/bench/defaults/main.yml).

usage() { sed -n '4s/^# //p' "$0" >&2; exit "${1:-2}"; }

hold=""
keep=false
force=false
specs=()
while [ $# -gt 0 ]; do
    case "$1" in
        --hold) [ $# -ge 2 ] && [[ "$2" =~ ^[0-9]+$ ]] || usage; hold=$2; shift 2 ;;
        --keep) keep=true; shift ;;
        --force) force=true; shift ;;
        -h|--help) usage 0 ;;
        -*) usage ;;
        *) specs+=("$1"); shift ;;
    esac
done
[ ${#specs[@]} -gt 0 ] || specs=("piscreen,speed=16000000" "piscreen,drm,speed=16000000")

sysfs=${SYSFS_GRAPHICS:-/sys/class/graphics}
devdir=${DEV_DIR:-/dev}
outbox=${BENCH_OUTBOX:-/srv/bench/outbox}
fb_wait_s=6   # how long a loaded overlay gets to bring up its framebuffer
shopt -s nullglob

say() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
fb_list() { local f; for f in "$sysfs"/fb[0-9]*; do basename "$f"; done | sort; }
fb_describe() {
    local fb=$1
    printf '%s: %s, %s pixels, %s bpp\n' "$fb" "$(cat "$sysfs/$fb/name")" \
        "$(tr ',' 'x' < "$sysfs/$fb/virtual_size")" "$(cat "$sysfs/$fb/bits_per_pixel")"
}
drm_list() {
    local c d
    for c in /sys/class/drm/card[0-9]*; do
        [[ "$(basename "$c")" == *-* ]] && continue   # connectors, not cards
        d=$(basename "$(readlink -f "$c/device/driver" 2>/dev/null || echo unknown)")
        printf '%s: %s\n' "$(basename "$c")" "$d"
    done
}

# an overlay this run loaded and has not unloaded yet; the trap takes it out on any exit, Ctrl-C included
loaded=""
cleanup() {
    [ -n "$loaded" ] || return 0
    echo "unloading $loaded" >&2
    dtoverlay -r "$loaded" || echo "could not unload $loaded; it was loaded at runtime only, a reboot clears it" >&2
}
trap cleanup EXIT

# --- paint: four bars red, green, blue, white, and a 40 px black square at the top-left ---
paint() {
    python3 - "$1" "$sysfs" "$devdir" <<'PY'
import pathlib
import sys

fb, sysfs, devdir = sys.argv[1:4]
s = pathlib.Path(sysfs, fb)
width, height = (int(v) for v in s.joinpath("virtual_size").read_text().split(","))
bpp = int(s.joinpath("bits_per_pixel").read_text())
stride = int(s.joinpath("stride").read_text())
if bpp not in (16, 32):
    sys.exit(f"{fb}: {bpp} bits per pixel, expected 16 (RGB565) or 32 (XRGB8888)")


def pixel(r: int, g: int, b: int) -> bytes:
    if bpp == 16:
        return (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)).to_bytes(2, "little")
    return bytes((b, g, r, 0))


bars = [pixel(255, 0, 0), pixel(0, 255, 0), pixel(0, 0, 255), pixel(255, 255, 255)]
black = pixel(0, 0, 0)
bar = max(width // 4, 1)
frame = bytearray()
for y in range(height):
    row = bytearray()
    for x in range(width):
        row += black if x < 40 and y < 40 else bars[min(x // bar, 3)]
    row += bytes(max(stride - len(row), 0))
    frame += row
pathlib.Path(devdir, fb).write_bytes(bytes(frame))
print(f"painted {fb}: {width}x{height}, {bpp} bpp, stride {stride}")
PY
}

ask() {
    local answer
    if [ -n "$hold" ]; then
        echo "holding the pattern for $hold s, look at the panel" >&2
        sleep "$hold"
        echo "-"
        return
    fi
    echo >&2
    echo "Look at the panel. Left to right it should show red, green, blue, white, and a black square" >&2
    echo "in one corner. Answer with one letter:" >&2
    echo "  b  bars in that order, square top-left      r  white, blue, green, red, square bottom-right" >&2
    echo "  i  cyan, magenta, yellow, black (inverted)  s  blue, green, red, white (swapped)" >&2
    echo "  w  still white                              o  something else" >&2
    read -r -p "> " answer </dev/tty || answer="-"
    echo "${answer:--}"
}

[ "$(id -u)" -eq 0 ] || { echo "run it as root: sudo bash $0 ..." >&2; exit 2; }
for tool in dtoverlay python3; do
    have "$tool" || { echo "$tool is missing; dtoverlay comes with the raspi-utils packages" >&2; exit 2; }
done
# no terminal to answer on (piped through ssh without -t): hold each pattern instead of asking
if [ -z "$hold" ] && ! { true </dev/tty; } 2>/dev/null; then
    hold=20
    echo "no terminal: each pattern stays 20 s; watch the panel (or pass --hold N)" >&2
fi

say "Host"
echo "model:  $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "kernel: $(uname -r)"
echo "os:     $(sed -n 's/^PRETTY_NAME="\(.*\)"$/\1/p' /etc/os-release 2>/dev/null || true)"

say "Display lines in /boot/firmware/config.txt"
grep -nE '^\s*(dtoverlay|dtparam|display_|hdmi_|framebuffer|max_framebuffers)' /boot/firmware/config.txt \
    || echo "none"

say "Overlays loaded now"
dtoverlay -l 2>&1 || true

say "Framebuffers and DRM cards now"
for fb in $(fb_list); do fb_describe "$fb"; done
drm_list
echo "spi devices: $(cd "$devdir" && echo spidev* || true)"
echo "modules: $(lsmod | awk '/fbtft|ili94|ads7846|spi_/ {print $1}' | tr '\n' ' ')"

if have pinctrl; then
    say "Levels on the panel's GPIOs (7-11 SPI, 17 touch irq, 18 and 22 backlight on some models, 24 dc, 25 reset)"
    pinctrl get 7,8,9,10,11,17,18,22,24,25 2>&1 || true
fi

# --- an overlay already in place: paint on it, load nothing ---
present=""
for fb in $(fb_list); do
    if grep -qi 'ili9486' "$sysfs/$fb/name"; then present=$fb; fi
done
if [ -n "$present" ]; then
    say "A panel driver is already bound to $present; painting it, loading nothing"
    paint "$present"
    answer=$(ask)
    echo "answer: $answer"
    exit 0
fi

# --- the job guard: the same rule as deploy/ansible/roles/bench/tasks/busy.yml, unreadable counts as busy ---
busy=()
if systemctl is-active --quiet rustyfarian-bench 2>/dev/null && [ -d "$outbox" ]; then
    mapfile -t busy < <(python3 - "$outbox" <<'PY'
import json
import pathlib
import sys

for p in sorted(pathlib.Path(sys.argv[1]).glob("*/job.json")):
    try:
        job = json.loads(p.read_text())
    except (OSError, ValueError):
        job = None
    if not (isinstance(job, dict) and job.get("status") == "done"):
        print(p.parent.name)
PY
    )
fi
if [ ${#busy[@]} -gt 0 ] && ! $force; then
    echo "serve has unfinished jobs: ${busy[*]}. Wait, or rerun with --force." >&2
    exit 1
fi

# --- try the candidates in order ---
results=()
for spec in "${specs[@]}"; do
    name=${spec%%,*}
    say "Loading $spec"
    before=$(fb_list)
    # a marker in the kernel log, so the lines below are this candidate's even if the ring buffer wrapped
    marker="display-probe[$$]: loading $spec"
    { echo "$marker" > /dev/kmsg; } 2>/dev/null || true
    if ! dtoverlay "$spec"; then
        results+=("$spec: did not load")
        continue
    fi
    loaded=$name
    new=""
    for _ in $(seq 1 $((fb_wait_s * 2))); do
        sleep 0.5
        new=$(comm -13 <(echo "$before") <(fb_list) | head -n 1)
        [ -n "$new" ] && break
    done
    echo "kernel said:"
    dmesg 2>/dev/null | awk -v m="$marker" 'index($0, m) {f = 1; next} f' \
        | grep -iE 'ili9486|fbtft|piscreen|spi|drm|ads7846|graphics|fb[0-9]' | sed 's/^/  /' || true
    drm_list | grep -i 'ili9486' | sed 's/^/  drm /' || true
    if [ -z "$new" ]; then
        verdict="loaded, but no framebuffer appeared within $fb_wait_s s"
    else
        fb_describe "$new"
        if paint "$new"; then
            answer=$(ask)
            letter=${answer,,}
            case "${letter:0:1}" in
                b) verdict="bars as expected: an ILI9486 on the (A) wiring; '$spec' is the overlay for the role" ;;
                r) verdict="bars reversed: an ILI9486 on the (A) wiring, upside down; '$spec' plus rotate=180" ;;
                i) verdict="inverted colours: ILI9486 with another init, most likely the (B); try waveshare35b" ;;
                s) verdict="red and blue swapped: driven by '$spec'; the role needs the bgr setting" ;;
                w) verdict="still white: not driven by '$spec'" ;;
                -) verdict="${hold:+shown for $hold s, }judge by eye" ;;
                *) verdict="other: $answer" ;;
            esac
        else
            verdict="framebuffer $new appeared, but painting it failed (see above)"
        fi
    fi
    results+=("$spec: $verdict")
    if $keep; then
        echo "kept $name loaded (until the next reboot)"
        loaded=""
        break
    fi
    if dtoverlay -r "$name"; then
        echo "unloaded $name"
        loaded=""
    else
        # the next candidate shares SPI0 CS0 with this one, so stop here; the trap would only fail again
        echo "could not unload $name; it was loaded at runtime only, a reboot clears it" >&2
        loaded=""
        break
    fi
done

say "Result"
printf '%s\n' "${results[@]}"
cat <<'EOF'

Reading it:
  bars as expected   -> the (A), or a compatible panel: the role uses the stock piscreen overlay
  bars reversed      -> the same panel mounted the other way round: add rotate=180
  inverted colours   -> the (B): copy Waveshare's waveshare35b.dtbo to /boot/firmware/overlays/, rerun with it
  white on all       -> the (C), or not an ILI9486: likewise with waveshare35c
  the black square   -> marks the framebuffer's top-left corner, so it tells the rotation the role needs
EOF
