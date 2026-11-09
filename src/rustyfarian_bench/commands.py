"""A host command run to completion: setup, teardown and `run` hooks, with a record of how it went.

The command runs in its own process group so a timeout or Ctrl-C can end it together with
whatever it spawned. A normal exit does not end the group: a setup such as `just mosquitto up`
may leave a daemon behind on purpose. POSIX only, like the probe.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .probe_lines import JOIN_WAIT, TERM_GRACE, end_group

# Lines kept per stream in the record, and how much of each: results.jsonl stays small.
TAIL_LINES = 5
TAIL_CHARS = 200
# After a normal exit the readers only have to drain what is buffered; a daemon the command
# left behind (a broker started by `setup`) may hold the pipes open for good, so do not wait long.
DRAIN_WAIT = 0.2


@dataclass(frozen=True)
class CommandRecord:
    argv: tuple[str, ...]
    exit_code: int | None  # None when it never started or was ended on a timeout
    duration: float
    timed_out: bool = False
    error: str = ""  # why it never ran or was cut short; empty otherwise
    stdout_tail: tuple[str, ...] = ()
    stderr_tail: tuple[str, ...] = ()
    log: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def summary(self) -> str:
        """One line for a verdict detail: the command, how it ended, the end of its stderr."""
        text = shlex.join(self.argv)
        if self.error:
            text += f": {self.error}"
        elif self.timed_out:
            text += f" timed out after {self.duration:g} s"
        else:
            text += f" exit {self.exit_code}"
        tail = self.stderr_tail or self.stdout_tail
        return f"{text}: {'; '.join(tail)}" if tail and not self.ok else text


def run_command(
    argv: Sequence[str],
    cwd: Path,
    log_path: Path,
    timeout: float,
    *,
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    grace: float = TERM_GRACE,
) -> CommandRecord:
    """Run `argv` to completion, or end its process group after `timeout` seconds.

    Never raises for a command that fails or cannot start: the record says so. Ctrl-C ends the
    group and propagates.
    """
    argv = list(argv)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(argv) + "\n")
        log.flush()
        try:
            proc = spawn(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            error = f"could not start: {e}"
            log.write(error + "\n")
            return CommandRecord(tuple(argv), None, 0.0, error=error, log=str(log_path))
        assert proc.stdout is not None and proc.stderr is not None
        lock = threading.Lock()
        tails: tuple[deque[str], deque[str]] = (
            deque(maxlen=TAIL_LINES),
            deque(maxlen=TAIL_LINES),
        )

        def read(pipe: IO[bytes], prefix: str, tail: deque[str]) -> None:
            try:
                for raw in pipe:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    with lock:
                        if not log.closed:
                            log.write(f"{prefix} {line}\n")
                            log.flush()
                    tail.append(line[:TAIL_CHARS])
            except (OSError, ValueError):  # the pipe was closed under the reader
                pass

        threads = [
            threading.Thread(target=read, args=(proc.stdout, "[out]", tails[0]), daemon=True),
            threading.Thread(target=read, args=(proc.stderr, "[err]", tails[1]), daemon=True),
        ]
        for t in threads:
            t.start()
        timed_out = False
        code: int | None = None
        try:
            try:
                code = proc.wait(timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                end_group(proc, grace)
        except BaseException:
            # start_new_session keeps the terminal's Ctrl-C from reaching the child.
            end_group(proc, grace)
            raise
        finally:
            for t in threads:
                t.join(timeout=JOIN_WAIT if (timed_out or code is None) else DRAIN_WAIT)
            for pipe, t in zip((proc.stdout, proc.stderr), threads, strict=True):
                if not t.is_alive():
                    pipe.close()
        duration = round(time.monotonic() - started, 3)
        with lock:
            if timed_out:
                log.write(f"timed out after {duration:g} s\n")
            else:
                log.write(f"exit {code} after {duration:g} s\n")
    return CommandRecord(
        tuple(argv),
        code,
        duration,
        timed_out=timed_out,
        stdout_tail=tuple(tails[0]),
        stderr_tail=tuple(tails[1]),
        log=str(log_path),
    )
