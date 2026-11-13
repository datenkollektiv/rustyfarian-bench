from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from rustyfarian_bench import device as device_mod
from rustyfarian_bench.config import Board
from rustyfarian_bench.device import (
    BUSY,
    SLEEP_HINT,
    Device,
    matches,
    normalize_chip,
    parse_board_info,
    probe_error,
    wait_for,
)

BOARD_INFO = """
[2026-11-05T10:00:00Z INFO ] Serial port: '/dev/cu.usbmodem2101'
[2026-11-05T10:00:00Z INFO ] Connecting...
[2026-11-05T10:00:01Z INFO ] Using flash stub
Chip type:         esp32s3 (revision v0.2)
Crystal frequency: 40 MHz
Flash size:        16MB
Features:          WiFi, BLE
MAC address:       DC:54:75:C8:12:34
"""


def test_parse_board_info() -> None:
    assert parse_board_info(BOARD_INFO) == ("esp32s3", "dc:54:75:c8:12:34")
    assert parse_board_info("Error: no device") == (None, None)


def test_normalize_chip() -> None:
    assert normalize_chip("ESP32-S3") == "esp32s3"
    assert normalize_chip("esp32c3 (revision v0.4)") == "esp32c3"


def test_matches_chip_and_optional_mac() -> None:
    d = Device(port="p", chip="esp32s3", mac="dc:54:75:c8:12:34")
    assert matches(Board("s3", "esp32s3", "S3"), d)
    assert matches(Board("s3", "esp32s3", "S3", mac="DC:54:75:C8:12:34"), d)
    assert not matches(Board("s3", "esp32s3", "S3", mac="00:00:00:00:00:00"), d)
    assert not matches(Board("c3", "esp32c3", "C3"), d)


