"""ProbeLines against a real `sh`: the whole process group is gone after teardown."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from rustyfarian_bench.probe_lines import ProbeLines

from .fakes import wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

# $1 and $2 are pidfiles: the background child's and the shell's own.
SCRIPT = 'sleep 60 & echo $! > "$1"; echo $$ > "$2"; echo ready; wait'
STUBBORN = f"trap '' TERM; {SCRIPT}"  # ignored signals are inherited by the child too


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _pids(*files: Path) -> list[int]:
    return [int(f.read_text()) for f in files if f.exists() and f.read_text().strip()]


def _run(tmp_path: Path, script: str, grace: float) -> tuple[list[int], float]:
    """Run `script` as a probe until "ready", tear it down, and return its pids and the
    seconds teardown took. Whatever is still alive afterwards is killed, so a failure
    here never leaves a `sleep 60` behind."""
    child, shell = tmp_path / "child.pid", tmp_path / "shell.pid"
    pids: list[int] = []
    try:
        probe = ProbeLines(
            ["sh", "-c", script, "sh", str(child), str(shell)],
            tmp_path,
            tmp_path / "run.probe1.log",
            echo=False,
            grace=grace,
        )
        with probe as lines:
            assert lines.get(10.0) == "ready"
            pids = _pids(child, shell)
            assert len(pids) == 2
            assert not any(_gone(p) for p in pids)
            t0 = time.monotonic()
        took = time.monotonic() - t0
        assert wait_until(lambda: all(_gone(p) for p in pids), 5.0)
        return pids, took
    finally:
        for pid in pids or _pids(child, shell):
            if not _gone(pid):
                os.kill(pid, signal.SIGKILL)


def test_teardown_ends_the_command_and_the_child_it_left_behind(tmp_path: Path) -> None:
    pids, _ = _run(tmp_path, SCRIPT, grace=2.0)
    assert len(pids) == 2


def test_teardown_kills_a_group_that_ignores_sigterm_once_the_grace_is_over(
    tmp_path: Path,
) -> None:
    pids, took = _run(tmp_path, STUBBORN, grace=0.2)
    assert len(pids) == 2
    assert took >= 0.15  # SIGTERM alone did not do it


def test_teardown_kills_a_child_that_outlived_its_exited_leader(tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    script = f'sleep 60 & echo $! > "{child}"; echo ready'  # the shell exits, sleep stays
    pids: list[int] = []
    try:
        probe = ProbeLines(
            ["sh", "-c", script], tmp_path, tmp_path / "run.probe1.log", echo=False, grace=2.0
        )
        with probe as lines:
            assert lines.get(10.0) == "ready"
            pids = _pids(child)
            assert len(pids) == 1
            assert not _gone(pids[0])
            assert probe._proc is not None
            probe._proc.wait(timeout=5.0)  # the leader is reaped before teardown starts
        assert wait_until(lambda: _gone(pids[0]), 5.0)
    finally:
        for pid in pids:
            if not _gone(pid):
                os.kill(pid, signal.SIGKILL)
