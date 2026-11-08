"""Shared fakes: a queued line source and a scripted serial port, no board needed."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

import pytest

_real_sleep = time.sleep


# One item per read(): bytes are returned, an exception is raised, a callable is a side
# effect (e.g. unplugging the port) after which the read returns b"".
@dataclass(frozen=True)
class Buffered:
    """Bytes the serial driver already holds: read like any bytes, dropped by
    `reset_input_buffer()` while they are the next thing to read."""

    data: bytes


ScriptItem = bytes | Buffered | BaseException | Callable[[], None]


class FakeLines:
    """Lines already queued, then silence; stands in for SerialLines."""

    def __init__(self, *lines: str, echo: bool = True):
        self.queue = deque(lines)
        self.drained = 0
        self.echo = echo
        self.lost = False  # what link_lost_since() reports
        self.lost_since_args: list[float] = []
        self.on_get: Callable[[], None] | None = None  # called at the start of every get()
        self.exited = 0

    def get(self, timeout: float) -> str | None:
        if self.on_get:
            self.on_get()
        return self.queue.popleft() if self.queue else None

    def drain(self) -> None:
        self.drained += 1
        self.queue.clear()

    def link_lost_since(self, t0: float) -> bool:
        self.lost_since_args.append(t0)
        return self.lost

    def __enter__(self) -> FakeLines:
        return self

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.exited += 1


class FakeSerial:
    """Replacement for serial.Serial: attributes the reader sets, a scripted read()."""

    def __init__(self, world: FakeSerialWorld):
        self.world = world
        self.port: str | None = None
        self.baudrate = 0
        self.timeout: float | None = None
        self.exclusive = False
        self.dtr = True
        self.rts = True
        self.events: list[str] = []
        self._script: deque[ScriptItem] | None = None

    def __enter__(self) -> FakeSerial:
        self.world.opened.append(self)
        self.events.append(f"open dtr={self.dtr} rts={self.rts}")
        script = self.world.next_script()
        if isinstance(script, BaseException):
            raise script
        self._script = deque(script)
        return self

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.events.append("close")

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("dtr", "rts") and "events" in self.__dict__ and self._script is not None:
            self.events.append(f"{name}={value}")
        super().__setattr__(name, value)

    def read(self, n: int) -> bytes:
        assert self._script is not None
        if not self._script:
            self.world.exhausted.set()
            _real_sleep(0.005)  # idle instead of spinning
            return b""
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item()
            return b""
        if isinstance(item, Buffered):
            return item.data
        return item

    def reset_input_buffer(self) -> None:
        assert self._script is not None
        self.events.append("flush")
        while self._script and isinstance(self._script[0], Buffered):
            self._script.popleft()


class FakeSerialWorld:
    """Scripts one entry per open of the port; patched over serial.Serial and os.path.exists.

    Each script is a list of ScriptItem, or an exception to raise from the open itself.
    When the scripts run out, reads return b"" and `exhausted` is set.
    """

    def __init__(self, *scripts: list[ScriptItem] | BaseException, present: bool = True):
        self.scripts: deque[list[ScriptItem] | BaseException] = deque(scripts)
        self.paths: set[str] = set()
        self.default_present = present
        self.opened: list[FakeSerial] = []
        self.exhausted = threading.Event()

    def next_script(self) -> list[ScriptItem] | BaseException:
        return self.scripts.popleft() if self.scripts else []

    def __call__(self, *a: object, **k: object) -> FakeSerial:
        return FakeSerial(self)

    def exists(self, path: str) -> bool:
        return self.default_present or path in self.paths

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("rustyfarian_bench.serial_lines.serial.Serial", self)
        monkeypatch.setattr("rustyfarian_bench.serial_lines.os.path.exists", self.exists)


def shrink_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make time.sleep near-instant so reconnect backoff and the reset pulse don't slow tests."""
    monkeypatch.setattr("rustyfarian_bench.serial_lines.time.sleep", lambda s: _real_sleep(0.001))


def wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Bounded poll; never a fixed sleep."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        _real_sleep(0.005)
    return pred()
