"""Sanity checks of the shared fakes the later reader tests build on."""

from __future__ import annotations

from pathlib import Path

import pytest
from serial import SerialException

from rustyfarian_bench.serial_lines import SerialLines

from .fakes import FakeLines, FakeSerialWorld, shrink_sleep, wait_until


def test_fake_lines_scripted_link_loss_is_recorded() -> None:
    lines = FakeLines("a")
    lines.lost = True
    assert lines.link_lost_since(1.5)
    assert lines.lost_since_args == [1.5]
    assert lines.get(0) == "a"
    with lines:
        pass
    assert lines.exited == 1


def test_fake_world_scripts_one_entry_per_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = FakeSerialWorld([b"ab", b"c\r\n", SerialException("gone")], [b"d\n"])
    world.install(monkeypatch)
    shrink_sleep(monkeypatch)
    with SerialLines("/dev/fake", tmp_path / "log", echo=False, reset=False) as lines:
        assert lines.get(2) == "abc"
        assert lines.get(2) == "d"
        assert wait_until(world.exhausted.is_set)
    assert len(world.opened) == 2
    assert world.opened[0].events[0] == "open dtr=False rts=False"
