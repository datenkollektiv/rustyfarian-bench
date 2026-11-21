"""The kiosk touch reader: fake sysfs tree, packed evdev events, no board."""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from rustyfarian_bench import kiosk_touch as kt
from rustyfarian_bench.kiosk_touch import (
    BTN_TOUCH,
    EV_KEY,
    EV_SYN,
    EVENT,
    SYN_DROPPED,
    TouchReader,
    find_touch,
    is_touch,
)

NAME = "ADS7846 Touchscreen"
EV_ABS = 0x03
SYN_REPORT = 0


def _event(kind: int, code: int, value: int) -> bytes:
    return EVENT.pack(1, 2, kind, code, value)


PRESS = _event(EV_KEY, BTN_TOUCH, 1)
RELEASE = _event(EV_KEY, BTN_TOUCH, 0)
MOVE = _event(EV_ABS, 0, 1234)
REPORT = _event(EV_SYN, SYN_REPORT, 0)
DROPPED = _event(EV_SYN, SYN_DROPPED, 0)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _sysfs(root: Path, names: dict[int, str]) -> Path:
    sysfs = root / "sys-input"
    sysfs.mkdir()
    for number, name in names.items():
        device = sysfs / f"event{number}" / "device"
        device.mkdir(parents=True)
        (device / "name").write_text(name + "\n")
    return sysfs


def _dev(root: Path, numbers: list[int]) -> Path:
    dev = root / "dev"
    (dev / "input").mkdir(parents=True)
    for number in numbers:
        (dev / "input" / f"event{number}").write_bytes(b"")
    return dev


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    return _sysfs(tmp_path, {0: "gpio-keys", 2: NAME}), _dev(tmp_path, [0, 2])


@pytest.fixture
def reader(tree: tuple[Path, Path], clock: Clock) -> Iterator[TouchReader]:
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    yield touch
    touch.close()


def _node(tree: tuple[Path, Path], number: int = 2) -> Path:
    return tree[1] / "input" / f"event{number}"


# find_touch


def test_find_touch_matches_by_substring(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {0: "gpio-keys", 3: NAME})
    assert find_touch(sysfs, "ADS7846") == sysfs / "event3"


def test_find_touch_without_match_is_none(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {0: "gpio-keys", 1: "vc4-hdmi"})
    assert find_touch(sysfs, "ADS7846") is None


def test_find_touch_orders_numerically(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {10: NAME, 2: NAME})
    assert find_touch(sysfs, NAME) == sysfs / "event2"


def test_find_touch_absent_tree_is_none(tmp_path: Path) -> None:
    assert find_touch(tmp_path / "nope", "ADS7846") is None


def test_find_touch_skips_node_without_name(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {5: NAME})
    (sysfs / "event1" / "device").mkdir(parents=True)
    assert find_touch(sysfs, "ADS7846") == sysfs / "event5"


def test_find_touch_ignores_non_event_entries(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {4: NAME})
    (sysfs / "eventual").mkdir()
    (sysfs / "mouse0").mkdir()
    assert find_touch(sysfs, "ADS7846") == sysfs / "event4"


# is_touch


def test_is_touch_press() -> None:
    assert is_touch(MOVE + PRESS + REPORT)


def test_is_touch_ignores_noise() -> None:
    assert not is_touch(MOVE + REPORT + _event(EV_KEY, 0x30, 1))


def test_is_touch_ignores_release() -> None:
    assert not is_touch(RELEASE + REPORT)


def test_is_touch_syn_dropped() -> None:
    assert is_touch(DROPPED)


def test_is_touch_empty_and_partial() -> None:
    assert not is_touch(b"")
    assert not is_touch(PRESS[:-1])


# TouchReader


def test_reader_without_device_is_not_present(tmp_path: Path, clock: Clock) -> None:
    touch = TouchReader(tmp_path / "nope", tmp_path / "dev", "ADS7846", clock)
    assert not touch.present
    assert not touch.touched()
    touch.close()


def test_reader_opens_matching_node(reader: TouchReader) -> None:
    assert reader.present
    assert not reader.touched()


def test_reader_event10_after_event2(tmp_path: Path, clock: Clock) -> None:
    sysfs = _sysfs(tmp_path, {10: NAME, 2: NAME})
    dev = _dev(tmp_path, [2, 10])
    (dev / "input" / "event2").write_bytes(PRESS)
    (dev / "input" / "event10").write_bytes(b"")
    touch = TouchReader(sysfs, dev, "ADS7846", clock)
    try:
        assert touch.touched()
    finally:
        touch.close()


def test_reader_regular_file_press(tree: tuple[Path, Path], clock: Clock) -> None:
    _node(tree).write_bytes(MOVE + PRESS + REPORT)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert touch.touched()
        assert not touch.touched()
    finally:
        touch.close()


def test_reader_regular_file_noise(tree: tuple[Path, Path], clock: Clock) -> None:
    _node(tree).write_bytes(MOVE + REPORT + RELEASE + REPORT)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert not touch.touched()
    finally:
        touch.close()


def test_reader_regular_file_syn_dropped(tree: tuple[Path, Path], clock: Clock) -> None:
    _node(tree).write_bytes(DROPPED)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert touch.touched()
    finally:
        touch.close()


def test_reader_fifo_press_then_quiet(tmp_path: Path, clock: Clock) -> None:
    sysfs = _sysfs(tmp_path, {1: NAME})
    dev = tmp_path / "dev"
    (dev / "input").mkdir(parents=True)
    fifo = dev / "input" / "event1"
    os.mkfifo(fifo)
    touch = TouchReader(sysfs, dev, "ADS7846", clock)
    writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        assert touch.present
        assert not touch.touched()  # empty pipe with a writer: EAGAIN, not a hang
        os.write(writer, MOVE + PRESS)
        assert touch.touched()
        assert not touch.touched()
        os.write(writer, RELEASE + REPORT)
        assert not touch.touched()
    finally:
        os.close(writer)
        touch.close()


