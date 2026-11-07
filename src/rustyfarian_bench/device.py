"""Find the board under test: poll serial ports, then ask espflash which chip answers."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from serial.tools import list_ports

from .config import Board
from .ui import info, warn

# Espressif native USB Serial/JTAG, plus the common USB-UART bridges on dev boards.
KNOWN_VIDS = {0x303A, 0x10C4, 0x1A86, 0x0403}
PORT_PREFIXES = ("/dev/cu.usbmodem", "/dev/cu.usbserial", "/dev/ttyUSB", "/dev/ttyACM")

# espflash parses this as a clap bool: only "true" / "false" are accepted, not "1".
ESPFLASH_ENV = {**os.environ, "ESPFLASH_SKIP_UPDATE_CHECK": "true"}


@dataclass(frozen=True)
class Device:
    port: str
    chip: str
    mac: str | None


class MissingTool(RuntimeError):
    """A required external program cannot be run; no later example can fare better."""


class PortRequired(RuntimeError):
    """An undetected board's port cannot be inferred; only `--port` can settle it."""


def missing_tool(tool: str, e: OSError) -> MissingTool:
    """Build the one-line message for a `tool` that is absent or not executable."""
    msg = f"cannot run {tool!r} ({e.strerror or e})"
    if tool == "espflash" and isinstance(e, FileNotFoundError):
        msg += "; install it with `cargo install espflash`"
    return MissingTool(msg)


def candidate_ports() -> list[str]:
    ports = []
    for p in list_ports.comports():
        if p.vid in KNOWN_VIDS or p.device.startswith(PORT_PREFIXES):
            # macOS lists each device as tty.* and cu.*; cu.* is the one to open.
            if p.device.startswith("/dev/tty.") and os.path.exists(
                p.device.replace("/dev/tty.", "/dev/cu.")
            ):
                continue
            ports.append(p.device)
    return sorted(ports)


def normalize_chip(raw: str) -> str:
    """`ESP32-S3`, `esp32s3 (revision v0.2)` -> `esp32s3`."""
    return raw.split("(")[0].strip().lower().replace("-", "")


def parse_board_info(text: str) -> tuple[str | None, str | None]:
    chip = re.search(r"Chip type:\s*(.+)", text)
    mac = re.search(r"MAC address:\s*([0-9A-Fa-f:]{17})", text)
    return (
        normalize_chip(chip.group(1)) if chip else None,
        mac.group(1).lower() if mac else None,
    )


# Another process holds the port (a parallel bench session, a running flash, a
# monitor): espflash fails to take the lock before it touches the chip.
BUSY = "port held by another process"
_BUSY_MARKERS = ("unable to acquire exclusive lock", "resource busy")


def probe_error(text: str) -> str:
    """Condense espflash's failure output to one line; `BUSY` for a locked port."""
    lowered = text.lower()
    if any(m in lowered for m in _BUSY_MARKERS):
        return BUSY
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "error" in ln.lower()]
    return (errors or lines or ["no output"])[-1]


def probe(port: str) -> tuple[Device | None, str]:
    """Ask espflash which chip answers on `port`; on failure return espflash's error.

    Raises `MissingTool` when espflash itself cannot be run.
    """
    try:
        out = subprocess.run(
            ["espflash", "board-info", "--port", port, "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=30,
            env=ESPFLASH_ENV,
        )
    except subprocess.TimeoutExpired:
        return None, "espflash board-info timed out"
    except (FileNotFoundError, PermissionError) as e:
        # Not a per-port error: `wait_for` would retry it forever.
        raise missing_tool("espflash", e) from e
    text = out.stdout + out.stderr
    chip, mac = parse_board_info(text)
    if chip:
        return Device(port=port, chip=chip, mac=mac), ""
    return None, probe_error(text)


def matches(board: Board, device: Device) -> bool:
    if device.chip != normalize_chip(board.chip):
        return False
    return board.mac is None or device.mac == board.mac.lower()


SLEEP_HINT = (
    "No ESP answers on the port. If the board is in deep sleep (native USB is off while it "
    "sleeps), put it in download mode: hold BOOT, tap RESET, release BOOT. Or replug it."
)


def wait_for(
    board: Board,
    port: str | None = None,
    poll: float = 0.25,
    ports_fn: Callable[[], list[str]] = candidate_ports,
    probe_fn: Callable[[str], tuple[Device | None, str]] = probe,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Device:
    """Block until a matching board answers; Ctrl-C aborts.

    Raises `MissingTool` when espflash cannot be run.

    A port with the wrong chip is skipped until it disappears (board swapped).
    A port held by another process (a parallel session) is skipped while it stays
    held and never counts as a silent board; with `port` given, that means waiting.
    The lock is only held while a session reads its port, so two boards with the
    same chip should be told apart by `mac` in bench.toml.
    A port where no chip answers is retried: a sleeping or resetting board
    re-enumerates and may answer on a later attempt.
    """
    info(f"Checking for {board.name} ({board.chip})...")
    asked = False
    hinted = False
    wrong_chip: set[str] = set()
    busy: set[str] = set()
    silent: dict[str, str] = {}  # port -> last error already reported
    misses = 0
    while True:
        ports = [port] if port else ports_fn()
        ports = [p for p in ports if os.path.exists(p)]
        # Forget ports that went away, so a swapped or replugged board is probed again.
        wrong_chip &= set(ports)
        busy &= set(ports)
        silent = {p: e for p, e in silent.items() if p in ports}
        for p in ports:
            if p in wrong_chip:
                continue
            device, error = probe_fn(p)
            if device and matches(board, device):
                info(f"Detected {device.chip} on {device.port} (MAC {device.mac})")
                return device
            if error == BUSY:
                if p not in busy:
                    busy.add(p)
                    warn(f"{p}: in use by another process — waiting until it is released")
                continue
            if p in busy:
                busy.discard(p)
                silent.pop(p, None)
            if device:
                wrong_chip.add(p)
                warn(
                    f"{p}: found {device.chip} (MAC {device.mac}), "
                    f"expected {board.name} — swap the board"
                )
                continue
            misses += 1
            if silent.get(p) != error:
                warn(f"{p}: no answer yet ({error}) — retrying")
                silent[p] = error
            if misses >= 2 and not hinted:
                info(SLEEP_HINT)
                hinted = True
        if not ports and not asked:
            info(f"Please plug in the test device: {board.name}")
            asked = True
        sleep_fn(poll)