def test_wait_for_retries_silent_port_until_it_answers(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    answers = iter(
        [
            (None, "Error: connection failed"),
            (None, "Error: connection failed"),
            (Device(str(port), "esp32s3", "dc:54:75:c8:12:34"), ""),
        ]
    )
    probes: list[str] = []

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probes.append(p)
        return next(answers)

    d = wait_for(
        Board("s3", "esp32s3", "S3"),
        ports_fn=lambda: [str(port)],
        probe_fn=probe_fn,
        sleep_fn=lambda _: None,
    )
    assert d.chip == "esp32s3"
    assert len(probes) == 3


def test_wait_for_skips_wrong_chip_until_replugged(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    calls = 0

    def ports_fn() -> list[str]:
        # Replug after two polls: the port vanishes once, then returns with the right board.
        if calls == 1 and port.exists():
            port.unlink()
        elif not port.exists():
            port.touch()
        return [str(port)]

    def probe_fn(p: str) -> tuple[Device | None, str]:
        nonlocal calls
        calls += 1
        chip = "esp32c3" if calls == 1 else "esp32s3"
        return Device(p, chip, None), ""

    d = wait_for(
        Board("s3", "esp32s3", "S3"), ports_fn=ports_fn, probe_fn=probe_fn, sleep_fn=lambda _: None
    )
    assert d.chip == "esp32s3"
    assert calls == 2


ESPFLASH_BUSY = """
[2026-11-06T20:38:50Z INFO ] Serial port: '/dev/cu.usbserial-110'
[2026-11-06T20:38:50Z INFO ] Connecting...
Error:   x Failed to open serial port /dev/cu.usbserial-110
  ├─▶ Error while connecting to device
  ╰─▶ Unable to acquire exclusive lock on serial port
"""


def test_probe_error_classifies_locked_port_and_keeps_last_error_otherwise() -> None:
    assert probe_error(ESPFLASH_BUSY) == BUSY
    assert probe_error("Error: Device or resource busy (os error 16)") == BUSY
    assert probe_error("x\nError: connection failed\n") == "Error: connection failed"
    assert probe_error("") == "no output"


def test_wait_for_skips_busy_port_without_counting_it_as_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, free = tmp_path / "usbmodem_s3", tmp_path / "usbserial_c3"
    held.touch()
    free.touch()
    probes: list[str] = []
    shown: list[str] = []
    monkeypatch.setattr(device_mod, "info", shown.append)
    monkeypatch.setattr(device_mod, "warn", shown.append)

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probes.append(p)
        if p == str(held):
            return None, BUSY
        # The C3 answers only on its second probe, as a resetting board would.
        if probes.count(p) < 2:
            return None, "Error: connection failed"
        return Device(p, "esp32c3", None), ""

    d = wait_for(
        Board("c3", "esp32c3", "C3"),
        ports_fn=lambda: [str(held), str(free)],
        probe_fn=probe_fn,
        sleep_fn=lambda _: None,
    )

    assert d.chip == "esp32c3" and d.port == str(free)
    assert probes.count(str(held)) == 2  # re-checked each poll, never treated as silent
    assert sum("in use by another process" in m for m in shown) == 1
    assert SLEEP_HINT not in shown


def test_wait_for_accepts_a_port_once_it_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = tmp_path / "usbserial"
    port.touch()
    shown: list[str] = []
    monkeypatch.setattr(device_mod, "info", shown.append)
    monkeypatch.setattr(device_mod, "warn", shown.append)
    answers = iter([(None, BUSY), (None, BUSY), (Device(str(port), "esp32c3", None), "")])

    d = wait_for(
        Board("c3", "esp32c3", "C3"),
        port=str(port),
        probe_fn=lambda p: next(answers),
        sleep_fn=lambda _: None,
    )

    assert d.port == str(port)
    assert sum("waiting until it is released" in m for m in shown) == 1


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (FileNotFoundError(2, "No such file or directory", "espflash"), True),
        (PermissionError(13, "Permission denied", "espflash"), False),
    ],
)
def test_probe_without_a_runnable_espflash_raises_missing_tool(
    monkeypatch: pytest.MonkeyPatch, exc: OSError, hint: bool
) -> None:
    def broken(*a: object, **k: object) -> object:
        raise exc

    monkeypatch.setattr("rustyfarian_bench.device.subprocess.run", broken)

    with pytest.raises(device_mod.MissingTool, match=r"cannot run 'espflash'") as info:
        device_mod.probe("/dev/x")
    assert ("cargo install espflash" in str(info.value)) is hint


class _Port:
    def __init__(self, device: str, vid: int | None, pid: int | None, serial: str | None):
        self.device, self.vid, self.pid, self.serial_number = device, vid, pid, serial


_PORTS = [
    _Port("/dev/cu.usbserial-AQ01L7M0", 0x0403, 0x6001, "AQ01L7M0"),
    _Port("/dev/tty.usbserial-AQ01L7M0", 0x0403, 0x6001, "AQ01L7M0"),
    _Port("/dev/cu.usbserial-0001", 0x10C4, 0xEA60, "0001"),
    _Port("/dev/cu.usbserial-B", 0x0403, 0x6001, "OTHER"),
]


def test_usb_ports_match_vid_pid_and_prefer_cu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: _PORTS)
    monkeypatch.setattr("os.path.exists", lambda p: True)
    assert device_mod.usb_ports("0403:6001") == [
        "/dev/cu.usbserial-AQ01L7M0",
        "/dev/cu.usbserial-B",
    ]
    assert device_mod.usb_ports("0403:6001", "AQ01L7M0") == ["/dev/cu.usbserial-AQ01L7M0"]
    assert device_mod.usb_ports("1a86:7523") == []


def _usb_board(serial: str | None = None) -> Board:
    return Board("avr", "atmega328p", "Nano", detect=False, usb_id="0403:6001", usb_serial=serial)


