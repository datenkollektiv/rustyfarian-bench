from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import serial

from rustyfarian_bench import serial_lines
from rustyfarian_bench.config import ResetMode
from rustyfarian_bench.serial_lines import DOWNLOAD_HINT, SerialLines, download_hint

from .fakes import FakeSerialWorld, ScriptItem, shrink_sleep, wait_until

PORT = "/dev/fake-port"

ROM_DOWNLOAD = "rst:0x15 (USB_UART_CHIP_RESET),boot:0x10 (DOWNLOAD(USB/UART0))"
ROM_FLASH = "rst:0x15 (USB_UART_CHIP_RESET),boot:0x18 (SPI_FAST_FLASH_BOOT)"


def test_download_boot_line_yields_hint() -> None:
    assert download_hint(ROM_DOWNLOAD) == DOWNLOAD_HINT


def test_waiting_for_download_yields_hint() -> None:
    assert download_hint("waiting for download") == DOWNLOAD_HINT


def test_normal_boot_and_app_lines_yield_nothing() -> None:
    assert download_hint(ROM_FLASH) is None
    assert download_hint("B3F button ready on GPIO 41 — press to test.") is None
    assert download_hint("") is None


def make(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: FakeSerialWorld,
    reset: ResetMode = "none",
) -> SerialLines:
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)
    return SerialLines(PORT, tmp_path / "run.log", echo=False, reset=reset)


def lines_of(sl: SerialLines, n: int) -> list[str | None]:
    return [sl.get(2.0) for _ in range(n)]


def gate_then(gate: threading.Event, *rest: ScriptItem) -> list[ScriptItem]:
    """Script that holds the reader until the test has taken its t0."""

    def hold() -> None:
        gate.wait(2.0)

    return [hold, *rest]


def test_chunks_and_crlf_are_split_into_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"ab", b"c\r\nde", b"f\n"])
    with make(tmp_path, monkeypatch, world) as sl:
        assert lines_of(sl, 2) == ["abc", "def"]
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == "abc\ndef\n"


def test_partial_line_is_dropped_on_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"x\npar", serial.SerialException("gone")], [b"b\n"])
    with make(tmp_path, monkeypatch, world) as sl:
        assert lines_of(sl, 2) == ["x", "b"]


def test_hard_reset_runs_once_across_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([serial.SerialException("re-enumerated")], [])
    with make(tmp_path, monkeypatch, world, reset="rts"):
        assert wait_until(world.exhausted.is_set)
    pulses = [sum(e == "rts=True" for e in s.events) for s in world.opened]
    assert pulses[0] == 1
    assert sum(pulses) == 1
    assert len(world.opened) >= 2


def test_enter_waits_for_first_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = FakeSerialWorld([b"hello\n"], present=False)
    timer = threading.Timer(0.1, world.paths.add, args=(PORT,))
    sl = make(tmp_path, monkeypatch, world)
    timer.start()
    try:
        with sl:
            assert len(world.opened) == 1  # the first step cannot race the reader
    finally:
        timer.join()


def test_never_opening_counts_as_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serial_lines, "FIRST_OPEN_WAIT", 0.2)
    world = FakeSerialWorld(present=False)
    with make(tmp_path, monkeypatch, world) as sl:
        assert sl.link_lost_since(time.monotonic())
    assert not world.opened


def test_reset_reenumeration_within_grace_is_not_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([serial.SerialException("re-enumerated")], [b"banner\n"])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        t0 = time.monotonic()
        assert sl.get(2.0) == "banner"
        assert not sl.link_lost_since(t0)


def unplug(world: FakeSerialWorld) -> ScriptItem:
    def go() -> None:
        world.paths.discard(PORT)
        raise serial.SerialException("re-enumerating")

    return go


def test_reset_reenumeration_still_open_within_grace_is_not_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld(present=False)
    world.paths.add(PORT)
    world.scripts.append([unplug(world)])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        t0 = time.monotonic()
        assert wait_until(lambda: PORT not in world.paths)
        assert not sl.link_lost_since(t0)  # port still gone, but inside the reset's grace


