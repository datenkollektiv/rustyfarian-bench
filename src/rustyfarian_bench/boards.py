"""The boards attached to the bench host: listing, verified MACs, names from `parts.toml`.

A MAC is reported only when it is verified. An Espressif native USB-JTAG port (`303a:1001`)
carries the base MAC as its USB serial; any other port is verified by a probe (`espflash
board-info` in a child process) for as long as the port's fingerprint stays the same. The cache
lives in memory only: a restart forgets it, which is the safe direction after a board swap.
Stdlib plus pyserial: no web framework is imported here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import tomllib
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from serial.tools import list_ports as _comports

from . import config, device, probe_lines
from .config import ConfigError
from .remote import OnProc, _check_name

NATIVE = (0x303A, 0x1001)  # Espressif USB-JTAG/serial: its USB serial is the base MAC
PROBE_ID = re.compile(r"p-[0-9a-f]{8}")
PROBE_KEEP = 32  # probes remembered, oldest evicted
PROBE_TIMEOUT = device.PROBE_TIMEOUT + 15.0  # seconds: espflash's limit plus interpreter start
MAX_PARTS_FILE = 256 * 1024  # bytes; a catalogue of labelled boards stays far below this
COOLDOWN = 30.0  # seconds after a probe during which its port answers from the cache
SCHEMA = 1
NOTE = "espflash board-info resets each probed chip; a running app restarts"
# Console UARTs of the host itself: not boards, and a probe must never open them.
_NOT_BOARDS = ("/dev/ttyS", "/dev/ttyAMA")
_BY_ID_DIR = Path("/dev/serial/by-id")
_CHIP = re.compile(r"[a-z0-9]{1,20}")
_ERROR_MAX = 200

# (st_dev, st_ino, usb_serial, "vid:pid", location): changes when a board is replugged or swapped.
Fingerprint = tuple[int, int, str | None, str, str | None]


class UnknownPort(ConfigError):
    """A requested port is not in the current listing."""


@dataclass(frozen=True)
class Target:
    """Where a flash job goes: the port, and the verified MAC and part name when known."""

    port: str
    mac: str | None
    name: str | None


@dataclass(frozen=True)
class PortInfo:
    port: str
    by_id: str | None
    vid: int | None
    pid: int | None
    usb_serial: str | None
    location: str | None = None  # the USB bus path; only for the fingerprint

    @property
    def vid_pid(self) -> str | None:
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"


@dataclass(frozen=True)
class Part:
    name: str
    mac: str  # lowercase


def _by_id_links() -> dict[str, str]:
    links: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for link in sorted(_BY_ID_DIR.iterdir()):
            links[os.path.realpath(link)] = str(link)
    return links


def list_ports() -> list[PortInfo]:
    """Every USB serial port (and the usual USB-serial device names), sorted by port."""
    by_id = _by_id_links()
    found: dict[str, PortInfo] = {}
    for p in _comports.comports():
        device_name: str = p.device
        if device_name.startswith(_NOT_BOARDS):
            continue
        if p.vid is None and not device_name.startswith(device.PORT_PREFIXES):
            continue
        # macOS lists each device as tty.* and cu.*; cu.* is the one to open.
        if device_name.startswith("/dev/tty.") and os.path.exists(
            device_name.replace("/dev/tty.", "/dev/cu.")
        ):
            continue
        found[device_name] = PortInfo(
            port=device_name,
            by_id=by_id.get(os.path.realpath(device_name)),
            vid=p.vid,
            pid=p.pid,
            usb_serial=p.serial_number or None,
            location=p.location or None,
        )
    return [found[k] for k in sorted(found)]


def native_mac(info: PortInfo) -> str | None:
    """The base MAC a native USB-JTAG port reports as its USB serial, lowercase, or None."""
    if (info.vid, info.pid) != NATIVE:
        return None
    serial = info.usb_serial
    if serial and config.MAC.fullmatch(serial):
        return serial.lower()
    return None


def fingerprint(
    info: PortInfo, stat: Callable[[str], os.stat_result] = os.stat
) -> Fingerprint | None:
    """Identity of what sits behind the port now; None when the node cannot be examined."""
    try:
        st = stat(info.port)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, info.usb_serial, info.vid_pid or "", info.location)


def load_parts(path: Path) -> dict[str, Part]:
    """Read `parts.toml` (`[[parts]]` with `name`, `mac`, `barcode`) into parts keyed by MAC.

    `barcode` and any other key are ignored. Raises ConfigError for a file that is missing,
    malformed, empty, or has a bad or duplicate name or MAC; the message names the entry and
    field, never the file's content.
    """
    try:
        if path.stat().st_size > MAX_PARTS_FILE:
            raise ConfigError(f"{path}: more than {MAX_PARTS_FILE} bytes")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: cannot be read ({type(e).__name__})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML ({e})") from None
    except RecursionError:
        raise ConfigError(f"{path}: nested too deeply") from None
    entries = raw.get("parts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: expected at least one [[parts]] entry")
    parts: dict[str, Part] = {}
    names: set[str] = set()
    for i, entry in enumerate(entries, 1):
        where = f"{path}: parts #{i}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: expected a table")
        name = _check_name(entry.get("name"), f"{where}: name")
        mac = entry.get("mac")
        if not isinstance(mac, str) or not config.MAC.fullmatch(mac):
            raise ConfigError(f"{where}: mac: expected aa:bb:cc:dd:ee:ff")
        mac = mac.lower()
        if name in names:
            raise ConfigError(f"{where}: name {name!r} is listed twice")
        if mac in parts:
            raise ConfigError(f"{where}: mac {mac} is listed twice")
        names.add(name)
        parts[mac] = Part(name=name, mac=mac)
    return parts


@dataclass
class ProbeOutcome:
    chip: str | None = None
    mac: str | None = None
    error: str | None = None


def _clean(text: str) -> str:
    return "".join(c if c.isprintable() else " " for c in text)[:_ERROR_MAX]


def _parse_outcome(output: str) -> ProbeOutcome:
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or not {"chip", "mac", "error"} <= data.keys():
            continue
        chip, mac, error = data["chip"], data["mac"], data["error"]
        if isinstance(error, str) and error:
            return ProbeOutcome(error=_clean(error))
        if isinstance(chip, str) and _CHIP.fullmatch(chip):
            if isinstance(mac, str) and config.MAC.fullmatch(mac):
                return ProbeOutcome(chip=chip, mac=mac.lower())
            return ProbeOutcome(error="board-info printed no MAC")
        return ProbeOutcome(error="probe child returned no chip")
    return ProbeOutcome(error="probe child printed no result")


def run_probe_child(
    port: str,
    timeout: float = PROBE_TIMEOUT,
    on_proc: OnProc | None = None,
    argv: Sequence[str] | None = None,
) -> ProbeOutcome:
    """Run `python -m rustyfarian_bench.probe_port <port>` in its own session; never raises.

    `argv` replaces the command before the port (the test seam). `on_proc` is told of the child
    and, with None, once it is gone, so the worker's `stop` can end it.
    """
    prefix = list(argv) if argv else [sys.executable, "-m", "rustyfarian_bench.probe_port"]
    try:
        proc = subprocess.Popen(
            [*prefix, port],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own group: a timeout or stop ends all of it
            env={**device.ESPFLASH_ENV, "PYTHONUNBUFFERED": "1"},
        )
    except OSError as e:
        return ProbeOutcome(error=_clean(f"cannot start the probe ({e.strerror or e})"))
    if on_proc:
        on_proc(proc)
    out = b""
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        probe_lines.end_group(proc)
        if on_proc:
            on_proc(None)
    if timed_out:
        return ProbeOutcome(error="timed out")
    return _parse_outcome(out.decode("utf-8", errors="replace"))


@dataclass
class PortResult:
    port: str
    state: str = "queued"  # queued, running, done
    chip: str | None = None
    mac: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "state": self.state,
            "chip": self.chip,
            "mac": self.mac,
            "error": self.error,
        }


@dataclass
class ProbeState:
    id: str
    results: list[PortResult]
    reset: bool = True  # False when every port answered from the cooldown cache

    @property
    def state(self) -> str:
        states = {r.state for r in self.results}
        if states <= {"done"}:
            return "done"
        return "queued" if states == {"queued"} else "running"


@dataclass(frozen=True)
class _Record:
    mac: str
    chip: str
    at: str
    fp: Fingerprint


@dataclass
class _Last:
    at: float
    fp: Fingerprint | None
    result: PortResult = field(repr=False)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BoardRegistry:
    """Lists the attached boards with their verified identity and runs the probes.

    `lister` returns the current ports (a test seam); `parts_file` is re-read on every `snapshot`.
    Handlers (the thread pool) and the worker thread both use it, so state sits under a lock.
    """

    def __init__(
        self,
        lister: Callable[[], list[PortInfo]] | None = None,
        parts_file: Path | None = None,
        *,
        probe_argv: Sequence[str] | None = None,
        probe_timeout: float = PROBE_TIMEOUT,
        cooldown: float = COOLDOWN,
        stat: Callable[[str], os.stat_result] = os.stat,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], str] = _now,
    ):
        self._lister = lister or list_ports
        self._parts_file = parts_file
        self._probe_argv = probe_argv
        self._probe_timeout = probe_timeout
        self._cooldown = cooldown
        self._stat = stat
        self._monotonic = monotonic
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}
        self._active: dict[str, PortResult] = {}
        self._last: dict[str, _Last] = {}
        self._probes: OrderedDict[str, ProbeState] = OrderedDict()
        self._warned: set[tuple[str, str, str]] = set()

    # -- listing ------------------------------------------------------------------------------

    def parts(self) -> dict[str, Part] | None:
        """The parts table, re-read now; None without a parts file; ConfigError when it broke."""
        return None if self._parts_file is None else load_parts(self._parts_file)

    def snapshot(self) -> dict[str, Any]:
        """The `GET /boards` reply. Raises ConfigError when the parts file cannot be read."""
        parts = self.parts()
        infos = self._lister()
        stamp = self._clock()
        with self._lock:
            # Under the lock: a probe finishing meanwhile must not meet an older fingerprint.
            fps = {i.port: fingerprint(i, self._stat) for i in infos}
            for port in [p for p in self._records if p not in fps]:
                del self._records[port]
            for port in [p for p in self._last if p not in fps]:
                del self._last[port]  # a vanished port's cooldown must not vouch for its successor
            boards = [self._board(i, fps[i.port], parts, stamp) for i in infos]
        return {"schema": SCHEMA, "parts": {"loaded": parts is not None}, "boards": boards}

    def resolve_target(self, port: str | None, part: str | None) -> Target:
        """The board a flash job addresses, by `port` or by the `part` name; exactly one.

        A part must name exactly one attached, verified board: an unverified port could hold
        another board than the one the name stands for. A port passes the MAC on only if verified.
        """
        if (port is None) == (part is None):
            raise UnknownPort("give exactly one of port and part")
        snap = self.snapshot()
        listed: list[dict[str, Any]] = snap["boards"]
        if port is not None:
            for b in listed:
                if b["port"] == port:
                    ok = b["verified"]
                    return Target(port, b["mac"] if ok else None, b["name"] if ok else None)
            raise UnknownPort("port: not in the current listing")
        if not snap["parts"]["loaded"]:
            raise UnknownPort("part: the bench has no parts file")
        found = [b for b in listed if b["verified"] and b["name"] == part]
        if not found:
            raise UnknownPort(
                f"part: {part!r} is not attached or not verified; run `boards --probe`"
            )
        if len(found) > 1:
            raise UnknownPort(f"part: {part!r} is ambiguous, several attached boards carry it")
        return Target(found[0]["port"], found[0]["mac"], part)

    def _board(
        self, info: PortInfo, fp: Fingerprint | None, parts: dict[str, Part] | None, stamp: str
    ) -> dict[str, Any]:
        record = self._records.get(info.port)
        if record is not None and record.fp != fp:
            del self._records[info.port]  # replugged or swapped since the probe
            record = None
        native = native_mac(info)
        mac = chip = source = at = None
        if native is not None:
            mac, source, at = native, "usb-serial", stamp
        if record is not None:
            if native is not None and native != record.mac:
                key = (info.port, native, record.mac)
                if key not in self._warned:
                    self._warned.add(key)
                    print(
                        f"warning: {info.port}: USB serial {native} differs from probed "
                        f"MAC {record.mac}; reporting the probe",
                        file=sys.stderr,
                    )
            mac, chip, source, at = record.mac, record.chip, "probe", record.at
        name = parts[mac].name if parts is not None and mac is not None and mac in parts else None
        return {
            "port": info.port,
            "by_id": info.by_id,
            "vid_pid": info.vid_pid,
            "usb_serial": info.usb_serial,
            "verified": mac is not None,
            "mac": mac,
            "chip": chip,
            "name": name,
            "mac_source": source,
            "verified_at": at,
        }

    # -- probing ------------------------------------------------------------------------------

    def request_probe(
        self, ports: Sequence[str] | None, submit: Callable[[Callable[[OnProc], None]], None]
    ) -> ProbeState:
        """Queue one task per port through `submit`; raises UnknownPort for a port not listed.

        A port with a probe already queued or running joins that probe, and a port probed less
        than `cooldown` seconds ago (same fingerprint) answers from that result: no new reset.
        """
        infos = {i.port: i for i in self._lister()}
        wanted = list(infos) if ports is None else list(dict.fromkeys(ports))
        for port in wanted:
            if port not in infos:
                raise UnknownPort("port: not in the current listing")
        fps = {port: fingerprint(infos[port], self._stat) for port in wanted}
        now = self._monotonic()
        fresh: list[PortResult] = []
        results: list[PortResult] = []
        with self._lock:
            for port in wanted:
                last = self._last.get(port)
                if port in self._active:
                    results.append(self._active[port])
                elif (
                    last is not None
                    and fps[port] is not None
                    and last.fp == fps[port]
                    and now - last.at < self._cooldown
                ):
                    cached = last.result
                    results.append(PortResult(port, "done", cached.chip, cached.mac, cached.error))
                else:
                    result = PortResult(port)
                    self._active[port] = result
                    results.append(result)
                    fresh.append(result)
            state = None if fresh or not results else self._joinable(results)
            if state is None:
                reset = any(r.state != "done" for r in results)  # cached results are done
                state = ProbeState(f"p-{secrets.token_hex(4)}", results, reset)
                self._probes[state.id] = state
                while len(self._probes) > PROBE_KEEP:
                    self._probes.popitem(last=False)
        for result in fresh:
            submit(self._task(result))
        return state

    def _joinable(self, results: list[PortResult]) -> ProbeState | None:
        for state in self._probes.values():
            if len(state.results) == len(results) and all(
                a is b for a, b in zip(state.results, results, strict=True)
            ):
                return state
        return None

    def get_probe(self, probe_id: str) -> dict[str, Any] | None:
        """The `GET /boards/probe/{id}` reply, or None for an unknown or malformed id."""
        if not PROBE_ID.fullmatch(probe_id):
            return None
        with self._lock:
            state = self._probes.get(probe_id)
            if state is None:
                return None
            return {
                "id": state.id,
                "state": state.state,
                "reset": state.reset,
                "results": [r.as_dict() for r in state.results],
            }

    def fail_pending(self, reason: str) -> None:
        """End every probe that has not finished, as at server shutdown."""
        with self._lock:
            for result in self._active.values():
                result.state, result.error = "done", reason
            self._active.clear()

    def _task(self, result: PortResult) -> Callable[[OnProc], None]:
        def call(on_proc: OnProc) -> None:
            try:
                self._run(result, on_proc)
            except Exception as e:
                self._finish(result, ProbeOutcome(error=f"{type(e).__name__}"), None)

        return call

    def _run(self, result: PortResult, on_proc: OnProc) -> None:
        with self._lock:
            if result.state == "done":  # failed at shutdown while queued
                return
            result.state = "running"
        port = result.port
        before = self._fingerprint_of(port)
        if before is None:
            self._finish(result, ProbeOutcome(error="port is gone"), None)
            return
        outcome = run_probe_child(port, self._probe_timeout, on_proc, self._probe_argv)
        after = self._fingerprint_of(port)
        if outcome.error is None and (after is None or after != before):
            self._finish(result, ProbeOutcome(error="port changed during the probe"), None)
            return
        self._finish(result, outcome, after, before)

    def _fingerprint_of(self, port: str) -> Fingerprint | None:
        info = next((i for i in self._lister() if i.port == port), None)
        return None if info is None else fingerprint(info, self._stat)

    def _finish(
        self,
        result: PortResult,
        outcome: ProbeOutcome,
        fp: Fingerprint | None,
        before: Fingerprint | None = None,
    ) -> None:
        with self._lock:
            if result.state == "done":  # fail_pending got there first
                return
            result.chip, result.mac, result.error = outcome.chip, outcome.mac, outcome.error
            result.state = "done"
            if self._active.get(result.port) is result:
                del self._active[result.port]
            # Only a success is cached: a failed probe (busy port, timeout) is retried at once.
            if fp is not None and fp == before and outcome.mac is not None:
                self._last[result.port] = _Last(self._monotonic(), fp, result)
                if outcome.chip is not None:
                    self._records[result.port] = _Record(
                        outcome.mac, outcome.chip, self._clock(), fp
                    )


__all__ = [
    "COOLDOWN",
    "NOTE",
    "BoardRegistry",
    "Part",
    "PortInfo",
    "ProbeOutcome",
    "Target",
    "UnknownPort",
    "fingerprint",
    "list_ports",
    "load_parts",
    "native_mac",
    "run_probe_child",
]
