"""`boards`: the table, the probe poll, exit codes and what reaches the terminal."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rustyfarian_bench import cli, client

URL = "http://bench.test"


def board(port: str = "/dev/ttyUSB0", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "port": port,
        "by_id": None,
        "vid_pid": "10c4:ea60",
        "usb_serial": "0001",
        "verified": False,
        "mac": None,
        "chip": None,
        "name": None,
        "mac_source": None,
        "verified_at": None,
    }
    return base | over


VERIFIED = board(
    "/dev/ttyACM0",
    vid_pid="303a:1001",
    verified=True,
    mac="aa:bb:cc:dd:ee:ff",
    chip="esp32s3",
    name="bench-s3",
    mac_source="probe",
    verified_at="2026-11-23T10:00:00Z",
)


class Host:
    """Stands in for `client._request`."""

    def __init__(
        self,
        boards: list[dict[str, Any]],
        states: tuple[str, ...] = ("done",),
        error: str | None = None,
    ) -> None:
        self.boards = boards
        self.states = list(states)
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((url, body))
        path = url.removeprefix(URL)
        if path == "/boards":
            return {"schema": 1, "parts": {"loaded": False}, "boards": self.boards}
        if path == "/boards/probe":
            return {"id": "p-1", "state": "queued", "ports": [], "reset": True, "note": ""}
        assert path == "/boards/probe/p-1"
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        results = [
            {"port": "/dev/ttyUSB0", "state": state, "chip": None, "mac": None, "error": self.error}
        ]
        return {"id": "p-1", "state": state, "reset": True, "results": results}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (client.URL_ENV, client.TOKEN_ENV, client.CA_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client, "POLL_INTERVAL", 0.001)


def host(monkeypatch: pytest.MonkeyPatch, *args: Any, **kw: Any) -> Host:
    h = Host(*args, **kw)
    monkeypatch.setattr(client, "_request", h)
    return h


def boards(*args: str) -> int:
    return cli.main(["boards", "--bench", URL, *args])


def test_list_prints_a_table_and_does_not_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = host(monkeypatch, [VERIFIED, board()])
    assert boards() == 0
    assert h.calls == [(f"{URL}/boards", None)]
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["PORT", "USB", "SERIAL", "CHIP", "MAC", "NAME", "SEEN"]
    assert out[1].split()[:6] == [
        "/dev/ttyACM0", "303a:1001", "0001", "esp32s3", "aa:bb:cc:dd:ee:ff", "bench-s3",
    ]  # fmt: skip
    assert out[2].split() == ["/dev/ttyUSB0", "10c4:ea60", "0001", "-", "-", "-", "unverified"]
    assert "boards --probe" in out[3]


def test_a_stale_mac_on_an_unverified_row_is_not_shown() -> None:
    lines = client._render({"boards": [board(mac="11:22:33:44:55:66", chip="esp32")]})
    assert "11:22" not in lines[1]


def test_empty_listing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    host(monkeypatch, [])
    assert boards() == 0
    assert "no serial ports" in capsys.readouterr().out


def test_control_characters_are_stripped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host(monkeypatch, [board(name="x", usb_serial="a\x1b[2Jb", verified=True, mac="m\x07")])
    boards()
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\x07" not in out


def test_json_prints_the_reply_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host(monkeypatch, [VERIFIED])
    assert boards("--json") == 0
    assert json.loads(capsys.readouterr().out)["boards"] == [VERIFIED]


def test_probe_posts_polls_then_lists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = host(monkeypatch, [VERIFIED], states=("queued", "running", "done"))
    assert boards("--probe") == 0
    assert h.calls[0] == (f"{URL}/boards/probe", {})
    assert [u for u, _ in h.calls].count(f"{URL}/boards/probe/p-1") == 3
    assert h.calls[-1][0] == f"{URL}/boards"
    assert "resets" in capsys.readouterr().out


def test_probe_with_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    h = host(monkeypatch, [])
    boards("--probe", "--port", "/dev/ttyUSB0")
    assert h.calls[0][1] == {"ports": ["/dev/ttyUSB0"]}


def test_a_failed_probe_exits_1_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host(monkeypatch, [board()], error="timed out")
    assert boards("--probe") == 1
    assert "/dev/ttyUSB0: timed out" in capsys.readouterr().out


def test_json_probe_keeps_stdout_pure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host(monkeypatch, [board()], error="timed out")
    assert boards("--probe", "--json") == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out)["probe"]["results"][0]["error"] == "timed out"
    assert "resets" in cap.err


def test_port_needs_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    h = host(monkeypatch, [])
    assert boards("--port", "/dev/ttyUSB0") == 2
    assert h.calls == []


def test_remote_error_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        raise client.RemoteError(f"{url}: 401 nope")

    monkeypatch.setattr(client, "_request", fail)
    assert boards() == 2


def test_unexpected_reply_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_request", lambda *_: {"boards": "x"})
    assert boards() == 2


def test_token_over_plain_http_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    h = host(monkeypatch, [])
    monkeypatch.setenv(client.TOKEN_ENV, "secret")
    assert cli.main(["boards", "--bench", "http://bench.example"]) == 2
    assert h.calls == []


def test_a_non_url_bench_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    host(monkeypatch, [])
    assert cli.main(["boards", "--bench", "bench.example"]) == 2