def test_reset_outage_still_open_after_grace_is_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serial_lines, "RESET_GRACE", 0.05)
    world = FakeSerialWorld(present=False)
    world.paths.add(PORT)
    world.scripts.append([unplug(world)])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        t0 = time.monotonic()
        assert wait_until(lambda: sl.link_lost_since(t0))  # the board never came back


def test_loss_after_grace_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serial_lines, "RESET_GRACE", 0.05)

    # time.sleep is shrunk by make(), so wait on an event for a real 0.2 s
    def pause() -> None:
        threading.Event().wait(0.2)

    world = FakeSerialWorld([pause, serial.SerialException("unplugged")], [])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        t0 = time.monotonic()
        assert wait_until(world.exhausted.is_set)
        assert sl.link_lost_since(t0)
        assert not sl.link_lost_since(time.monotonic())  # recovered: later windows are clean


@pytest.mark.parametrize("exc", [serial.SerialException("x"), OSError(5, "EIO")])
def test_read_error_is_a_loss_and_reader_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    gate = threading.Event()
    world = FakeSerialWorld(gate_then(gate, b"a\n", exc), [b"b\n"])
    with make(tmp_path, monkeypatch, world) as sl:
        t0 = time.monotonic()
        gate.set()
        assert lines_of(sl, 2) == ["a", "b"]
        assert sl.link_lost_since(t0)
        assert not sl.link_lost_since(time.monotonic())


@pytest.mark.parametrize("exc", [serial.SerialException("busy"), OSError(16, "EBUSY")])
def test_open_error_is_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    gate = threading.Event()
    world = FakeSerialWorld(gate_then(gate, serial.SerialException("gone")), exc, [b"b\n"])
    with make(tmp_path, monkeypatch, world) as sl:
        t0 = time.monotonic()
        gate.set()
        assert sl.get(2.0) == "b"
        assert sl.link_lost_since(t0)


def test_empty_read_with_vanished_path_is_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS keeps returning b"" from a vanished tty instead of raising.
    gate = threading.Event()
    world = FakeSerialWorld(gate_then(gate, lambda: world.paths.discard(PORT)), present=False)
    world.paths.add(PORT)
    with make(tmp_path, monkeypatch, world) as sl:
        t0 = time.monotonic()
        assert not sl.link_lost_since(t0)
        gate.set()
        assert wait_until(lambda: sl.link_lost_since(t0))


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_reader_crash_marks_link_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    world = FakeSerialWorld(gate_then(gate, ValueError("boom")))
    with make(tmp_path, monkeypatch, world) as sl:
        t0 = time.monotonic()
        gate.set()
        assert wait_until(lambda: sl.link_lost_since(t0))


def test_exit_joins_thread_and_is_not_a_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"a\n"])
    sl = make(tmp_path, monkeypatch, world)
    with sl:
        t0 = time.monotonic()
        assert sl.get(2.0) == "a"
    assert not sl._thread.is_alive()
    assert not sl.link_lost_since(t0)


@pytest.mark.parametrize(
    ("mode", "toggled", "untouched"),
    [("dtr", "dtr=True", "rts=True"), ("rts", "rts=True", "dtr=True"), ("none", None, "dtr=True")],
)
def test_reset_mode_picks_the_line_that_pulses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ResetMode,
    toggled: str | None,
    untouched: str,
) -> None:
    world = FakeSerialWorld([])
    with make(tmp_path, monkeypatch, world, reset=mode):
        assert wait_until(world.exhausted.is_set)
    events = world.opened[0].events
    assert events[0] == "open dtr=False rts=False"
    assert untouched not in events
    if toggled:
        assert events.count(toggled) == 1
    else:
        assert not any(e.startswith(("dtr=", "rts=")) for e in events)
