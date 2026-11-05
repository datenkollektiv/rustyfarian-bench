from __future__ import annotations

from rustyfarian_bench.config import Board
from rustyfarian_bench.device import Device, matches, normalize_chip, parse_board_info

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
