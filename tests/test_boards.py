"""`boards`: port listing, verified MACs, the probe registry and the probe child. No board."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from serial.tools import list_ports

from rustyfarian_bench import boards, device, probe_port
from rustyfarian_bench.boards import BoardRegistry, PortInfo
from rustyfarian_bench.config import ConfigError
from rustyfarian_bench.remote import Call

MAC = "02:00:00:12:00:b8"
NATIVE_VID, NATIVE_PID = 0x303A, 0x1001


def port_info(
    port: str = "/dev/ttyUSB0",
    vid: int | None = 0x10C4,
    pid: int | None = 0xEA60,
    serial: str | None = "0001",
    location: str | None = "1-1.3",
) -> PortInfo:
    return PortInfo(port, None, vid, pid, serial, location)


def native(mac: str = "AA:BB:CC:DD:EE:01", port: str = "/dev/ttyACM0") -> PortInfo:
    return port_info(port, NATIVE_VID, NATIVE_PID, mac)


@dataclass
class Bench:
    """A fake host: the ports now attached and each device node's inode."""

    ports: list[PortInfo] = field(default_factory=list)
    inodes: dict[str, int] = field(default_factory=dict)
    now: float = 1000.0

    def add(self, info: PortInfo) -> PortInfo:
        self.ports.append(info)
        self.inodes.setdefault(info.port, 100 + len(self.inodes))
        return info

    def replug(self, port: str) -> None:
        self.inodes[port] += 1000

    def remove(self, port: str) -> None:
        self.ports = [p for p in self.ports if p.port != port]
        del self.inodes[port]

    def lister(self) -> list[PortInfo]:
        return list(self.ports)

    def stat(self, path: str) -> os.stat_result:
        if path not in self.inodes:
            raise FileNotFoundError(path)
        return os.stat_result((0o20600, self.inodes[path], 7, 1, 0, 0, 0, 0, 0, 0))

    def monotonic(self) -> float:
        return self.now

    def registry(self, **kw: Any) -> BoardRegistry:
        return BoardRegistry(
            self.lister, kw.pop("parts_file", None), stat=self.stat, monotonic=self.monotonic, **kw
        )


def probe_code(chip: str | None = "esp32", mac: str | None = MAC, error: str | None = None) -> str:
    return (
        f"import json; print(json.dumps({{'chip': {chip!r}, 'mac': {mac!r}, 'error': {error!r}}}))"
    )


def child_argv(code: str) -> list[str]:
    """A probe child command; the port arrives as `sys.argv[1]`."""
    return [sys.executable, "-c", code]


@pytest.fixture
def bench() -> Bench:
    return Bench()


