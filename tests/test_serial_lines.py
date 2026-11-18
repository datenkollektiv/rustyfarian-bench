from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import serial

from rustyfarian_bench import runner, serial_lines
from rustyfarian_bench.config import BenchConfig, Board, ResetMode, Scenario, Step
from rustyfarian_bench.device import Device
from rustyfarian_bench.serial_lines import DOWNLOAD_HINT, SerialLines, download_hint
from rustyfarian_bench.steps import StepResult, run_step

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
    if not world.default_present:
        # A port that may not be there must not make entering wait out a whole pulse.
        monkeypatch.setattr(serial_lines, "PULSE_WAIT", 0.5)
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


@pytest.mark.parametrize("mode", ["rts", "none"])
def test_opening_never_holds_an_esp_in_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ResetMode
) -> None:
    # RTS high with DTR low resets a USB-Serial-JTAG chip (and EN on auto-reset bridges);
    # a reopen after re-enumeration, e.g. a deep-sleep wake, must not pass through it.
    world = FakeSerialWorld([serial.SerialException("re-enumerated")], [])
    with make(tmp_path, monkeypatch, world, reset=mode):
        assert wait_until(world.exhausted.is_set)
    assert len(world.opened) >= 2
    for s in world.opened:
        assert (False, True) not in s.open_lines


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
    if mode == "dtr":  # DTR is that board's reset line: never raised just to open
        assert events[0] == "open dtr=False rts=False"
        after_open = events[1:]
    else:
        assert events[:2] == ["open dtr=True rts=False", "dtr=False"]
        after_open = events[2:]
    assert untouched not in after_open
    if toggled:
        assert after_open.count(toggled) == 1
    else:
        assert not any(e.startswith(("dtr=", "rts=")) for e in after_open)


def test_a_dtr_reset_board_gets_dtr_only_for_its_reset_pulse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On an Arduino-style bridge, raising DTR is the reset: the open and a reopen after
    # re-enumeration must not raise it; only the startup pulse does.
    world = FakeSerialWorld([serial.SerialException("re-enumerated")], [])
    with make(tmp_path, monkeypatch, world, reset="dtr"):
        assert wait_until(world.exhausted.is_set)
    assert [s.dtr_raised_by_reader for s in world.opened[:2]] == [1, 0]


def test_none_sends_no_pulse_but_keeps_the_esp_safe_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A known limitation, pinned: "none" opens like an ESP, raising DTR once before the
    # open. That can reset a DTR auto-reset board, which belongs on reset = "dtr".
    world = FakeSerialWorld([serial.SerialException("re-enumerated")], [])
    with make(tmp_path, monkeypatch, world, reset="none"):
        assert wait_until(world.exhausted.is_set)
    for s in world.opened[:2]:
        assert s.dtr_raised_by_reader == 1  # the open's own, no pulse
        assert s.open_lines[1:] == [(True, True), (True, False)]
        assert "rts=True" not in s.events


# --- the startup reset's boot banner (bug 002) ---------------------------------------------

BANNER = "ESP-ROM:esp32c3-api1-20210207\nrst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)\n"
SECOND_BOOT = "rst:0x3 (RTC_SW_SYS_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)\n"
REBOOT = "rst:0x|ESP-ROM:"


def boot_timing(monkeypatch: pytest.MonkeyPatch, quiet: float = 0.3, settle: float = 2.0) -> None:
    # Small, but ten times the 30 ms the fake boot takes to print its banner, so a busy CI
    # runner does not end the quiet time before the banner arrives.
    monkeypatch.setattr(serial_lines, "BOOT_QUIET", quiet, raising=False)
    monkeypatch.setattr(serial_lines, "BOOT_SETTLE", settle, raising=False)


def after(seconds: float) -> ScriptItem:
    """A script item that takes `seconds` of real time (time.sleep is shrunk by make())."""

    def wait() -> None:
        threading.Event().wait(seconds)

    return wait


def watch(sl: SerialLines, reject: str | None = REBOOT, seconds: float = 0.2) -> StepResult:
    """The first step of a run: a watch-only window, as the runner starts it."""
    step = Step(say="watch", duration=seconds, reject=reject, arm=False)
    return run_step(step, sl, lambda question: True)


def test_a_first_window_ignores_the_banner_of_the_startup_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_timing(monkeypatch)
    # The banner comes a moment after the pulse, as the chip needs to boot.
    world = FakeSerialWorld([after(0.03), BANNER.encode(), b"app ready\n"])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        result = watch(sl)
    assert result.ok, result.detail


def test_a_second_reboot_inside_the_first_window_still_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_timing(monkeypatch)
    gate = threading.Event()
    # The window's own reboot follows the startup banner, but only once the test lets it.

    def hold() -> None:
        gate.wait(2.0)

    world = FakeSerialWorld([after(0.03), BANNER.encode(), hold, SECOND_BOOT.encode()])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        timer = threading.Timer(0.05, gate.set)
        timer.start()
        result = watch(sl, seconds=0.5)
        timer.join()
    assert not result.ok
    assert "RTC_SW_SYS_RST" in result.detail  # the second boot, not the startup banner
    assert "POWERON" not in result.detail


