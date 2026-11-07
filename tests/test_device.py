from __future__ import annotations

from pathlib import Path

import pytest

from rustyfarian_bench import device as device_mod
from rustyfarian_bench.config import Board
from rustyfarian_bench.device import (
    BUSY,
    SLEEP_HINT,
    Device,
    matches,
    normalize_chip,
    parse_board_info,
    probe_error,
    wait_for,
)

BOARD_INFO = """
[2026-11-05T10:00:00Z INFO ] Serial port: '/dev/cu.usbmodem2101'
[2026-11-05T10:00:00Z INFO ] Connecting...
[2026-11-05T10:00:01Z INFO ] Using flash stub
Chip type:         esp32s3 (revision v0.2)
Crystal frequency: 40 MHz
Flash size:        16MB
Features:          WiFi, BLE
MAC address:       DC:54:75:C8:12:34
"""


def test_parse_board_info() -> None:
    assert parse_board_info(BOARD_INFO) == ("esp32s3", "dc:54:75:c8:12:34")
    assert parse_board_info("Error: no device") == (None, None)


def test_normalize_chip() -> None:
    assert normalize_chip("ESP32-S3") == "esp32s3"
    assert normalize_chip("esp32c3 (revision v0.4)") == "esp32c3"


def test_matches_chip_and_optional_mac() -> None:
    d = Device(port="p", chip="esp32s3", mac="dc:54:75:c8:12:34")
    assert matches(Board("s3", "esp32s3", "S3"), d)
    assert matches(Board("s3", "esp32s3", "S3", mac="DC:54:75:C8:12:34"), d)
    assert not matches(Board("s3", "esp32s3", "S3", mac="00:00:00:00:00:00"), d)
    assert not matches(Board("c3", "esp32c3", "C3"), d)


def test_wait_for_retries_silent_port_until_it_answers(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    answers = iter(
        [
            (None, "Error: connection failed"),
            (None, "Error: connection failed"),
            (Device(str(port), "esp32s3", "dc:54:75:c8:12:34"), ""),
        ]
    )
    probes: list[str] = []

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probes.append(p)
        return next(answers)

    d = wait_for(
        Board("s3", "esp32s3", "S3"),
        ports_fn=lambda: [str(port)],
        probe_fn=probe_fn,
        sleep_fn=lambda _: None,
    )
    assert d.chip == "esp32s3"
    assert len(probes) == 3


def test_wait_for_skips_wrong_chip_until_replugged(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    calls = 0

    def ports_fn() -> list[str]:
        # Replug after two polls: the port vanishes once, then returns with the right board.
        if calls == 1 and port.exists():
            port.unlink()
        elif not port.exists():
            port.touch()
        return [str(port)]

    def probe_fn(p: str) -> tuple[Device | None, str]:
        nonlocal calls
        calls += 1
        chip = "esp32c3" if calls == 1 else "esp32s3"
        return Device(p, chip, None), ""

    d = wait_for(
        Board("s3", "esp32s3", "S3"), ports_fn=ports_fn, probe_fn=probe_fn, sleep_fn=lambda _: None
    )
    assert d.chip == "esp32s3"
    assert calls == 2


ESPFLASH_BUSY = """
[2026-11-06T20:38:50Z INFO ] Serial port: '/dev/cu.usbserial-110'
[2026-11-06T20:38:50Z INFO ] Connecting...
Error:   x Failed to open serial port /dev/cu.usbserial-110
  ├─▶ Error while connecting to device
  ╰─▶ Unable to acquire exclusive lock on serial port
"""


def test_probe_error_classifies_locked_port_and_keeps_last_error_otherwise() -> None:
    assert probe_error(ESPFLASH_BUSY) == BUSY
    assert probe_error("Error: Device or resource busy (os error 16)") == BUSY
    assert probe_error("x\nError: connection failed\n") == "Error: connection failed"
    assert probe_error("") == "no output"


def test_wait_for_skips_busy_port_without_counting_it_as_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, free = tmp_path / "usbmodem_s3", tmp_path / "usbserial_c3"
    held.touch()
    free.touch()
    probes: list[str] = []
    shown: list[str] = []
    monkeypatch.setattr(device_mod, "info", shown.append)
    monkeypatch.setattr(device_mod, "warn", shown.append)

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probes.append(p)
        if p == str(held):
            return None, BUSY
        # The C3 answers only on its second probe, as a resetting board would.
        if probes.count(p) < 2:
            return None, "Error: connection failed"
        return Device(p, "esp32c3", None), ""

    d = wait_for(
        Board("c3", "esp32c3", "C3"),
        ports_fn=lambda: [str(held), str(free)],
        probe_fn=probe_fn,
        sleep_fn=lambda _: None,
    )

    assert d.chip == "esp32c3" and d.port == str(free)
    assert probes.count(str(held)) == 2  # re-checked each poll, never treated as silent
    assert sum("in use by another process" in m for m in shown) == 1
    assert SLEEP_HINT not in shown


def test_wait_for_accepts_a_port_once_it_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = tmp_path / "usbserial"
    port.touch()
    shown: list[str] = []
    monkeypatch.setattr(device_mod, "info", shown.append)
    monkeypatch.setattr(device_mod, "warn", shown.append)
    answers = iter([(None, BUSY), (None, BUSY), (Device(str(port), "esp32c3", None), "")])

    d = wait_for(
        Board("c3", "esp32c3", "C3"),
        port=str(port),
        probe_fn=lambda p: next(answers),
        sleep_fn=lambda _: None,
    )

    assert d.port == str(port)
    assert sum("waiting until it is released" in m for m in shown) == 1


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (FileNotFoundError(2, "No such file or directory", "espflash"), True),
        (PermissionError(13, "Permission denied", "espflash"), False),
    ],
)
def test_probe_without_a_runnable_espflash_raises_missing_tool(
    monkeypatch: pytest.MonkeyPatch, exc: OSError, hint: bool
) -> None:
    def broken(*a: object, **k: object) -> object:
        raise exc

    monkeypatch.setattr("rustyfarian_bench.device.subprocess.run", broken)

    with pytest.raises(device_mod.MissingTool, match=r"cannot run 'espflash'") as info:
        device_mod.probe("/dev/x")
    assert ("cargo install espflash" in str(info.value)) is hint