def test_reader_fifo_partial_event_split(tmp_path: Path, clock: Clock) -> None:
    sysfs = _sysfs(tmp_path, {1: NAME})
    dev = tmp_path / "dev"
    (dev / "input").mkdir(parents=True)
    fifo = dev / "input" / "event1"
    os.mkfifo(fifo)
    touch = TouchReader(sysfs, dev, "ADS7846", clock)
    writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(writer, PRESS[:10])
        assert not touch.touched()
        os.write(writer, PRESS[10:17])
        assert not touch.touched()
        os.write(writer, PRESS[17:])
        assert touch.touched()
        assert not touch.touched()
    finally:
        os.close(writer)
        touch.close()


def test_reader_partial_split_after_whole_event(tree: tuple[Path, Path], clock: Clock) -> None:
    node = _node(tree)
    node.write_bytes(MOVE + PRESS[:5])
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert not touch.touched()
        with node.open("ab") as out:
            out.write(PRESS[5:])
        assert touch.touched()
    finally:
        touch.close()


def test_reader_press_in_a_later_chunk_is_found(tree: tuple[Path, Path], clock: Clock) -> None:
    # more events than one read returns: the press sits past the first 64-event chunk
    _node(tree).write_bytes(MOVE * (kt.READ_EVENTS + 5) + PRESS)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert touch.touched()
    finally:
        touch.close()


def test_reader_close_twice(reader: TouchReader) -> None:
    reader.close()
    reader.close()
    assert not reader.present


# lost device and rescan


class _Fault:
    """Make os.read raise ENODEV on demand, delegating to the real one otherwise."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fail = False
        self._real = os.read
        monkeypatch.setattr(os, "read", self._read)

    def _read(self, fd: int, size: int) -> bytes:
        if self.fail:
            raise OSError(errno.ENODEV, os.strerror(errno.ENODEV))
        return self._real(fd, size)


def test_enodev_closes_then_rescans_after_interval(
    tree: tuple[Path, Path],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fault = _Fault(monkeypatch)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert touch.present
        fault.fail = True
        with caplog.at_level(logging.WARNING, logger="rustyfarian-bench-kiosk"):
            assert not touch.touched()
        assert not touch.present
        assert "lost" in caplog.text

        fault.fail = False
        _node(tree).write_bytes(PRESS)
        clock.now += kt.RESCAN_SECONDS - 1
        assert not touch.touched()  # too early: no rescan
        assert not touch.present

        clock.now += 2
        assert touch.touched()  # rescanned, reopened, press read
        assert touch.present
    finally:
        touch.close()


def test_lost_device_warns_once(
    tree: tuple[Path, Path],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fault = _Fault(monkeypatch)
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        with caplog.at_level(logging.WARNING, logger="rustyfarian-bench-kiosk"):
            for _ in range(3):
                fault.fail = True
                touch.touched()
                clock.now += kt.RESCAN_SECONDS + 1
                fault.fail = False
                touch.touched()
        assert caplog.text.count("lost") == 1
    finally:
        touch.close()


def test_reopen_drops_carried_partial_event(
    tree: tuple[Path, Path], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault = _Fault(monkeypatch)
    node = _node(tree)
    node.write_bytes(PRESS[:7])
    touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
    try:
        assert not touch.touched()  # 7 bytes carried
        fault.fail = True
        touch.touched()
        fault.fail = False
        node.write_bytes(PRESS)  # a stale 7-byte carry would misalign this press
        clock.now += kt.RESCAN_SECONDS + 1
        assert touch.touched()
    finally:
        touch.close()


def test_missing_device_warns_once_and_retries_no_faster_than_interval(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    sysfs = tmp_path / "sys-input"
    sysfs.mkdir()
    dev = _dev(tmp_path, [2])
    with caplog.at_level(logging.WARNING, logger="rustyfarian-bench-kiosk"):
        touch = TouchReader(sysfs, dev, "ADS7846", clock)
        try:
            for _ in range(3):
                clock.now += kt.RESCAN_SECONDS + 1
                assert not touch.touched()
            assert caplog.text.count("no touch device") == 1

            device = sysfs / "event2" / "device"  # the controller shows up late
            device.mkdir(parents=True)
            (device / "name").write_text(NAME)
            (dev / "input" / "event2").write_bytes(PRESS)
            assert not touch.touched()  # interval not yet over
            clock.now += kt.RESCAN_SECONDS + 1
            assert touch.touched()
            assert touch.present
        finally:
            touch.close()


def test_permission_error_warns_once(
    tree: tuple[Path, Path],
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def deny(path: object, flags: int, *args: object, **kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "open", deny)
    with caplog.at_level(logging.WARNING, logger="rustyfarian-bench-kiosk"):
        touch = TouchReader(tree[0], tree[1], "ADS7846", clock)
        for _ in range(3):
            clock.now += kt.RESCAN_SECONDS + 1
            assert not touch.touched()
    assert not touch.present
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "udev" in warnings[0].getMessage()
    touch.close()


def test_find_touch_skips_a_name_that_is_not_utf8(tmp_path: Path) -> None:
    sysfs = _sysfs(tmp_path, {5: NAME})
    device = sysfs / "event1" / "device"
    device.mkdir(parents=True)
    (device / "name").write_bytes(b"\xff\xfe ADS7846\n")
    assert find_touch(sysfs, "ADS7846") == sysfs / "event5"
