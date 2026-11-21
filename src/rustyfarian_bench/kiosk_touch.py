"""The kiosk's touch reader: has the panel been tapped since the last look?

It reads the Linux evdev node of the touch controller (stdlib only, so the sleep logic and its
tests need no Pillow). The kiosk treats a reader that holds no open device as "never dark", so
nothing in here raises: a missing, unreadable or vanished node is logged once and retried.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import struct
import time
from collections.abc import Callable
from pathlib import Path

# struct input_event on a 64-bit kernel: two 8-byte time_t fields, type, code, value (24 bytes).
EVENT = struct.Struct("@llHHi")
EV_SYN = 0x00
EV_KEY = 0x01
SYN_DROPPED = 3
BTN_TOUCH = 0x14A
RESCAN_SECONDS = 5
READ_EVENTS = 64
MAX_READS = 16

log = logging.getLogger("rustyfarian-bench-kiosk")


def find_touch(sysfs_input: Path, match: str) -> Path | None:
    """The first `eventN` under sysfs (numeric order) whose device name contains `match`."""

    def number(node: Path) -> int:
        return int(node.name.removeprefix("event"))

    try:
        nodes = sorted(
            (n for n in sysfs_input.glob("event[0-9]*") if n.name[5:].isdigit()), key=number
        )
        for node in nodes:
            try:
                name = node.joinpath("device", "name").read_text()
            except (OSError, ValueError):  # ValueError: a name that is not UTF-8
                continue
            if match in name:
                return node
    except OSError:
        return None
    return None


def is_touch(events: bytes) -> bool:
    """True if the whole events in `events` hold a touch-down or an event-buffer overflow."""
    whole = events[: len(events) // EVENT.size * EVENT.size]
    for _, _, kind, code, value in EVENT.iter_unpack(whole):
        if kind == EV_KEY and code == BTN_TOUCH and value == 1:
            return True
        if kind == EV_SYN and code == SYN_DROPPED:
            return True
    return False


class TouchReader:
    """Non-blocking reader of the touch node; `touched()` drains it once per kiosk tick."""

    def __init__(
        self,
        sysfs_input: Path,
        dev_dir: Path,
        match: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sysfs = sysfs_input
        self._dev = dev_dir / "input"
        self._match = match
        self._monotonic = monotonic
        self._fd: int | None = None
        self._buf = b""
        self._next_scan = 0.0
        self._warned: set[str] = set()
        self._open()

    @property
    def present(self) -> bool:
        """True while a touch node is open."""
        return self._fd is not None

    def touched(self) -> bool:
        """True if a touch-down arrived since the last call; reopens a lost node when due."""
        if self._fd is None:
            if self._monotonic() < self._next_scan:
                return False
            self._open()
            if self._fd is None:
                return False
        touched = False
        try:
            for _ in range(MAX_READS):
                data = os.read(self._fd, EVENT.size * READ_EVENTS)
                if not data:
                    break
                self._buf += data
                whole = len(self._buf) // EVENT.size * EVENT.size
                touched = is_touch(self._buf[:whole]) or touched
                self._buf = self._buf[whole:]
        except BlockingIOError:
            pass
        except OSError as exc:
            self._lost(exc)
        return touched

    def close(self) -> None:
        """Close the node; safe to call twice."""
        fd, self._fd = self._fd, None
        self._buf = b""
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    def _lost(self, exc: OSError) -> None:
        self._warn_once("lost", "touch device lost (%s); retrying", exc)
        self.close()
        self._next_scan = self._monotonic() + RESCAN_SECONDS

    def _open(self) -> None:
        self._next_scan = self._monotonic() + RESCAN_SECONDS
        node = find_touch(self._sysfs, self._match)
        if node is None:
            self._warn_once(
                "missing",
                "no touch device matching %r under %s; the panel stays on",
                self._match,
                self._sysfs,
            )
            return
        path = self._dev / node.name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                self._warn_once(
                    "denied",
                    "cannot read %s (%s); check the udev rule and the unit's DeviceAllow",
                    path,
                    exc.strerror,
                )
            else:
                self._warn_once("open", "cannot open %s (%s); retrying", path, exc)
            return
        self._buf = b""
        self._fd = fd
        if "chosen" not in self._warned:
            self._warned.add("chosen")
            log.info("touch: %s", path)
