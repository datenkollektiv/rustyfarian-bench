"""Shared fakes: a queued line source and a scripted serial port, no board needed."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO

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

    lost_text = "serial connection lost"  # what a step says when link_lost_since() is True

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


class FakeProbeLines(FakeLines):
    """Stands in for ProbeLines: its own `lost_text`, a drain that keeps the lines, an exit note.

    `error` is raised on entering, like a command that cannot be started.
    """

    lost_text = "probe command exited"

    def __init__(
        self,
        *lines: str,
        note: str = "",
        error: OSError | None = None,
        echo: bool = True,
    ):
        super().__init__(*lines, echo=echo)
        self.note = note
        self.error = error

    def drain(self) -> None:
        self.drained += 1  # the process starts with the step: there is no stale backlog

    def exit_note(self) -> str:
        return self.note

    def __enter__(self) -> FakeProbeLines:
        if self.error is not None:
            raise self.error
        return self


@dataclass(frozen=True)
class ProbeCall:
    argv: list[str]
    cwd: Path
    log_path: Path


class FakeProbes:
    """The `open_probe` factory a Session is given: records each call, hands out `FakeProbeLines`.

    A call with no scripted probe left gets an empty one. `error` makes every probe fail to start.
    """

    def __init__(self, *probes: FakeProbeLines, error: OSError | None = None):
        self.scripted = deque(probes)
        self.error = error
        self.calls: list[ProbeCall] = []
        self.opened: list[FakeProbeLines] = []

    def __call__(self, argv: Sequence[str], cwd: Path, log_path: Path) -> FakeProbeLines:
        self.calls.append(ProbeCall(list(argv), cwd, log_path))
        probe = self.scripted.popleft() if self.scripted else FakeProbeLines()
        probe.error = self.error or probe.error
        self.opened.append(probe)
        return probe


class FakeProcess:
    """A scripted subprocess.Popen: real pipes carry `stdout` and `stderr`, nothing is started.

    With a `returncode` the process has already exited once the script is written (the pipes
    reach EOF); without one it runs until `die()` or a signal from `FakeKillpg`. With
    `ignore_term` SIGTERM does nothing, as for a command that traps it.
    """

    def __init__(
        self,
        stdout: Sequence[bytes] = (),
        stderr: Sequence[bytes] = (),
        *,
        returncode: int | None = None,
        ignore_term: bool = False,
        pid: int = 4242,
    ):
        self.pid = pid
        self.ignore_term = ignore_term
        self.returncode: int | None = None
        out_r, self._out_w = os.pipe()
        err_r, self._err_w = os.pipe()
        self.stdout: IO[bytes] = os.fdopen(out_r, "rb")
        self.stderr: IO[bytes] = os.fdopen(err_r, "rb")
        self._open_writers = {self._out_w, self._err_w}
        for chunk in stdout:
            self.feed(chunk)
        for chunk in stderr:
            self.feed(chunk, err=True)
        if returncode is not None:
            self.die(returncode)

    def feed(self, data: bytes, *, err: bool = False) -> None:
        os.write(self._err_w if err else self._out_w, data)

    def die(self, code: int) -> None:
        """The process (and its group) is gone: both pipes reach EOF."""
        if self.returncode is None:
            self.returncode = code
        for fd in tuple(self._open_writers):
            os.close(fd)
            self._open_writers.discard(fd)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert wait_until(lambda: self.returncode is not None, timeout or 2.0), "never ended"
        assert self.returncode is not None
        return self.returncode

    def close(self) -> None:
        """Release the pipes, whether or not the code under test did."""
        self.die(self.returncode if self.returncode is not None else 0)
        self.stdout.close()
        self.stderr.close()


@dataclass(frozen=True)
class SpawnCall:
    argv: list[str]
    kwargs: dict[str, object]

    @property
    def cwd(self) -> Path:
        return Path(str(self.kwargs["cwd"]))


class FakeSpawner:
    """Stands in for subprocess.Popen: records each call, hands out scripted `FakeProcess`es.

    `error` is raised instead, like a command that is missing or not executable.
    """

    def __init__(self, *procs: FakeProcess, error: OSError | None = None):
        self.procs = deque(procs)
        self.spawned = list(procs)
        self.error = error
        self.calls: list[SpawnCall] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> FakeProcess:
        self.calls.append(SpawnCall(list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return self.procs.popleft()

    def close(self) -> None:
        for proc in self.spawned:
            proc.close()


class FakeKillpg:
    """Records os.killpg calls on a FakeProcess's group and acts on them.

    Signal 0 probes the group (ProcessLookupError once it is gone); SIGTERM ends it unless the
    process ignores it; SIGKILL always does. Installed over `probe_lines.os.killpg`, which is
    the `os` module's own attribute: monkeypatch puts it back after the test. `os.getpgid` is
    patched too, since a fake pid has no real group.
    """

    def __init__(self, proc: FakeProcess):
        self.proc = proc
        self.calls: list[tuple[int, int]] = []  # (pgid, signal)

    @property
    def signals(self) -> list[int]:
        """The signals actually sent, without the existence probes."""
        return [sig for _, sig in self.calls if sig != 0]

    def __call__(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, int(sig)))
        if self.proc.returncode is not None:
            raise ProcessLookupError(pgid)
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and not self.proc.ignore_term):
            self.proc.die(-int(sig))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("rustyfarian_bench.probe_lines.os.killpg", self)
        monkeypatch.setattr("rustyfarian_bench.probe_lines.os.getpgid", lambda pid: pid)


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
        # Physical (DTR, RTS) during open: macOS asserts both, then pyserial applies DTR, then RTS.
        self.open_lines: list[tuple[bool, bool]] = []
        self._script: deque[ScriptItem] | None = None

    @property
    def dtr_raised_by_reader(self) -> int:
        """How often the reader drove DTR high (asked for before open, or set after).

        Each one reboots a DTR auto-reset board (an Arduino-style bridge with a capacitor
        on DTR). The OS's own assertion while opening (macOS) is not the reader's.
        """
        return int(self.open_lines[1][0]) + self.events.count("dtr=True")

    def __enter__(self) -> FakeSerial:
        self.world.opened.append(self)
        self.open_lines = [(True, True), (self.dtr, True), (self.dtr, self.rts)]
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
