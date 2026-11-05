"""Find the board under test: poll serial ports, then ask espflash which chip answers."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from serial.tools import list_ports

from .config import Board
from .ui import info, warn

# Espressif native USB Serial/JTAG, plus the common USB-UART bridges on dev boards.
KNOWN_VIDS = {0x303A, 0x10C4, 0x1A86, 0x0403}
PORT_PREFIXES = ("/dev/cu.usbmodem", "/dev/cu.usbserial", "/dev/ttyUSB", "/dev/ttyACM")

ESPFLASH_ENV = {**os.environ, "ESPFLASH_SKIP_UPDATE_CHECK": "1"}


@dataclass(frozen=True)
class Device:
    port: str
    chip: str
    mac: str | None


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


def probe(port: str) -> Device | None:
    try:
        out = subprocess.run(
            ["espflash", "board-info", "--port", port, "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=30,
            env=ESPFLASH_ENV,
        )
    except subprocess.TimeoutExpired:
        return None
    chip, mac = parse_board_info(out.stdout + out.stderr)
    return Device(port=port, chip=chip, mac=mac) if chip else None


def matches(board: Board, device: Device) -> bool:
    if device.chip != normalize_chip(board.chip):
        return False
    return board.mac is None or device.mac == board.mac.lower()


def wait_for(board: Board, port: str | None = None, poll: float = 0.5) -> Device:
    """Block until a matching board answers; Ctrl-C aborts."""
    info(f"Checking for {board.name} ({board.chip})...")
    asked = False
    rejected: set[str] = set()
    while True:
        ports = [port] if port else candidate_ports()
        ports = [p for p in ports if os.path.exists(p)]
        # Forget rejected ports that went away, so a swapped board is probed again.
        rejected &= set(ports)
        for p in ports:
            if p in rejected:
                continue
            device = probe(p)
            if device and matches(board, device):
                info(f"Detected {device.chip} on {device.port} (MAC {device.mac})")
                return device
            rejected.add(p)
            found = f"{device.chip} (MAC {device.mac})" if device else "no ESP chip"
            warn(f"{p}: found {found}, expected {board.name} — swap the board")
        if not ports and not asked:
            info(f"Please plug in the test device: {board.name}")
            asked = True
        time.sleep(poll)