def test_an_attended_watch_that_is_the_first_step_passes_on_a_reset_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_timing(monkeypatch)
    board = Board("c3", "esp32c3", "C3", reset="rts")
    steps = (
        Step(say="boot", expect=("app ready",), arm=False),  # skipped by --attended
        Step(say="wave", duration=0.2, reject=REBOOT, attended=True),
    )
    scenario = Scenario("chk", board, ("ex",), steps, tmp_path / "chk.toml")
    cfg = BenchConfig(tmp_path / "bench", ("true",), ("true",), {"c3": board}, (scenario,))
    world = FakeSerialWorld([after(0.03), BANNER.encode(), b"app ready\n"])
    shrink_sleep(monkeypatch)
    world.install(monkeypatch)

    def real_lines(port: str, log: Path, **kw: object) -> SerialLines:
        # The real reader over the fake port; only the echo is switched off.
        return SerialLines(port, log, baud=board.baud, echo=False, reset=board.reset)

    monkeypatch.setattr(runner, "_run", lambda *a, **k: True)
    monkeypatch.setattr(runner, "SerialLines", real_lines)
    monkeypatch.setattr(
        runner.Session, "_device_for", lambda self, s: Device("/dev/x", "esp32c3", None)
    )
    opts = runner.Options(repo=tmp_path, assume_yes=True, mode="attended")

    result = runner.Session(cfg, opts).run_example(scenario, "ex")

    assert [s.say for s in result.steps] == ["wave"]
    assert result.steps[0].ok, result.steps[0].detail
    assert result.verdict == "pass"


def test_an_expect_step_that_is_first_still_sees_the_startup_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard: entering must not drain, or a boot-banner check would never match.
    boot_timing(monkeypatch)
    world = FakeSerialWorld([after(0.03), BANNER.encode(), b"app ready\n"])
    with make(tmp_path, monkeypatch, world, reset="rts") as sl:
        step = Step(say="boot", expect=("rst:0x1", "app ready"), timeout=1.0, arm=False)
        result = run_step(step, sl, lambda question: True)
    assert result.ok, result.detail


def test_a_board_without_reset_adds_no_wait_on_entering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard: with reset = "none" there is no boot to wait for, however long the quiet time.
    boot_timing(monkeypatch, quiet=5.0, settle=5.0)
    world = FakeSerialWorld([b"app ready\n"])
    t0 = time.monotonic()
    with make(tmp_path, monkeypatch, world, reset="none") as sl:
        assert time.monotonic() - t0 < 1.0
        assert sl.get(2.0) == "app ready"
    assert not any(e.startswith("rts=") for e in world.opened[0].events)


def test_entering_does_not_return_before_the_startup_pulse_has_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_timing(monkeypatch)
    world = FakeSerialWorld([])
    sl = make(tmp_path, monkeypatch, world, reset="rts")
    # A pulse that takes 0.1 s of real time (make() shrank every sleep to a millisecond).
    monkeypatch.setattr(
        "rustyfarian_bench.serial_lines.time.sleep", lambda s: threading.Event().wait(0.05)
    )
    with sl:
        # The pulse ends by letting RTS go; nothing after it touches the lines.
        assert world.opened[0].events[-2:] == ["rts=True", "rts=False"]


def test_a_pulse_that_never_finishes_does_not_hang_entering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard: the wait for the pulse is bounded by PULSE_WAIT.
    boot_timing(monkeypatch)
    release = threading.Event()
    world = FakeSerialWorld([])
    sl = make(tmp_path, monkeypatch, world, reset="rts")
    monkeypatch.setattr(serial_lines, "PULSE_WAIT", 0.2)
    monkeypatch.setattr("rustyfarian_bench.serial_lines.time.sleep", lambda s: release.wait(5.0))
    t0 = time.monotonic()
    try:
        with sl:
            assert time.monotonic() - t0 < 1.5
            # Let the stuck reader finish its pulse so leaving does not wait out the join grace.
            release.set()
    finally:
        release.set()


def test_a_banner_after_the_port_re_enumerated_is_still_not_in_the_first_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Native USB: the port vanishes right after the pulse for longer than the quiet time,
    # then returns, and the banner is printed on the second open.
    boot_timing(monkeypatch)
    world = FakeSerialWorld(present=False)
    world.paths.add(PORT)
    world.scripts.append([unplug(world)])
    world.scripts.append([after(0.03), BANNER.encode(), b"app ready\n"])
    back = threading.Timer(0.15, world.paths.add, args=(PORT,))
    back.start()
    try:
        with make(tmp_path, monkeypatch, world, reset="rts") as sl:
            result = watch(sl)
    finally:
        back.join()
    assert len(world.opened) >= 2
    assert result.ok, result.detail
