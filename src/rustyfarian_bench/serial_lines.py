"""Background serial reader that survives USB re-enumeration.

A native USB Serial/JTAG port disappears on chip reset and comes back; a handle
opened just before the reset goes dead silently. The reader therefore reopens
the port whenever a read fails, and hands out complete lines through a queue.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import IO, Protocol

import serial

from . import ui
from .config import ResetMode


class LineSource(Protocol):
    def get(self, timeout: float) -> str | None: ...

    def drain(self) -> None: ...

    def link_lost_since(self, t0: float) -> bool: ...


# The ROM prints one of these when a reset lands in download mode. On an S3 this
# state can stick across every chip reset (see the consumer's project lore);
# only a power cycle clears it, so tell the user instead of waiting silently.
DOWNLOAD_HINT = (
    "Board is in download mode and stays there across resets: "
    "unplug and replug it (without BOOT); the run keeps waiting."
)
_DOWNLOAD_MARKERS = ("(DOWNLOAD(", "waiting for download")


def download_hint(line: str) -> str | None:
    """Return the replug hint if `line` is a ROM download-mode line, else None."""
    return DOWNLOAD_HINT if any(m in line for m in _DOWNLOAD_MARKERS) else None


# A chip reset re-enumerates a native USB port: it vanishes and returns within a
# second or two. Outages that end inside this window after the hard reset are the
# reset itself, not a lost link. A board that never comes back still counts, and so
# does an unplug that lands inside the window only if it lasts past the window's end.
RESET_GRACE = 5.0
# How long __enter__ waits for the port to open the first time, so the first step
# does not race the reader thread.
FIRST_OPEN_WAIT = 10.0


class SerialLines:
    def __init__(
        self,
        port: str,
        log_path: Path,
        baud: int = 115_200,
        echo: bool = True,
        reset: ResetMode = "rts",
    ):
        self.port = port
        self.baud = baud
        self.echo = echo
        self._reset_mode = reset
        self._reset_pending = reset != "none"
        self._hinted = False
        # Outages as [start, end]; end is None while the link is down. The reader
        # starts out disconnected.
        self._outages: list[list[float | None]] = [[time.monotonic(), None]]
        self._outage_lock = threading.Lock()
        self._ignore_before = 0.0
        self._opened = threading.Event()
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log: IO[str] = log_path.open("a", encoding="utf-8")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> SerialLines:
        self._thread.start()
        self._opened.wait(FIRST_OPEN_WAIT)  # if it never opens, the open outage counts as lost
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._log.close()

    def get(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def link_lost_since(self, t0: float) -> bool:
        """True if the link was down at any point after `t0`, bar the reset's own blip."""
        with self._outage_lock:
            for start, end in self._outages:
                if end is None:
                    # Still down: the reset's own re-enumeration only counts once its
                    # grace is over, i.e. when the board did not come back.
                    started_late = start is not None and start >= self._ignore_before
                    if started_late or time.monotonic() > self._ignore_before:
                        return True
                elif end > t0 and end > self._ignore_before:
                    return True
        return False

    def _mark_connected(self) -> None:
        with self._outage_lock:
            if self._outages[-1][1] is None:
                self._outages[-1][1] = time.monotonic()
        self._opened.set()

    def _mark_lost(self) -> None:
        if self._stop.is_set():
            return
        with self._outage_lock:
            if self._outages[-1][1] is not None:
                self._outages.append([time.monotonic(), None])

    def _emit(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self._log.write(line + "\n")
        self._log.flush()
        if self.echo:
            ui.device_line(line)
        if not self._hinted and (hint := download_hint(line)):
            self._hinted = True
            ui.warn(hint)
        self._queue.put(line)

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            self._mark_lost()  # a dead reader must not look like a quiet board
            raise

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not os.path.exists(self.port):
                self._mark_lost()
                time.sleep(0.1)
                continue
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = self.port, self.baud, 0.1
            # Exclusive (flock): espflash respects the lock, so a parallel session's
            # probe fails fast on this port instead of resetting the board under test.
            s.exclusive = True
            # Deassert DTR/RTS *before* open: on USB Serial/JTAG (and auto-reset
            # bridges) they drive EN/BOOT, and pyserial asserts both on open by
            # default, which holds the chip in reset — a silent port.
            s.dtr = False
            s.rts = False
            buf = b""  # a partial line from a dead handle must not prefix the next one
            try:
                with s:  # SerialBase.__enter__ opens the configured port
                    resetting = self._reset_pending
                    if resetting:
                        self._reset_pending = False
                        # Before the reset, and before __enter__ is released, so the
                        # re-enumeration the reset causes is clipped.
                        with self._outage_lock:
                            self._ignore_before = time.monotonic() + RESET_GRACE
                    self._mark_connected()
                    if resetting:
                        (_dtr_reset if self._reset_mode == "dtr" else _hard_reset)(s)
                    while not self._stop.is_set():
                        chunk = s.read(256)
                        if not chunk:
                            # macOS keeps returning b"" from a vanished tty
                            # instead of raising.
                            if not os.path.exists(self.port):
                                raise serial.SerialException("port vanished")
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            self._emit(raw)
            except (serial.SerialException, OSError):
                self._mark_lost()
                time.sleep(0.1)


def _hard_reset(s: serial.Serial) -> None:
    """Reset into the app on the already-open port (esptool's USB-JTAG hard reset).

    RTS pulls EN low while DTR stays deasserted, so the chip boots from flash.
    `espflash reset` is not used: it enters the bootloader first, and on a native
    USB Serial/JTAG port its closing reset leaves the chip in download mode.
    The reader is already listening, so the boot banner is captured.
    """
    s.dtr = False
    s.rts = True
    time.sleep(0.2)
    s.rts = False
    time.sleep(0.2)


def _dtr_reset(s: serial.Serial) -> None:
    """Pulse DTR, which classic auto-reset bridges (CH340, FT232) couple to RESET."""
    s.rts = False
    s.dtr = True
    time.sleep(0.1)
    s.dtr = False
    time.sleep(0.2)
