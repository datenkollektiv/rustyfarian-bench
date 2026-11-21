#!/usr/bin/env bash
set -euo pipefail
# touch-probe.sh — show what the bench Pi's touch panel reports, as the kiosk's idle sleep would read it.
# Usage: bash touch-probe.sh 2>&1 | tee ~/touch-probe.log
#
# Read-only, and the kiosk keeps running: evdev lets several readers open a node at once. It finds
# the panel the way the kiosk does (/sys/class/input/event*/device/name), shows the node's owner and
# mode and the udev rules that set them, tries an open as the kiosk's user, then reads two windows
# of events, draining once a second as the kiosk does: 5 s untouched, 10 s of taps. It prints counts
# only; docs/features/archive/bench-kiosk-sleep-v1.md says what to expect. Run it as the admin user: reading the node and opening it as
# the kiosk's user go through `sudo -n` where needed, and are skipped when sudo asks for a password
# (run `sudo -v` first). It writes nothing; the log is wherever the tee above puts it.
# For a dry run elsewhere, SYSFS_INPUT, DEV_DIR, TOUCH_NAME and KIOSK_USER override /sys/class/input,
# /dev, "ADS7846 Touchscreen" (display_touch_name) and bench-display (display_user) in
# deploy/ansible/roles/display/defaults/main.yml.

usage() { sed -n '4s/^# //p' "$0" >&2; exit "${1:-2}"; }

case "${1:-}" in
    "") ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac

sysfs=${SYSFS_INPUT:-/sys/class/input}
devdir=${DEV_DIR:-/dev}
name=${TOUCH_NAME:-ADS7846 Touchscreen}
kiosk_user=${KIOSK_USER:-bench-display}
idle_s=5
taps_s=10
shopt -s nullglob

say() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
# the panel's IRQ line in /proc/interrupts, and its count summed over the CPUs
irq_count() {
    awk 'tolower($0) ~ /ads7846/ {n = 0; for (i = 2; i <= NF && $i ~ /^[0-9]+$/; i++) n += $i; print n; f = 1}
         END {if (!f) print "none"}' /proc/interrupts 2>/dev/null || echo "unknown"
}
# wait for the user where a terminal is there, else count down
ready() {
    if { true </dev/tty; } 2>/dev/null; then
        read -r -p "$1, then press Enter " _ </dev/tty || true
    else
        echo "$1; starting in 5 s" >&2
        sleep 5
    fi
}

# --- capture: drain the node once a second for N seconds, count events by type and code ---
capture() {
    ${runner[@]+"${runner[@]}"} python3 - "$1" "$2" <<'PY'
import collections
import os
import struct
import sys
import time

node, seconds = sys.argv[1], int(sys.argv[2])
# struct input_event on a 64-bit userland (aarch64): 24 bytes
EVENT = struct.Struct("@llHHi")
NAMES = {
    (0, 0): "EV_SYN SYN_REPORT",
    (0, 3): "EV_SYN SYN_DROPPED",
    (1, 330): "EV_KEY BTN_TOUCH",
    (3, 0): "EV_ABS ABS_X",
    (3, 1): "EV_ABS ABS_Y",
    (3, 24): "EV_ABS ABS_PRESSURE",
}
counts = collections.Counter()
presses = releases = wake_seconds = reads = odd_bytes = 0
try:
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
except OSError as e:
    sys.exit(f"open {node}: {e}")
try:
    for _ in range(seconds):
        time.sleep(1)
        woke = False
        while True:
            try:
                chunk = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                break
            if not chunk:
                break
            reads += 1
            whole = len(chunk) - len(chunk) % EVENT.size
            odd_bytes += len(chunk) - whole
            for _sec, _usec, etype, code, value in EVENT.iter_unpack(chunk[:whole]):
                counts[etype, code] += 1
                if (etype, code) == (1, 330):
                    presses += value == 1
                    releases += value == 0
                    woke = woke or value == 1
                elif (etype, code) == (0, 3):
                    woke = True
        wake_seconds += woke
except OSError as e:
    sys.exit(f"read {node}: {e}")
finally:
    os.close(fd)
print(f"event size: {EVENT.size} bytes; reads: {reads}; bytes not a whole event: {odd_bytes}")
print(f"events: {sum(counts.values())}")
for (etype, code), n in sorted(counts.items()):
    print(f"  type {etype} code {code} ({NAMES.get((etype, code), 'other')}): {n}")
print(f"BTN_TOUCH value 1: {presses}; value 0: {releases}; SYN_DROPPED: {counts[0, 3]}")
print(f"seconds with a press or SYN_DROPPED (kiosk wakes): {wake_seconds} of {seconds}")
PY
}

