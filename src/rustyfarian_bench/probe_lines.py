"""A host command as a line source: a probe step judges its stdout like serial lines.

The command runs in its own process group, so teardown can end it together with
whatever it spawned (a `just` recipe, say). stdout becomes the lines; stderr is
only logged, echoed and kept for the exit note. POSIX only, like the flock-based
serial reader.
"""

from __future__ import annotations

import contextlib
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

from . import ui

# How long SIGTERM gets to end the whole process group before SIGKILL.
TERM_GRACE = 2.0
# Bounded waits at teardown: a grandchild holding a pipe open must not hang the run.
JOIN_WAIT = 2.0
# stderr lines kept for the exit note.
STDERR_TAIL = 5
# How long exit_note waits for a command that closed stdout to be reaped.
REAP_WAIT = 0.5


def end_group(proc: subprocess.Popen[bytes], grace: float = TERM_GRACE) -> None:
    """SIGTERM the process group, give it `grace` to vanish, then SIGKILL what is left."""
    # Signal even when the leader is already gone: children it left behind keep the group
    # (and so the pgid) alive, and an empty group answers ProcessLookupError.
    pgid = proc.pid  # start_new_session made the command its group's leader
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while not group_gone(proc, pgid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if not group_gone(proc, pgid):
            os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    # A leader stuck in the kernel must not hang the run.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=JOIN_WAIT)


def group_gone(proc: subprocess.Popen[bytes], pgid: int) -> bool:
    proc.poll()  # reap the leader: a zombie still counts as a group member
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM once only zombies remain
        return True
    return False


class ProbeLines:
    lost_text = "probe command exited"

    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        log_path: Path,
        *,
        echo: bool = True,
        spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        grace: float = TERM_GRACE,
    ):
        self.argv = list(argv)
        self.cwd = cwd
        self.echo = echo
        self._spawn = spawn
        self._grace = grace
        self._queue: queue.Queue[str] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL)
        self._ended = threading.Event()  # stdout reached EOF: the command is gone
        self._log_lock = threading.Lock()
        self._closed = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log: IO[str] = log_path.open("a", encoding="utf-8")

    def __enter__(self) -> ProbeLines:
        try:
            self._proc = self._spawn(
                self.argv,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            self._log.close()
            raise
        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._threads = [
            threading.Thread(target=self._read, args=(self._proc.stdout, False), daemon=True),
            threading.Thread(target=self._read, args=(self._proc.stderr, True), daemon=True),
        ]
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            proc = self._proc
            if proc is not None:
                end_group(proc, self._grace)
                for t in self._threads:
                    t.join(timeout=JOIN_WAIT)
                for pipe, t in zip((proc.stdout, proc.stderr), self._threads, strict=True):
                    if pipe is not None and not t.is_alive():
                        pipe.close()
        finally:
            with self._log_lock:
                self._log.close()

    def get(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def drain(self) -> None:
        """No-op: the command starts with the step, so no stale backlog exists."""

    def link_lost_since(self, t0: float) -> bool:
        """True once the command has closed stdout; lines queued before that still come out."""
        return self._ended.is_set()

    def exit_note(self) -> str:
        """How the command ended, with the tail of its stderr."""
        proc = self._proc
        code = proc.poll() if proc is not None else None
        deadline = time.monotonic() + REAP_WAIT
        while proc is not None and code is None and time.monotonic() < deadline:
            time.sleep(0.02)
            code = proc.poll()
        note = "probe still running" if code is None else f"probe exited {code}"
        tail = "; ".join(self._stderr_tail)
        return f"{note}: {tail}" if tail else note

    def _read(self, pipe: IO[bytes], is_stderr: bool) -> None:
        try:
            for raw in pipe:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                with self._log_lock:
                    if not self._log.closed:
                        self._log.write(line + "\n")
                        self._log.flush()
                if self.echo:
                    ui.device_line(line)
                if is_stderr:
                    self._stderr_tail.append(line)
                else:
                    self._queue.put(line)
        except (OSError, ValueError):  # the pipe was closed under the reader
            pass
        finally:
            if not is_stderr:
                self._ended.set()