def test_wait_for_usb_waits_until_the_board_appears() -> None:
    seen = iter([[], [], ["/dev/cu.usbserial-AQ01L7M0"]])
    sleeps: list[float] = []
    d = device_mod.wait_for_usb(
        _usb_board(), ports_fn=lambda usb_id, serial: next(seen), sleep_fn=sleeps.append
    )
    assert (d.port, d.chip, d.mac) == ("/dev/cu.usbserial-AQ01L7M0", "atmega328p", None)
    assert len(sleeps) == 2


def test_wait_for_usb_refuses_to_guess_between_matches() -> None:
    with pytest.raises(device_mod.PortRequired, match="usb_serial"):
        device_mod.wait_for_usb(
            _usb_board(),
            ports_fn=lambda usb_id, serial: ["/dev/a", "/dev/b"],
            sleep_fn=lambda s: None,
        )


# -- detection timeout -------------------------------------------------------------------------


class FakeClock:
    """A clock that only moves when `sleep` is called, so no test really waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _silent(p: str) -> tuple[Device | None, str]:
    return None, "Error: connection failed"


def _busy(p: str) -> tuple[Device | None, str]:
    return None, BUSY


def _wrong_chip(p: str) -> tuple[Device | None, str]:
    return Device(p, "esp32c3", None), ""


@pytest.mark.parametrize(
    "probe_fn", [None, _silent, _busy, _wrong_chip], ids=["no-port", "silent", "busy", "wrong-chip"]
)
def test_wait_for_gives_up_after_the_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_fn: Callable[[str], tuple[Device | None, str]] | None,
) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    monkeypatch.setattr(device_mod, "info", lambda m: None)
    monkeypatch.setattr(device_mod, "warn", lambda m: None)
    clock = FakeClock()

    with pytest.raises(device_mod.NoBoardInTime) as exc:
        wait_for(
            Board("s3", "esp32s3", "S3"),
            ports_fn=lambda: [] if probe_fn is None else [str(port)],
            probe_fn=probe_fn or _silent,
            sleep_fn=clock.sleep,
            timeout=1.0,
            clock=clock,
        )

    assert str(exc.value) == "no S3 (esp32s3) found within 1 s"
    assert isinstance(exc.value, RuntimeError)
    assert 1.0 <= clock.now <= 1.25  # stopped at the deadline, not a poll later


def test_wait_for_returns_a_board_that_shows_up_before_the_deadline(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    clock = FakeClock()
    passes = 0

    def probe_fn(p: str) -> tuple[Device | None, str]:
        nonlocal passes
        passes += 1
        return (Device(p, "esp32s3", None), "") if passes == 3 else (None, "no answer")

    d = wait_for(
        Board("s3", "esp32s3", "S3"),
        ports_fn=lambda: [str(port)],
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=10.0,
        clock=clock,
    )

    assert d.chip == "esp32s3"
    assert clock.now == 0.5


def test_wait_for_returns_a_board_answering_on_a_pass_that_starts_before_the_deadline(
    tmp_path: Path,
) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    clock = FakeClock()

    def probe_fn(p: str) -> tuple[Device | None, str]:
        # poll 0.25 and timeout 1.0: the fourth pass starts at t = 0.75, before the deadline
        return (Device(p, "esp32s3", None), "") if clock.now >= 0.75 else (None, "no answer")

    d = wait_for(
        Board("s3", "esp32s3", "S3"),
        ports_fn=lambda: [str(port)],
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=1.0,
        clock=clock,
    )

    assert d.chip == "esp32s3"


def test_wait_for_does_not_probe_on_a_pass_that_starts_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    monkeypatch.setattr(device_mod, "info", lambda m: None)
    monkeypatch.setattr(device_mod, "warn", lambda m: None)
    clock = FakeClock()
    probed_at: list[float] = []

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probed_at.append(clock.now)
        # would answer on the pass starting exactly at t = 1.0, which must not happen
        return (Device(p, "esp32s3", None), "") if clock.now >= 1.0 else (None, "no answer")

    with pytest.raises(device_mod.NoBoardInTime):
        wait_for(
            Board("s3", "esp32s3", "S3"),
            ports_fn=lambda: [str(port)],
            probe_fn=probe_fn,
            sleep_fn=clock.sleep,
            timeout=1.0,
            clock=clock,
        )

    assert probed_at == [0.0, 0.25, 0.5, 0.75]


def test_wait_for_stops_before_probing_the_remaining_ports_once_the_deadline_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [str(tmp_path / f"usbmodem{i}") for i in range(3)]
    for p in ports:
        Path(p).touch()
    monkeypatch.setattr(device_mod, "info", lambda m: None)
    monkeypatch.setattr(device_mod, "warn", lambda m: None)
    clock = FakeClock()
    probed: list[str] = []

    def probe_fn(p: str) -> tuple[Device | None, str]:
        probed.append(p)
        clock.sleep(10)  # a slow probe
        return None, "no answer"

    with pytest.raises(device_mod.NoBoardInTime) as exc:
        wait_for(
            Board("s3", "esp32s3", "S3"),
            ports_fn=lambda: ports,
            probe_fn=probe_fn,
            sleep_fn=clock.sleep,
            timeout=15.0,
            clock=clock,
        )

    assert str(exc.value) == "no S3 (esp32s3) found within 15 s"
    assert probed == ports[:2]  # t = 0 and t = 10 start a probe; t = 20 is past the deadline


def test_wait_for_without_a_timeout_keeps_waiting(tmp_path: Path) -> None:
    port = tmp_path / "usbmodem"
    port.touch()
    clock = FakeClock()

    def probe_fn(p: str) -> tuple[Device | None, str]:
        # An hour of fake time passes before the board answers.
        return (Device(p, "esp32s3", None), "") if clock.now >= 3600 else (None, "no answer")

    d = wait_for(
        Board("s3", "esp32s3", "S3"),
        ports_fn=lambda: [str(port)],
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=None,
        clock=clock,
    )

    assert d.chip == "esp32s3"
    assert clock.now >= 3600


def test_wait_for_usb_gives_up_after_the_timeout() -> None:
    clock = FakeClock()

    with pytest.raises(device_mod.NoBoardInTime) as exc:
        device_mod.wait_for_usb(
            _usb_board(),
            ports_fn=lambda usb_id, serial: [],
            sleep_fn=clock.sleep,
            timeout=2.0,
            clock=clock,
        )

    assert str(exc.value) == "no Nano (atmega328p) found within 2 s"
    assert 2.0 <= clock.now <= 2.25


def test_wait_for_usb_returns_a_board_plugged_in_before_the_deadline() -> None:
    clock = FakeClock()

    d = device_mod.wait_for_usb(
        _usb_board(),
        ports_fn=lambda usb_id, serial: ["/dev/cu.usbserial-1"] if clock.now >= 0.5 else [],
        sleep_fn=clock.sleep,
        timeout=5.0,
        clock=clock,
    )

    assert d.port == "/dev/cu.usbserial-1"


def test_wait_for_usb_several_matches_fail_at_once_even_with_a_timeout() -> None:
    clock = FakeClock()

    with pytest.raises(device_mod.PortRequired):
        device_mod.wait_for_usb(
            _usb_board(),
            ports_fn=lambda usb_id, serial: ["/dev/a", "/dev/b"],
            sleep_fn=clock.sleep,
            timeout=60.0,
            clock=clock,
        )

    assert clock.sleeps == 0


# -- unique detection --------------------------------------------------------------------------


S3 = Board("s3", "esp32s3", "S3")


def _ports(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for n in names:
        (tmp_path / n).touch()
        out.append(str(tmp_path / n))
    return out


def _by_port(
    answers: dict[str, tuple[Device | None, str]], log: list[str] | None = None
) -> Callable[[str], tuple[Device | None, str]]:
    def probe_fn(p: str) -> tuple[Device | None, str]:
        if log is not None:
            log.append(p)
        return answers[p]

    return probe_fn


def _s3(p: str, mac: str | None = None) -> tuple[Device | None, str]:
    return Device(p, "esp32s3", mac), ""


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_mod, "info", lambda m: None)
    monkeypatch.setattr(device_mod, "warn", lambda m: None)


@pytest.mark.usefixtures("quiet")
def test_unique_two_matches_raise_ambiguous_naming_both_ports_without_sleeping(
    tmp_path: Path,
) -> None:
    b, a = _ports(tmp_path, "b", "a")
    clock = FakeClock()

    with pytest.raises(device_mod.AmbiguousBoard) as exc:
        wait_for(
            S3,
            ports_fn=lambda: [b, a],
            probe_fn=_by_port({a: _s3(a), b: _s3(b)}),
            sleep_fn=clock.sleep,
            clock=clock,
            unique=True,
        )

    msg = str(exc.value)
    assert isinstance(exc.value, RuntimeError)
    assert a in msg
    assert b in msg
    assert msg.index(a) < msg.index(b)  # sorted
    assert "mac" in msg
    assert clock.sleeps == 0


@pytest.mark.usefixtures("quiet")
def test_unique_probes_every_port_in_the_pass_before_deciding(tmp_path: Path) -> None:
    first, second = _ports(tmp_path, "a", "b")
    log: list[str] = []

    with pytest.raises(device_mod.AmbiguousBoard):
        wait_for(
            S3,
            ports_fn=lambda: [first, second],
            probe_fn=_by_port({first: _s3(first), second: _s3(second)}, log),
            sleep_fn=lambda _: None,
            unique=True,
        )

    assert log == [first, second]


@pytest.mark.usefixtures("quiet")
def test_unique_one_match_and_a_wrong_chip_returns_the_match(tmp_path: Path) -> None:
    match, other = _ports(tmp_path, "a", "b")

    d = wait_for(
        S3,
        ports_fn=lambda: [match, other],
        probe_fn=_by_port({match: _s3(match), other: _wrong_chip(other)}),
        sleep_fn=lambda _: None,
        unique=True,
    )

    assert d.port == match


@pytest.mark.usefixtures("quiet")
def test_unique_a_single_board_alone_is_returned(tmp_path: Path) -> None:
    (only,) = _ports(tmp_path, "a")

    d = wait_for(
        S3,
        ports_fn=lambda: [only],
        probe_fn=_by_port({only: _s3(only)}),
        sleep_fn=lambda _: None,
        unique=True,
    )

    assert d.port == only


@pytest.mark.usefixtures("quiet")
def test_unique_a_pinned_mac_picks_its_board_among_same_chip_boards(tmp_path: Path) -> None:
    a, b = _ports(tmp_path, "a", "b")
    pinned = Board("s3", "esp32s3", "S3", mac="BB:00:00:00:00:02")

    d = wait_for(
        pinned,
        ports_fn=lambda: [a, b],
        probe_fn=_by_port({a: _s3(a, "aa:00:00:00:00:01"), b: _s3(b, "bb:00:00:00:00:02")}),
        sleep_fn=lambda _: None,
        unique=True,
    )

    assert d.port == b


@pytest.mark.usefixtures("quiet")
def test_unique_an_explicit_port_applies_no_rule(tmp_path: Path) -> None:
    a, b = _ports(tmp_path, "a", "b")
    log: list[str] = []

    d = wait_for(
        S3,
        port=b,
        ports_fn=lambda: [a, b],
        probe_fn=_by_port({a: _s3(a), b: _s3(b)}, log),
        sleep_fn=lambda _: None,
        unique=True,
    )

    assert d.port == b
    assert log == [b]


@pytest.mark.usefixtures("quiet")
def test_without_unique_the_first_match_wins_and_the_rest_is_not_probed(tmp_path: Path) -> None:
    a, b = _ports(tmp_path, "a", "b")
    log: list[str] = []

    d = wait_for(
        S3,
        ports_fn=lambda: [a, b],
        probe_fn=_by_port({a: _s3(a), b: _s3(b)}, log),
        sleep_fn=lambda _: None,
    )

    assert d.port == a
    assert log == [a]


@pytest.mark.usefixtures("quiet")
@pytest.mark.parametrize(
    ("answer", "reason"), [(_busy, "busy"), (_silent, "no answer")], ids=["busy", "silent"]
)
def test_unique_an_unresolved_port_keeps_polling_until_the_deadline(
    tmp_path: Path, answer: Callable[[str], tuple[Device | None, str]], reason: str
) -> None:
    match, stray = _ports(tmp_path, "a", "b")
    clock = FakeClock()

    with pytest.raises(device_mod.NoBoardInTime) as exc:
        wait_for(
            S3,
            ports_fn=lambda: [match, stray],
            probe_fn=lambda p: _s3(p) if p == match else answer(p),
            sleep_fn=clock.sleep,
            timeout=2.0,
            clock=clock,
            unique=True,
        )

    msg = str(exc.value)
    assert msg.startswith("no S3 (esp32s3) found within 2 s")
    assert stray in msg
    assert match not in msg
    assert clock.sleeps > 0


@pytest.mark.usefixtures("quiet")
def test_unique_returns_once_the_unresolved_port_turns_out_to_be_another_chip(
    tmp_path: Path,
) -> None:
    match, stray = _ports(tmp_path, "a", "b")
    clock = FakeClock()
    stray_probes = 0

    def probe_fn(p: str) -> tuple[Device | None, str]:
        nonlocal stray_probes
        if p == match:
            return _s3(p)
        stray_probes += 1
        return _silent(p) if stray_probes < 3 else _wrong_chip(p)

    d = wait_for(
        S3,
        ports_fn=lambda: [match, stray],
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=60.0,
        clock=clock,
        unique=True,
    )

    assert d.port == match
    assert stray_probes == 3


@pytest.mark.usefixtures("quiet")
def test_unique_a_matched_port_is_not_probed_again_on_later_passes(tmp_path: Path) -> None:
    match, stray = _ports(tmp_path, "a", "b")
    log: list[str] = []
    clock = FakeClock()

    def probe_fn(p: str) -> tuple[Device | None, str]:
        log.append(p)
        if p == match:
            return _s3(p)
        return _silent(p) if log.count(stray) < 3 else _wrong_chip(p)

    wait_for(
        S3,
        ports_fn=lambda: [match, stray],
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=60.0,
        clock=clock,
        unique=True,
    )

    assert log.count(match) == 1
    assert log.count(stray) == 3


@pytest.mark.usefixtures("quiet")
def test_unique_a_matched_port_that_disappears_is_probed_again_when_it_returns(
    tmp_path: Path,
) -> None:
    replugged, other = _ports(tmp_path, "a", "b")
    clock = FakeClock()
    log: list[str] = []
    passes = 0

    def ports_fn() -> list[str]:
        nonlocal passes
        passes += 1
        if passes == 2:
            Path(replugged).unlink()  # unplugged between passes
        elif passes == 3:
            Path(replugged).touch()  # and plugged back in
        return [replugged, other]

    def probe_fn(p: str) -> tuple[Device | None, str]:
        log.append(p)
        if p == other and passes < 3:
            return _silent(p)  # keeps the pass unresolved, so more passes happen
        return _wrong_chip(p) if p == other else _s3(p)

    d = wait_for(
        S3,
        ports_fn=ports_fn,
        probe_fn=probe_fn,
        sleep_fn=clock.sleep,
        timeout=60.0,
        clock=clock,
        unique=True,
    )

    assert d.port == replugged
    assert log.count(replugged) == 2  # once before it vanished, once after it returned