say "Host"
echo "model:  $(tr -d '\0' 2>/dev/null < /proc/device-tree/model || echo unknown)"
echo "kernel: $(uname -r)"
echo "os:     $(sed -n 's/^PRETTY_NAME="\(.*\)"$/\1/p' /etc/os-release 2>/dev/null || true)"
echo "framebuffers:"
for f in /sys/class/graphics/fb[0-9]*/name; do echo "  $(basename "$(dirname "$f")"): $(cat "$f")"; done

say "Display lines in /boot/firmware/config.txt"
grep -nE '^\s*dtoverlay' /boot/firmware/config.txt 2>/dev/null || echo "none"

say "\"$name\" in /proc/bus/input/devices"
awk -v n="N: Name=\"$name\"" 'BEGIN {RS = ""} index($0, n)' /proc/bus/input/devices 2>/dev/null \
    | grep . || echo "none"
echo "char-input major: $(awk '$2 == "input" {print $1}' /proc/devices 2>/dev/null || true)"

say "Input nodes as the kiosk finds them ($sysfs/event*/device/name)"
node=""
for e in $(for f in "$sysfs"/event[0-9]*; do basename "$f"; done | sort -V); do
    n=$(cat "$sysfs/$e/device/name" 2>/dev/null || echo "?")
    echo "$e: $n"
    if [ -z "$node" ] && [[ "$n" == *"$name"* ]]; then node="$devdir/input/$e"; fi
done
echo "kiosk's pick: ${node:-none}"

say "Display unit"
systemctl show rustyfarian-bench-display -p ActiveState -p ExecStart -p Environment -p DeviceAllow \
    --no-pager 2>&1 || true
journalctl -u rustyfarian-bench-display -b --no-pager 2>/dev/null | grep -iE 'touch|asleep' | tail -n 10 || true

say "udev rules touching the input subsystem"
grep -rn 'SUBSYSTEM=="input"' /usr/lib/udev/rules.d /etc/udev/rules.d 2>/dev/null \
    | grep -E 'GROUP|MODE|OWNER' || echo "none"
id "$kiosk_user" 2>&1 || true

if [ -z "$node" ]; then
    say "Result"
    echo "no input device named \"$name\"; permission and event checks skipped"
    exit 0
fi

say "Node $node"
stat -c '%a %U:%G %n' "$node" 2>&1 || true
if have udevadm; then
    udevadm info -q property -n "$node" 2>&1 | grep -E '^(DEVNAME|ID_INPUT|ID_PATH)=' || true
    udevadm info -a -n "$node" 2>&1 | grep -m 3 -E 'ATTRS?\{name\}' || true
fi

# outside the unit: this checks udev's owner and mode only, not the unit's DeviceAllow
say "Open as $kiosk_user"
if sudo -n true 2>/dev/null; then
    sudo -n -u "$kiosk_user" python3 -c '
import os, sys
try:
    os.close(os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK))
    print("open: ok")
except OSError as e:
    print(f"open: {e}")
' "$node" 2>&1 || true
else
    echo "skipped: sudo -n asks for a password (run sudo -v first)"
fi

# the reader: this user where the node is readable, else root through sudo -n
runner=()
if [ ! -r "$node" ]; then
    if sudo -n true 2>/dev/null; then
        runner=(sudo -n)
    else
        say "Result"
        echo "$node is not readable here and sudo -n asks for a password; event windows skipped (run sudo -v first)"
        exit 0
    fi
fi

say "Untouched window, $idle_s s"
ready "Do not touch the panel for $idle_s s"
idle_from=$(irq_count)
capture "$node" "$idle_s" || true
idle_to=$(irq_count)

say "Tap window, $taps_s s"
ready "Tap the panel a few times during the next $taps_s s"
taps_from=$(irq_count)
capture "$node" "$taps_s" || true
taps_to=$(irq_count)

say "ADS7846 interrupts, summed over the CPUs"
echo "untouched window: $idle_from -> $idle_to; tap window: $taps_from -> $taps_to"
grep -i ads7846 /proc/interrupts || echo "no ads7846 line in /proc/interrupts"

say "Done"
echo "Paste this whole log back."
