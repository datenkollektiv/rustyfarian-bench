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


class LineSource(Protocol):
    def get(self, timeout: float) -> str | None: ...

    def drain(self) -> None: ...


class SerialLines:
    def __init__(self, port: str, log_path: Path, baud: int = 115_200, echo: bool = True):
        self.port = port
        self.baud = baud
        self.echo = echo
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log: IO[str] = log_path.open("a", encoding="utf-8")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> SerialLines:
        self._thread.start()
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

    def _emit(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self._log.write(line + "\n")
        self._log.flush()
        if self.echo:
            ui.device_line(line)
        self._queue.put(line)

    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            if not os.path.exists(self.port):
                time.sleep(0.1)
                continue
            try:
                with serial.Serial(self.port, self.baud, timeout=0.1) as s:
                    while not self._stop.is_set():
                        chunk = s.read(256)
                        if not chunk:
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            self._emit(raw)
            except (serial.SerialException, OSError):
                time.sleep(0.1)
