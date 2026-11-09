"""run_command: a host command run to completion, against fake processes (nothing real runs)."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from rustyfarian_bench.commands import CommandRecord, run_command

from .fakes import FakeKillpg, FakeProcess, FakeSpawner

ARGV = ["just", "build", "x"]


@pytest.fixture
def spawner() -> Iterator[FakeSpawner]:
    s = FakeSpawner()
    yield s
    s.close()


def _run(
    spawner: FakeSpawner,
    tmp_path: Path,
    *procs: FakeProcess,
    timeout: float = 2.0,
    grace: float = 0.05,
) -> CommandRecord:
    spawner.procs.extend(procs)
    spawner.spawned.extend(procs)
    return run_command(
        ARGV,
        tmp_path,
        tmp_path / "logs" / "run.setup1.log",
        timeout,
        spawn=cast(Callable[..., "subprocess.Popen[bytes]"], spawner),
        grace=grace,
    )


def _log(tmp_path: Path) -> str:
    return (tmp_path / "logs" / "run.setup1.log").read_text(encoding="utf-8")


def test_a_zero_exit_records_argv_exit_code_duration_and_tails(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess([b"one\n", b"two\n"], [b"warn\n"], returncode=0)

    rec = _run(spawner, tmp_path, proc)

    assert list(rec.argv) == ARGV
    assert rec.exit_code == 0
    assert rec.ok
    assert not rec.timed_out
    assert not rec.error
    assert rec.duration >= 0
    assert list(rec.stdout_tail) == ["one", "two"]
    assert list(rec.stderr_tail) == ["warn"]


def test_the_command_is_spawned_plainly_in_its_own_session(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    _run(spawner, tmp_path, FakeProcess(returncode=0))

    [call] = spawner.calls
    assert call.argv == ARGV  # a list, never through a shell
    assert not call.kwargs.get("shell")
    assert call.cwd == tmp_path
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["stdout"] == subprocess.PIPE
    assert call.kwargs["stderr"] == subprocess.PIPE
    assert call.kwargs["start_new_session"] is True


def test_the_log_holds_the_command_both_streams_and_the_exit(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess([b"hello\n"], [b"oops\n"], returncode=0)

    rec = _run(spawner, tmp_path, proc)

    text = _log(tmp_path)
    assert text.splitlines()[0] == "$ just build x"
    assert "[out] hello" in text
    assert "[err] oops" in text
    assert "exit 0 after" in text
    assert rec.log  # where to find it


def test_a_nonzero_exit_is_recorded_not_raised(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"building\n"], [b"error: boom\n"], returncode=3)

    rec = _run(spawner, tmp_path, proc)

    assert rec.exit_code == 3
    assert not rec.ok
    assert not rec.timed_out
    assert list(rec.stderr_tail) == ["error: boom"]
    assert "exit 3" in _log(tmp_path)


def test_a_command_that_cannot_start_is_a_record_with_an_error(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    spawner.error = FileNotFoundError("just")

    rec = _run(spawner, tmp_path)

    assert rec.exit_code is None
    assert "just" in rec.error
    assert not rec.ok
    assert not rec.timed_out
    assert len(spawner.calls) == 1


def test_a_timeout_ends_the_process_group_and_is_recorded(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"started\n"])  # runs until a signal ends it
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)

    rec = _run(spawner, tmp_path, proc, timeout=0.05)

    assert rec.timed_out
    assert not rec.ok
    assert killpg.signals == [signal.SIGTERM]
    assert killpg.calls[0][0] == proc.pid  # its own group, led by the command
    assert list(rec.stdout_tail) == ["started"]  # what it said before it was ended is kept


def test_a_timeout_escalates_to_sigkill_when_sigterm_is_ignored(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess(ignore_term=True)
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)

    rec = _run(spawner, tmp_path, proc, timeout=0.05, grace=0.05)

    assert rec.timed_out
    assert killpg.signals == [signal.SIGTERM, signal.SIGKILL]


def test_the_group_is_not_signalled_after_a_normal_exit(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A setup command may start a daemon in the group (`just mosquitto up`): it must live on.
    proc = FakeProcess([b"started a daemon\n"], returncode=0)
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)

    rec = _run(spawner, tmp_path, proc)

    assert rec.ok
    assert killpg.calls == []


def test_a_nonzero_exit_does_not_signal_the_group_either(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess(returncode=1)
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)

    _run(spawner, tmp_path, proc)

    assert killpg.calls == []


class _CtrlC(FakeProcess):
    """The operator presses Ctrl-C while the command is awaited (once: the cleanup waits too)."""

    interrupted = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return super().wait(timeout)


def test_a_keyboard_interrupt_ends_the_group_and_propagates(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _CtrlC()
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        _run(spawner, tmp_path, proc)

    assert killpg.signals == [signal.SIGTERM]


def test_tails_keep_the_last_lines_and_cap_their_length(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    lines = [f"line {i}\n".encode() for i in range(12)]
    long = b"x" * 500 + b"\n"
    proc = FakeProcess([*lines, long], [b"e1\n", b"e2\n"], returncode=0)

    rec = _run(spawner, tmp_path, proc)

    assert len(rec.stdout_tail) == 5
    assert list(rec.stdout_tail[:-1]) == [f"line {i}" for i in range(8, 12)]
    assert all(len(line) <= 200 for line in rec.stdout_tail)
    assert list(rec.stderr_tail) == ["e1", "e2"]
    assert "line 0" in _log(tmp_path)  # the log keeps everything, the record only the tail


def test_output_is_decoded_leniently(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"bad \xff byte\r\n"], [b"caf\xe9\n"], returncode=0)

    rec = _run(spawner, tmp_path, proc)

    assert list(rec.stdout_tail) == ["bad � byte"]
    assert list(rec.stderr_tail) == ["caf�"]


def test_summary_says_how_a_command_ended(spawner: FakeSpawner, tmp_path: Path) -> None:
    failed = _run(spawner, tmp_path, FakeProcess([], [b"connection refused\n"], returncode=3))
    assert "exit 3" in failed.summary()
    assert "connection refused" in failed.summary()
    assert "just" in failed.summary()

    spawner.error = FileNotFoundError("no such file: just")
    unstartable = _run(spawner, tmp_path)
    assert "no such file: just" in unstartable.summary()


def test_summary_of_a_timeout_says_so(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess()
    FakeKillpg(proc).install(monkeypatch)

    rec = _run(spawner, tmp_path, proc, timeout=0.05)

    assert "timed out after" in rec.summary()
