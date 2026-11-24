"""`flash --probe`: which boards are probed before the flash, and what never stops the flash."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rustyfarian_bench import cli, client

from .test_client_boards import VERIFIED, board
from .test_client_flash import IMAGE, JOB, URL, env, flash, image, poll

__all__ = ["env", "image"]  # autouse/fixture re-exports

PART = "bench-one"
UNVERIFIED = board("/dev/ttyUSB0")
OTHER = board("/dev/ttyUSB1")
NAMED_VERIFIED = board(
    "/dev/ttyUSB2", verified=True, name=PART, mac="11:22:33:44:55:66", chip="esp32"
)


class Bench:
    """Stands in for `client._request`: routes on the path, records `(url, body)`."""

    def __init__(
        self,
        boards: list[dict[str, Any]],
        parts_loaded: bool = True,
        errors: dict[str, BaseException] | None = None,
        probe_errors: dict[str, str] | None = None,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.boards = boards
        self.parts_loaded = parts_loaded
        self.errors = errors or {}
        self.probe_errors = probe_errors or {}
        self.on_call = on_call
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.polls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((url, body))
        method = "GET" if body is None else "POST"
        route = url.removeprefix(URL)
        if self.on_call is not None:
            self.on_call(route)
        # errors are keyed "METHOD /path"; a job poll is "GET /jobs"
        key = f"{method} {route.split('?')[0]}"
        if key.startswith("GET /jobs/"):
            key = "GET /jobs"
        if key in self.errors:
            raise self.errors[key]
        if route == "/boards":
            return {"schema": 1, "parts": {"loaded": self.parts_loaded}, "boards": self.boards}
        if route == "/boards/probe":
            return {"id": "p-1", "state": "queued", "ports": []}
        if route == "/boards/probe/p-1":
            assert body is None
            asked = [b for u, b in self.calls if u == f"{URL}/boards/probe" and b is not None]
            ports: list[str] = asked[-1]["ports"] if asked else []
            results = [{"port": p, "error": self.probe_errors.get(p)} for p in ports]
            return {"id": "p-1", "state": "done", "results": results}
        if route == "/flash":
            return {"id": JOB, "status": "queued", "kind": "flash"}
        if key == "GET /jobs":
            return self.polls.pop(0)
        raise AssertionError(f"unrouted {method} {url}")

    @property
    def routes(self) -> list[str]:
        return [f"{'GET' if b is None else 'POST'} {u.removeprefix(URL)}" for u, b in self.calls]

    def body_of(self, route: str) -> dict[str, Any]:
        (body,) = (b for u, b in self.calls if u == f"{URL}{route}" and b is not None)
        return body


def bench(monkeypatch: pytest.MonkeyPatch, *boards: dict[str, Any], **kw: Any) -> Bench:
    b = Bench(list(boards), **kw)
    monkeypatch.setattr(client, "_request", b)
    return b


def test_without_probe_only_the_flash_is_posted(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    assert flash(image, "--part", PART) == 0
    assert b.routes == ["POST /flash"]


def test_without_probe_a_port_flash_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    assert flash(image, "--port", "/dev/ttyUSB0") == 0
    assert b.routes == ["POST /flash"]


def test_without_probe_wait_only_adds_job_polls(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    b.polls = [poll("done", "pass")]
    assert flash(image, "--part", PART, "--wait") == 0
    assert b.routes == ["POST /flash", "GET /jobs/" + JOB + "?offset=0"]


def test_a_verified_part_is_not_probed(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    b = bench(monkeypatch, NAMED_VERIFIED, UNVERIFIED)
    assert flash(image, "--part", PART, "--probe") == 0
    assert b.routes == ["GET /boards", "POST /flash"]
    assert b.body_of("/flash")["part"] == PART


def test_an_unverified_part_probes_only_the_unverified_ports(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED, VERIFIED, OTHER)
    assert flash(image, "--part", PART, "--probe") == 0
    assert b.routes[:3] == ["GET /boards", "POST /boards/probe", "GET /boards/probe/p-1"]
    assert b.routes[-1] == "POST /flash"
    assert b.body_of("/boards/probe") == {"ports": ["/dev/ttyUSB0", "/dev/ttyUSB1"]}


def test_with_no_unverified_ports_nothing_is_probed(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, VERIFIED)
    assert flash(image, "--part", PART, "--probe") == 0
    assert b.routes == ["GET /boards", "POST /flash"]


def test_with_no_listed_ports_nothing_is_probed(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch)
    assert flash(image, "--part", PART, "--probe") == 0
    assert b.routes == ["GET /boards", "POST /flash"]


def test_without_a_parts_file_nothing_is_probed(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED, parts_loaded=False)
    assert flash(image, "--part", PART, "--probe") == 0
    assert b.routes == ["GET /boards", "POST /flash"]


def test_a_port_probes_only_that_port(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    b = bench(monkeypatch, UNVERIFIED, OTHER)
    assert flash(image, "--port", "/dev/ttyUSB1", "--probe") == 0
    assert b.body_of("/boards/probe") == {"ports": ["/dev/ttyUSB1"]}
    assert b.body_of("/flash")["port"] == "/dev/ttyUSB1"
    assert b.routes[-1] == "POST /flash"


@pytest.mark.parametrize("target", ["/dev/ttyACM0", "/dev/ttyNOPE"], ids=["verified", "unlisted"])
def test_a_verified_or_unlisted_port_is_not_probed(
    monkeypatch: pytest.MonkeyPatch, image: Path, target: str
) -> None:
    b = bench(monkeypatch, VERIFIED, UNVERIFIED)
    assert flash(image, "--port", target, "--probe") == 0
    assert b.routes == ["GET /boards", "POST /flash"]


def test_the_reset_warning_comes_before_the_probe_request(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def at_probe(route: str) -> None:
        if route == "/boards/probe":
            cap = capsys.readouterr()
            seen.append(cap.out + cap.err)

    bench(monkeypatch, UNVERIFIED, on_call=at_probe)
    assert flash(image, "--part", PART, "--probe") == 0
    assert len(seen) == 1
    assert "resets" in seen[0]


def test_no_warning_when_nothing_is_probed(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench(monkeypatch, VERIFIED)
    assert flash(image, "--part", PART, "--probe") == 0
    cap = capsys.readouterr()
    assert "resets" not in cap.out + cap.err


def test_a_probe_error_on_a_port_is_warned_and_the_flash_goes_out(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(monkeypatch, UNVERIFIED, probe_errors={"/dev/ttyUSB0": "timed out"})
    assert flash(image, "--part", PART, "--probe") == 0
    cap = capsys.readouterr()
    assert "timed out" in cap.out + cap.err
    assert b.routes[-1] == "POST /flash"


def test_a_probe_request_error_is_warned_and_the_flash_goes_out(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = client.RemoteError(f"{URL}/boards/probe: 503 busy")
    b = bench(monkeypatch, UNVERIFIED, errors={"POST /boards/probe": err})
    assert flash(image, "--part", PART, "--probe") == 0
    cap = capsys.readouterr()
    assert "503 busy" in cap.out + cap.err
    assert b.routes == ["GET /boards", "POST /boards/probe", "POST /flash"]


def test_a_failed_listing_is_warned_and_the_flash_goes_out(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = client.RemoteError(f"{URL}/boards: 500 broken")
    b = bench(monkeypatch, UNVERIFIED, errors={"GET /boards": err})
    assert flash(image, "--part", PART, "--probe") == 0
    cap = capsys.readouterr()
    assert "500 broken" in cap.out + cap.err
    assert b.routes == ["GET /boards", "POST /flash"]


def test_a_server_refusal_after_the_probe_is_2(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    err = client.RemoteError(f"{URL}/flash: 422 not attached or not verified")
    b = bench(monkeypatch, UNVERIFIED, errors={"POST /flash": err})
    assert flash(image, "--part", PART, "--probe") == 2
    assert b.routes[-1] == "POST /flash"


def test_ctrl_c_during_the_probe_is_130_and_nothing_is_flashed(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(monkeypatch, UNVERIFIED, errors={"GET /boards/probe/p-1": KeyboardInterrupt()})
    assert flash(image, "--part", PART, "--probe") == 130
    assert "POST /flash" not in b.routes
    cap = capsys.readouterr()
    assert "nothing was flashed" in cap.out + cap.err
    assert "keeps running on the bench host" in cap.out + cap.err


def test_ctrl_c_without_probe_says_only_aborted(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bench(monkeypatch, UNVERIFIED, errors={"POST /flash": KeyboardInterrupt()})
    assert flash(image, "--part", PART) == 130
    cap = capsys.readouterr()
    assert "aborted" in cap.out + cap.err
    assert "probe" not in cap.out + cap.err


def test_probe_with_wait_probes_flashes_then_polls(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    b.polls = [poll("done", "pass")]
    assert flash(image, "--part", PART, "--probe", "--wait") == 0
    assert b.routes == [
        "GET /boards",
        "POST /boards/probe",
        "GET /boards/probe/p-1",
        "POST /flash",
        "GET /jobs/" + JOB + "?offset=0",
    ]


def test_ctrl_c_while_queueing_the_probe_is_130_and_nothing_is_flashed(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED, errors={"POST /boards/probe": KeyboardInterrupt()})
    assert flash(image, "--part", PART, "--probe") == 130
    assert "POST /flash" not in b.routes


@pytest.mark.parametrize("args", [["--port", "p"], ["--part", PART]])
def test_the_parser_takes_probe_with_either_target(
    monkeypatch: pytest.MonkeyPatch, args: list[str], image: Path
) -> None:
    b = bench(monkeypatch, VERIFIED)
    assert flash(image, *args, "--probe") == 0
    assert b.routes[0] == "GET /boards"


def test_the_parser_refuses_probe_alone(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    with pytest.raises(SystemExit) as stop:
        cli.main(["flash", "--bench", URL, "--probe", str(image)])
    assert stop.value.code == 2
    assert b.calls == []


def test_a_bad_image_makes_no_request_even_with_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = bench(monkeypatch, UNVERIFIED)
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff" * 0x9000)
    assert flash(bad, "--part", PART, "--probe") == 2
    assert b.calls == []


def test_the_fixture_image_is_the_shared_one(image: Path) -> None:
    assert image.read_bytes() == IMAGE
