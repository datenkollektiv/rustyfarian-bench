"""ProbeLines: a host command as a line source, against fake processes (no real command runs)."""

from __future__ import annotations

import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from rustyfarian_bench.probe_lines import ProbeLines

from .fakes import FakeKillpg, FakeProcess, FakeSpawner, wait_until

ARGV = ["tool", "--flag", "x"]


@pytest.fixture
def spawner() -> Iterator[FakeSpawner]:
    s = FakeSpawner()
    yield s
    s.close()


def _probe(
    spawner: FakeSpawner, tmp_path: Path, *procs: FakeProcess, grace: float = 0.05
) -> ProbeLines:
    spawner.procs.extend(procs)
    spawner.spawned.extend(procs)
    return ProbeLines(
        ARGV,
        tmp_path,
        tmp_path / "logs" / "run.probe1.log",
        echo=False,
        spawn=cast(Callable[..., "subprocess.Popen[bytes]"], spawner),
        grace=grace,
    )


def _log(tmp_path: Path) -> str:
    path = tmp_path / "logs" / "run.probe1.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_lines_come_out_in_order_without_line_endings(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"one\n", b"two\r\n", b"three\n"], returncode=0)
    with _probe(spawner, tmp_path, proc) as lines:
        assert [lines.get(2.0) for _ in range(3)] == ["one", "two", "three"]


def test_an_invalid_byte_is_replaced_not_fatal(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"bad \xff byte\n", b"next\n"], returncode=0)
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "bad � byte"
        assert lines.get(2.0) == "next"


def test_stderr_is_logged_and_kept_for_the_note_but_never_queued(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess([b"out\n"], [b"warn: slow broker\n"], returncode=3)
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "out"
        assert wait_until(lambda: "warn: slow broker" in lines.exit_note())
        assert lines.get(0.05) is None
        assert wait_until(lambda: "warn: slow broker" in _log(tmp_path))
        assert "out" in _log(tmp_path)


def test_the_exit_note_names_the_exit_code_and_the_last_stderr_lines(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess([], [f"err {i}\n".encode() for i in range(8)], returncode=3)
    with _probe(spawner, tmp_path, proc) as lines:
        assert wait_until(lambda: "err 7" in lines.exit_note())
        note = lines.exit_note()
    assert note.startswith("probe exited 3")
    assert "err 3" in note
    assert "err 2" not in note  # only the last five lines are kept


def test_the_link_is_not_lost_while_the_command_runs(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"up\n"])
    FakeKillpg(proc)  # nothing signalled here, but teardown must not touch a real group
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "up"
        assert not lines.link_lost_since(0.0)
        proc.die(0)  # let teardown find it gone


def test_the_link_is_lost_once_the_command_ended_and_queued_lines_still_arrive(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess([b"a\n", b"b\n"], returncode=1)
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "a"
        assert lines.get(2.0) == "b"
        assert wait_until(lambda: lines.link_lost_since(0.0))


def test_drain_keeps_what_the_command_already_printed(spawner: FakeSpawner, tmp_path: Path) -> None:
    proc = FakeProcess([b"early\n"], returncode=0)
    with _probe(spawner, tmp_path, proc) as lines:
        assert wait_until(lambda: "early" in _log(tmp_path))
        lines.drain()
        assert lines.get(2.0) == "early"


def test_a_probe_says_what_its_loss_is_called() -> None:
    assert ProbeLines.lost_text == "probe command exited"


def test_the_command_starts_in_the_given_directory_in_its_own_session(
    spawner: FakeSpawner, tmp_path: Path
) -> None:
    proc = FakeProcess(returncode=0)
    with _probe(spawner, tmp_path, proc):
        pass
    [call] = spawner.calls
    assert call.argv == ARGV
    assert call.cwd == tmp_path
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["stdout"] == subprocess.PIPE
    assert call.kwargs["stderr"] == subprocess.PIPE


@pytest.mark.parametrize("exc", [FileNotFoundError("tool"), PermissionError("tool")])
def test_a_command_that_cannot_start_raises_to_the_caller(tmp_path: Path, exc: OSError) -> None:
    broken = FakeSpawner(error=exc)
    with pytest.raises(type(exc)), _probe(broken, tmp_path):
        pytest.fail("the body must not run")


def test_teardown_ends_a_running_command_with_sigterm_alone(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"up\n"])
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "up"
    assert killpg.signals == [signal.SIGTERM]
    assert {pgid for pgid, _ in killpg.calls} == {proc.pid}
    assert proc.returncode == -signal.SIGTERM


def test_teardown_kills_a_command_that_ignores_sigterm_after_the_grace(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"up\n"], ignore_term=True)
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)
    with _probe(spawner, tmp_path, proc, grace=0.2) as lines:
        assert lines.get(2.0) == "up"
        t0 = time.monotonic()
    assert time.monotonic() - t0 >= 0.15  # SIGKILL only after the grace
    assert killpg.signals == [signal.SIGTERM, signal.SIGKILL]
    assert proc.returncode == -signal.SIGKILL


def test_teardown_of_a_command_that_already_exited_is_quiet(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"bye\n"], returncode=0)
    killpg = FakeKillpg(proc)  # every call says "no such process group"
    killpg.install(monkeypatch)
    with _probe(spawner, tmp_path, proc) as lines:
        assert lines.get(2.0) == "bye"
    assert signal.SIGKILL not in killpg.signals


def test_teardown_twice_signals_once(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"up\n"])
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)
    with _probe(spawner, tmp_path, proc) as lines:
        lines.__exit__(None, None, None)
    assert killpg.signals == [signal.SIGTERM]


def test_teardown_runs_when_the_body_raises(
    spawner: FakeSpawner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProcess([b"up\n"])
    killpg = FakeKillpg(proc)
    killpg.install(monkeypatch)
    with pytest.raises(RuntimeError, match="boom"), _probe(spawner, tmp_path, proc):
        raise RuntimeError("boom")
    assert killpg.signals == [signal.SIGTERM]
    assert proc.returncode is not None
