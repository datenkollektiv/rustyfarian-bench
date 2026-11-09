"""run_command against a real `sh`: a timeout takes the whole process group with it."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

from rustyfarian_bench.commands import run_command

from .fakes import wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

# $1 and $2 are pidfiles: the background child's and the shell's own.
SCRIPT = 'sleep 60 & echo $! > "$1"; echo $$ > "$2"; echo started; wait'


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _pids(*files: Path) -> list[int]:
    return [int(f.read_text()) for f in files if f.exists() and f.read_text().strip()]


def test_a_timeout_kills_the_command_and_the_child_it_left_behind(tmp_path: Path) -> None:
    child, shell = tmp_path / "child.pid", tmp_path / "shell.pid"
    pids: list[int] = []
    try:
        rec = run_command(
            ["sh", "-c", SCRIPT, "sh", str(child), str(shell)],
            tmp_path,
            tmp_path / "run.setup1.log",
            1.0,
            grace=0.5,
        )
        pids = _pids(child, shell)
        assert rec.timed_out
        assert not rec.ok
        assert len(pids) == 2  # the command got as far as starting its child
        assert wait_until(lambda: all(_gone(p) for p in pids), 5.0)
    finally:
        for pid in pids or _pids(child, shell):
            if not _gone(pid):
                os.kill(pid, signal.SIGKILL)


def test_a_real_command_that_exits_returns_its_code_and_tails(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "run.teardown1.log"

    rec = run_command(["sh", "-c", "echo hi; echo err >&2; exit 4"], tmp_path, log, 10.0)

    assert rec.exit_code == 4
    assert not rec.ok
    assert not rec.timed_out
    assert list(rec.stdout_tail) == ["hi"]
    assert list(rec.stderr_tail) == ["err"]
    text = log.read_text(encoding="utf-8")
    assert "[out] hi" in text
    assert "[err] err" in text


def test_a_command_that_is_not_installed_is_a_record_not_an_exception(tmp_path: Path) -> None:
    rec = run_command(["definitely-not-a-command-xyz"], tmp_path, tmp_path / "run.setup1.log", 5.0)

    assert rec.exit_code is None
    assert rec.error
    assert not rec.ok