class Queue:
    """Stands in for the worker's queue: calls are kept and run by the test."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def submit(self, call: Call) -> None:
        self.calls.append(call)

    def run_all(self) -> None:
        calls, self.calls = self.calls, []
        for call in calls:
            call(lambda proc: None)


def board_of(listing: dict[str, Any], port: str) -> dict[str, Any]:
    [board] = [b for b in listing["boards"] if b["port"] == port]
    assert isinstance(board, dict)
    return board


def probe_once(reg: BoardRegistry, queue: Queue, ports: list[str] | None = None) -> dict[str, Any]:
    state = reg.request_probe(ports, queue.submit)
    queue.run_all()
    got = reg.get_probe(state.id)
    assert got is not None
    return got


# -- listing and native verification -------------------------------------------------------------


def test_a_native_usb_serial_that_is_a_mac_is_verified_by_it(bench: Bench) -> None:
    bench.add(native("AA:BB:CC:DD:EE:01"))

    board = board_of(bench.registry().snapshot(), "/dev/ttyACM0")

    assert board["verified"] is True
    assert board["mac"] == "aa:bb:cc:dd:ee:01"
    assert board["mac_source"] == "usb-serial"
    assert board["chip"] is None
    assert board["vid_pid"] == "303a:1001"
    assert board["verified_at"] is not None


@pytest.mark.parametrize("serial", [None, "", "0001", "AA:BB:CC:DD:EE", "not-a-mac-at-all!!"])
def test_a_native_port_without_a_mac_serial_is_unverified(bench: Bench, serial: str | None) -> None:
    bench.add(port_info("/dev/ttyACM0", NATIVE_VID, NATIVE_PID, serial))

    board = board_of(bench.registry().snapshot(), "/dev/ttyACM0")

    assert (board["verified"], board["mac"], board["chip"], board["name"]) == (
        False,
        None,
        None,
        None,
    )


def test_only_pid_1001_counts_as_native() -> None:
    other = port_info("/dev/ttyACM0", NATIVE_VID, 0x0002, "AA:BB:CC:DD:EE:01")

    assert boards.native_mac(other) is None


def test_a_bridge_is_unverified_whatever_its_serial(bench: Bench) -> None:
    bench.add(port_info(serial="AA:BB:CC:DD:EE:01"))

    board = board_of(bench.registry().snapshot(), "/dev/ttyUSB0")

    assert board["verified"] is False
    assert board["mac"] is None
    assert board["usb_serial"] == "AA:BB:CC:DD:EE:01"


def test_an_empty_bench_lists_nothing(bench: Bench) -> None:
    assert bench.registry().snapshot() == {"schema": 1, "parts": {"loaded": False}, "boards": []}


def test_the_listing_carries_the_documented_fields(bench: Bench) -> None:
    bench.add(port_info())

    [board] = bench.registry().snapshot()["boards"]

    assert set(board) == {
        "port",
        "by_id",
        "vid_pid",
        "usb_serial",
        "verified",
        "mac",
        "chip",
        "name",
        "mac_source",
        "verified_at",
    }


# -- names ----------------------------------------------------------------------------------------


@pytest.fixture
def parts_file(tmp_path: Path) -> Path:
    path = tmp_path / "parts.toml"
    path.write_text(
        '[[parts]]\nname = "bench-wroom32-01"\nmac = "02:00:00:12:00:B8"\nbarcode = "x"\n',
        encoding="utf-8",
    )
    return path


def test_a_probed_board_is_named_from_the_parts_by_lowercase_mac(
    bench: Bench, parts_file: Path
) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(parts_file=parts_file, probe_argv=child_argv(probe_code())), Queue()

    probe_once(reg, queue)
    listing = reg.snapshot()

    assert listing["parts"] == {"loaded": True}
    assert board_of(listing, "/dev/ttyUSB0")["name"] == "bench-wroom32-01"


def test_without_a_parts_file_no_board_has_a_name(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()

    probe_once(reg, queue)

    board = board_of(reg.snapshot(), "/dev/ttyUSB0")
    assert board["verified"] is True
    assert board["name"] is None


def test_a_mac_not_in_the_parts_has_no_name(bench: Bench, parts_file: Path) -> None:
    bench.add(native("AA:BB:CC:DD:EE:01"))

    assert (
        board_of(bench.registry(parts_file=parts_file).snapshot(), "/dev/ttyACM0")["name"] is None
    )


def test_the_parts_file_is_read_again_on_every_listing(bench: Bench, parts_file: Path) -> None:
    bench.add(native("02:00:00:12:00:B8"))
    reg = bench.registry(parts_file=parts_file)
    assert board_of(reg.snapshot(), "/dev/ttyACM0")["name"] == "bench-wroom32-01"

    parts_file.write_text('[[parts]]\nname = "renamed"\nmac = "02:00:00:12:00:b8"\n')

    assert board_of(reg.snapshot(), "/dev/ttyACM0")["name"] == "renamed"


def test_a_parts_file_that_broke_is_a_config_error_on_listing(
    bench: Bench, parts_file: Path
) -> None:
    reg = bench.registry(parts_file=parts_file)
    parts_file.write_text("[[parts")

    with pytest.raises(ConfigError):
        reg.snapshot()


# -- probing and the fingerprint -----------------------------------------------------------------


def test_a_probe_verifies_a_bridge_with_chip_and_source(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code("esp32", MAC))), Queue()

    done = probe_once(reg, queue)

    assert done["state"] == "done"
    assert done["results"] == [
        {"port": "/dev/ttyUSB0", "state": "done", "chip": "esp32", "mac": MAC, "error": None}
    ]
    board = board_of(reg.snapshot(), "/dev/ttyUSB0")
    assert (board["verified"], board["mac"], board["chip"], board["mac_source"]) == (
        True,
        MAC,
        "esp32",
        "probe",
    )


def test_a_replug_after_the_probe_makes_the_board_unverified_again(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)

    bench.replug("/dev/ttyUSB0")

    board = board_of(reg.snapshot(), "/dev/ttyUSB0")
    assert (board["verified"], board["mac"], board["chip"]) == (False, None, None)


def test_a_changed_usb_serial_or_location_invalidates_the_probe(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)

    bench.ports = [port_info(serial="0002")]

    assert board_of(reg.snapshot(), "/dev/ttyUSB0")["verified"] is False


def test_a_vanished_port_is_dropped_and_comes_back_unverified(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)
    bench.remove("/dev/ttyUSB0")
    assert reg.snapshot()["boards"] == []

    bench.add(port_info())  # same fingerprint inputs, but the old record is gone

    assert board_of(reg.snapshot(), "/dev/ttyUSB0")["verified"] is False


def test_a_probe_racing_a_replug_is_discarded(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench.add(port_info())

    def child_that_replugs(*a: Any, **kw: Any) -> boards.ProbeOutcome:
        bench.replug("/dev/ttyUSB0")
        return boards.ProbeOutcome(chip="esp32", mac=MAC)

    monkeypatch.setattr(boards, "run_probe_child", child_that_replugs)
    reg, queue = bench.registry(), Queue()

    done = probe_once(reg, queue)

    [result] = done["results"]
    assert (result["mac"], result["error"]) == (None, "port changed during the probe")
    assert board_of(reg.snapshot(), "/dev/ttyUSB0")["verified"] is False


def test_a_port_that_vanishes_before_its_turn_reports_it(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    state = reg.request_probe(None, queue.submit)
    bench.remove("/dev/ttyUSB0")

    queue.run_all()

    got = reg.get_probe(state.id)
    assert got is not None
    assert got["results"][0]["error"] == "port is gone"


def test_a_native_serial_that_differs_from_the_probe_reports_the_probe_and_warns(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.add(native("AA:BB:CC:DD:EE:01"))
    reg, queue = bench.registry(probe_argv=child_argv(probe_code("esp32s3", MAC))), Queue()
    probe_once(reg, queue)

    board = board_of(reg.snapshot(), "/dev/ttyACM0")

    assert (board["mac"], board["chip"], board["mac_source"]) == (MAC, "esp32s3", "probe")
    assert "differs" in capsys.readouterr().err


def test_a_native_serial_that_matches_the_probe_does_not_warn_and_gains_the_chip(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.add(native(MAC.upper()))
    reg, queue = bench.registry(probe_argv=child_argv(probe_code("esp32s3", MAC))), Queue()
    probe_once(reg, queue)

    board = board_of(reg.snapshot(), "/dev/ttyACM0")

    assert (board["mac"], board["chip"]) == (MAC, "esp32s3")
    assert capsys.readouterr().err == ""


def test_a_failed_probe_leaves_the_board_unverified_with_the_error(bench: Bench) -> None:
    bench.add(port_info())
    code = probe_code(None, None, "port held by another process")
    reg, queue = bench.registry(probe_argv=child_argv(code)), Queue()

    done = probe_once(reg, queue)

    assert done["results"][0]["error"] == "port held by another process"
    assert board_of(reg.snapshot(), "/dev/ttyUSB0")["verified"] is False


# -- requests: allowlist, dedupe, cooldown, caps --------------------------------------------------


@pytest.mark.parametrize("bad", ["/dev/ttyUSB9", "--help", "-x", "/etc/passwd", ""])
def test_a_port_outside_the_listing_is_refused_and_nothing_is_queued(
    bench: Bench, bad: str
) -> None:
    bench.add(port_info())
    queue = Queue()

    with pytest.raises(boards.UnknownPort):
        bench.registry().request_probe([bad], queue.submit)

    assert queue.calls == []


def test_one_refused_port_queues_none_of_the_request(bench: Bench) -> None:
    bench.add(port_info())
    queue = Queue()

    with pytest.raises(boards.UnknownPort):
        bench.registry().request_probe(["/dev/ttyUSB0", "--x"], queue.submit)

    assert queue.calls == []


def test_every_listed_port_gets_its_own_task_when_none_are_named(bench: Bench) -> None:
    bench.add(port_info("/dev/ttyUSB0"))
    bench.add(port_info("/dev/ttyUSB1"))
    queue = Queue()

    state = bench.registry().request_probe(None, queue.submit)

    assert len(queue.calls) == 2
    assert [r.port for r in state.results] == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    assert state.state == "queued"


def test_a_listing_without_ports_gives_a_finished_empty_probe(bench: Bench) -> None:
    queue = Queue()

    state = bench.registry().request_probe(None, queue.submit)

    assert (state.state, state.results, queue.calls) == ("done", [], [])


def test_a_second_request_for_a_queued_port_reuses_the_probe(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(), Queue()

    first = reg.request_probe(["/dev/ttyUSB0"], queue.submit)
    second = reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert second.id == first.id
    assert len(queue.calls) == 1


def test_a_request_overlapping_a_queued_port_adds_only_the_new_port(bench: Bench) -> None:
    bench.add(port_info("/dev/ttyUSB0"))
    bench.add(port_info("/dev/ttyUSB1"))
    reg, queue = bench.registry(), Queue()

    first = reg.request_probe(["/dev/ttyUSB0"], queue.submit)
    both = reg.request_probe(None, queue.submit)

    assert both.id != first.id
    assert len(queue.calls) == 2
    assert both.results[0] is first.results[0]


def test_a_duplicate_port_in_one_request_is_probed_once(bench: Bench) -> None:
    bench.add(port_info())
    queue = Queue()

    state = bench.registry().request_probe(["/dev/ttyUSB0", "/dev/ttyUSB0"], queue.submit)

    assert len(state.results) == 1
    assert len(queue.calls) == 1


def test_a_port_probed_within_the_cooldown_answers_from_the_result_without_a_reset(
    bench: Bench,
) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)
    bench.now += 29.0

    state = reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert queue.calls == []
    assert state.state == "done"
    assert state.results[0].mac == MAC


def test_after_the_cooldown_the_port_is_probed_again(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)
    bench.now += 31.0

    reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert len(queue.calls) == 1


def test_a_replug_inside_the_cooldown_is_probed_again(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)
    bench.replug("/dev/ttyUSB0")

    reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert len(queue.calls) == 1


def test_a_probe_answered_from_the_cache_says_no_reset_and_a_fresh_one_says_reset(
    bench: Bench,
) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    assert probe_once(reg, queue)["reset"] is True

    cached = reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert cached.reset is False
    got = reg.get_probe(cached.id)
    assert got is not None and got["reset"] is False


def test_a_failed_probe_is_not_cached_and_is_retried_at_once(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code(None, None, "busy"))), Queue()
    probe_once(reg, queue)

    state = reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert len(queue.calls) == 1
    assert state.reset is True


def test_a_port_that_vanished_loses_its_cooldown_so_its_successor_is_probed(
    bench: Bench,
) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    probe_once(reg, queue)
    inode = bench.inodes["/dev/ttyUSB0"]
    bench.remove("/dev/ttyUSB0")
    reg.snapshot()  # a listing while the port is gone
    bench.add(port_info())  # same serial and location, and the same inode on purpose
    bench.inodes["/dev/ttyUSB0"] = inode

    reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    assert len(queue.calls) == 1


def test_only_the_last_32_probes_are_remembered(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(cooldown=0.0, probe_argv=child_argv(probe_code())), Queue()
    ids = []
    for _ in range(boards.PROBE_KEEP + 3):
        ids.append(reg.request_probe(["/dev/ttyUSB0"], queue.submit).id)
        queue.run_all()

    assert reg.get_probe(ids[0]) is None
    assert reg.get_probe(ids[-1]) is not None


@pytest.mark.parametrize(
    "probe_id", ["", "p-1", "p-ZZZZZZZZ", "../etc", "p-0123456789", "x-01234567"]
)
def test_a_malformed_probe_id_is_unknown(bench: Bench, probe_id: str) -> None:
    assert bench.registry().get_probe(probe_id) is None


def test_an_unknown_wellformed_probe_id_is_unknown(bench: Bench) -> None:
    assert bench.registry().get_probe("p-0123abcd") is None


def test_fail_pending_ends_queued_probes_and_a_late_call_does_nothing(bench: Bench) -> None:
    bench.add(port_info())
    reg, queue = bench.registry(probe_argv=child_argv(probe_code())), Queue()
    state = reg.request_probe(["/dev/ttyUSB0"], queue.submit)

    reg.fail_pending("server stopped")
    queue.run_all()

    got = reg.get_probe(state.id)
    assert got is not None
    assert (got["state"], got["results"][0]["error"]) == ("done", "server stopped")
    assert board_of(reg.snapshot(), "/dev/ttyUSB0")["verified"] is False


def test_the_probe_state_follows_its_ports() -> None:
    a, b = boards.PortResult("a"), boards.PortResult("b")
    state = boards.ProbeState("p-00000000", [a, b])
    assert state.state == "queued"
    a.state = "running"
    assert state.state == "running"
    a.state = b.state = "done"
    assert state.state == "done"


# -- the probe child -------------------------------------------------------------------------------


def test_the_child_result_is_parsed_from_its_last_json_line() -> None:
    code = (
        "import json; print('noise'); "
        "print(json.dumps({'chip': 'esp32c3', 'mac': 'AA:BB:CC:DD:EE:FF', 'error': None}))"
    )

    out = boards.run_probe_child("/dev/x", 20, argv=child_argv(code))

    assert out == boards.ProbeOutcome("esp32c3", "aa:bb:cc:dd:ee:ff", None)


def test_the_child_gets_the_port_as_its_argument() -> None:
    code = (
        "import json, sys; print(json.dumps({'chip': 'esp32', 'mac': '"
        + MAC
        + "', 'error': sys.argv[1]}))"
    )

    out = boards.run_probe_child("/dev/ttyUSB7", 20, argv=child_argv(code))

    assert out.error == "/dev/ttyUSB7"


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("pass", "printed no result"),
        ("print('not json')", "printed no result"),
        ("import sys; sys.exit(3)", "printed no result"),
        ("print('[1, 2]')", "printed no result"),
        ('print(\'{"chip": "esp32", "mac": null, "error": null}\')', "no MAC"),
        ('print(\'{"chip": "ESP 32;rm", "mac": "aa:bb:cc:dd:ee:ff", "error": null}\')', "no chip"),
        ('print(\'{"chip": null, "mac": null, "error": "boom\\\\u0007"}\')', "boom"),
    ],
    ids=["silent", "garbage", "crash", "not-an-object", "no-mac", "odd-chip", "child-error"],
)
def test_a_child_that_does_not_report_a_chip_and_mac_is_an_error(code: str, error: str) -> None:
    out = boards.run_probe_child("/dev/x", 20, argv=child_argv(code))

    assert out.chip is None and out.mac is None
    assert out.error is not None and error in out.error
    assert out.error.isprintable()


def test_a_child_past_its_timeout_is_ended_and_reported() -> None:
    told: list[bool] = []

    out = boards.run_probe_child(
        "/dev/x",
        0.3,
        lambda proc: told.append(proc is not None),
        child_argv("import time; time.sleep(60)"),
    )

    assert out.error == "timed out"
    assert told == [True, False]


def test_a_command_that_cannot_start_is_an_error_not_an_exception() -> None:
    out = boards.run_probe_child("/dev/x", 5, argv=["/nonexistent/probe-binary"])

    assert out.error is not None and "cannot start" in out.error


# -- the child's entry point ---------------------------------------------------------------------


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    probe: Callable[[str], Any],
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(device, "probe", probe)
    code = probe_port.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_probe_port_prints_one_json_line_for_a_chip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_main(
        monkeypatch, capsys, ["/dev/ttyUSB0"], lambda p: (device.Device(p, "esp32", MAC), "")
    )

    assert (code, out) == (0, {"chip": "esp32", "mac": MAC, "error": None})


def test_probe_port_reports_espflash_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_main(monkeypatch, capsys, ["/dev/ttyUSB0"], lambda p: (None, device.BUSY))

    assert (code, out["error"], out["chip"]) == (1, device.BUSY, None)


def test_probe_port_reports_a_missing_espflash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(port: str) -> Any:
        raise device.MissingTool("cannot run 'espflash'")

    code, out = run_main(monkeypatch, capsys, ["/dev/ttyUSB0"], missing)

    assert code == 1
    assert "espflash" in out["error"]


@pytest.mark.parametrize("argv", [[], ["--help"], ["-x"], ["a", "b"]])
def test_probe_port_never_passes_a_flag_or_a_wrong_count_to_espflash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    def never(port: str) -> Any:
        raise AssertionError("espflash must not run")

    code, out = run_main(monkeypatch, capsys, argv, never)

    assert code == 1
    assert out["error"]


def test_probe_port_runs_as_a_module_without_a_board() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "rustyfarian_bench.probe_port", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(done.stdout.splitlines()[-1])["error"]


# -- listing real-looking comports ---------------------------------------------------------------


def comport(
    device_name: str, vid: int | None = 0x10C4, pid: int | None = 0xEA60, serial: str | None = "1"
) -> SimpleNamespace:
    return SimpleNamespace(
        device=device_name, vid=vid, pid=pid, serial_number=serial, location="1-1"
    )


def test_list_ports_keeps_usb_serial_ports_sorted_and_drops_the_host_consoles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = [
        comport("/dev/ttyUSB1"),
        comport("/dev/ttyS0", None, None, None),
        comport("/dev/ttyAMA0", None, None, None),
        comport("/dev/ttyACM0", NATIVE_VID, NATIVE_PID, MAC),
        comport("/dev/ttyUSB2", None, None, None),  # prefix match without USB ids
        comport("/dev/ttyXYZ", None, None, None),  # neither USB nor a known prefix
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: fake)
    monkeypatch.setattr(boards, "_by_id_links", lambda: {"/dev/ttyUSB1": "/dev/serial/by-id/x"})

    got = boards.list_ports()

    assert [p.port for p in got] == ["/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyUSB2"]
    assert got[1].by_id == "/dev/serial/by-id/x"
    assert got[1].vid_pid == "10c4:ea60"
    assert got[0].vid_pid == "303a:1001"
    assert got[2].vid_pid is None


def test_list_ports_prefers_the_cu_twin_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = [comport("/dev/tty.usbserial-1"), comport("/dev/cu.usbserial-1")]
    monkeypatch.setattr(list_ports, "comports", lambda: fake)
    monkeypatch.setattr(boards, "_by_id_links", dict)
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/cu.usbserial-1")

    assert [p.port for p in boards.list_ports()] == ["/dev/cu.usbserial-1"]


def test_the_fingerprint_is_none_for_a_node_that_cannot_be_examined(bench: Bench) -> None:
    assert boards.fingerprint(port_info("/dev/nope"), bench.stat) is None
