from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from rustyfarian_bench.serial_lines import SerialLines


def _held_by_someone_else(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def test_reader_holds_the_port_exclusively(tmp_path: Path) -> None:
    master, slave = os.openpty()
    try:
        with SerialLines(os.ttyname(slave), tmp_path / "log", echo=False, reset="none"):
            deadline = time.monotonic() + 2
            while not _held_by_someone_else(slave) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _held_by_someone_else(slave), "reader did not take the exclusive lock"
        assert not _held_by_someone_else(slave), "lock survived the reader's exit"
    finally:
        os.close(master)
        os.close(slave)
