"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

from rustyfarian_bench import serial_lines


@pytest.fixture(autouse=True)
def short_boot_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entering a SerialLines that resets the board waits for the boot to settle (seconds in
    real life); keep that wait to milliseconds so every test that opens a fake port stays fast.
    Tests of the wait itself set their own values over these."""
    monkeypatch.setattr(serial_lines, "BOOT_QUIET", 0.02, raising=False)
    monkeypatch.setattr(serial_lines, "BOOT_SETTLE", 0.1, raising=False)
