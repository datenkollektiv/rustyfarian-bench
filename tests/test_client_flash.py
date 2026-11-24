"""`flash`: the request body, `--wait` exit codes and what is refused before any upload."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from rustyfarian_bench import cli, client

from .fakes import FACTORY, NVS, PHY_INIT, make_image, table_bytes, with_header

URL = "http://bench.test"
JOB = "20260101-000000-abcdef"


def corrupt_table() -> bytes:
    """A partition table whose MD5 entry does not match its entries."""
    raw = bytearray(table_bytes((NVS, PHY_INIT, FACTORY)))
    raw[3 * 32 + 16] ^= 0xFF
    return bytes(raw)


IMAGE = with_header(5, 0, base=make_image((NVS, PHY_INIT, FACTORY)))


class Wire:
    """Stands in for `client._request`: records every call, answers POST and GET from scripts."""

    def __init__(
        self, *polls: dict[str, Any] | BaseException, post_error: BaseException | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.polls = list(polls)
        self.post_error = post_error

    def __call__(self, url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((url, body))
        if body is not None:
            if self.post_error is not None:
                raise self.post_error
            return {"id": JOB, "status": "queued", "kind": "flash"}
        reply = self.polls.pop(0)
        if isinstance(reply, BaseException):  # a scripted failure of the poll
            raise reply
        return reply

    @property
    def posted(self) -> dict[str, Any]:
        (body,) = (b for _, b in self.calls if b is not None)
        return body


def poll(status: str, verdict: str | None = None, log: str = "", offset: int = 0) -> dict[str, Any]:
    return {
        "job": {"status": status, "verdict": verdict, "detail": ""},
        "log": log,
        "offset": offset,
    }


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (client.URL_ENV, client.TOKEN_ENV, client.CA_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client, "POLL_INTERVAL", 0.001)


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "app.bin"
    path.write_bytes(IMAGE)
    return path


def wire(
    monkeypatch: pytest.MonkeyPatch, *polls: dict[str, Any] | BaseException, **kw: Any
) -> Wire:
    w = Wire(*polls, **kw)
    monkeypatch.setattr(client, "_request", w)
    return w


def flash(image: Path, *args: str) -> int:
    return cli.main(["flash", "--bench", URL, *args, str(image)])


def test_a_port_flash_posts_the_image_and_nothing_unset(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    w = wire(monkeypatch)

    assert flash(image, "--port", "/dev/ttyUSB0") == 0

    assert w.calls == [
        (
            f"{URL}/flash",
            {"port": "/dev/ttyUSB0", "image": base64.b64encode(IMAGE).decode("ascii")},
        )
    ]


def test_a_part_flash_carries_every_option_that_was_given(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    w = wire(monkeypatch)

    assert flash(image, "--part", "bench-one", "--baud", "921600", "--reset", "dtr",
                 "--expect", "up \\d+") == 0  # fmt: skip

    body = w.posted
    assert body["part"] == "bench-one"
    assert "port" not in body
    assert (body["baud"], body["reset"], body["expect"]) == (921600, "dtr", "up \\d+")
    assert base64.b64decode(body["image"]) == IMAGE


def test_the_trailing_slash_of_the_bench_is_dropped(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    w = wire(monkeypatch)

    assert cli.main(["flash", "--bench", f"{URL}/", "--port", "p", str(image)]) == 0
    assert w.calls[0][0] == f"{URL}/flash"


def test_without_wait_the_job_is_not_polled(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = wire(monkeypatch)

    assert flash(image, "--port", "p") == 0
    assert len(w.calls) == 1
    assert JOB in capsys.readouterr().out


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("pass", 0), ("unverified", 3), ("incomplete", 3), ("fail", 1), ("error", 1)],
)
def test_wait_exits_with_the_verdict(
    monkeypatch: pytest.MonkeyPatch, image: Path, verdict: str, code: int
) -> None:
    w = wire(monkeypatch, poll("running"), poll("done", verdict))

    assert flash(image, "--port", "p", "--wait") == code
    assert [u for u, b in w.calls if b is None] == [f"{URL}/jobs/{JOB}?offset=0"] * 2


def test_wait_streams_the_log_from_the_last_offset(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = wire(monkeypatch, poll("running", log="flashing\n", offset=9), poll("done", "pass"))

    assert flash(image, "--part", "x", "--wait") == 0
    assert w.calls[-1][0].endswith("?offset=9")
    assert "flashing" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    [
        with_header(5, 0),
        b"",
        b"\xff" * 0x9000,
        b"\xe9merged-image\x00\xff",
        with_header(5, 0, base=make_image((NVS,), table=corrupt_table())),
    ],
    ids=["no-table", "empty", "erased", "no-chip-id", "bad-table"],
)
def test_a_garbage_image_is_2_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: bytes
) -> None:
    w = wire(monkeypatch)
    path = tmp_path / "bad.bin"
    path.write_bytes(bad)

    assert flash(path, "--port", "p") == 2
    assert w.calls == []


def test_an_unreadable_image_is_2_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    w = wire(monkeypatch)

    assert flash(tmp_path / "missing.bin", "--port", "p") == 2
    assert flash(tmp_path, "--port", "p") == 2  # a directory
    assert w.calls == []


def test_a_remote_error_is_2(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    wire(monkeypatch, post_error=client.RemoteError(f"{URL}/flash: 422 no such port"))

    assert flash(image, "--port", "p") == 2


def test_a_401_from_the_host_is_2_and_names_the_token(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        raise client.RemoteError(f"{url}: 401 nope; set ${client.TOKEN_ENV}")

    monkeypatch.setattr(client, "_request", refuse)

    assert flash(image, "--port", "p") == 2
    assert client.TOKEN_ENV in capsys.readouterr().out


def test_ctrl_c_while_uploading_is_130(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    wire(monkeypatch, post_error=KeyboardInterrupt())

    assert flash(image, "--port", "p") == 130


def test_ctrl_c_while_waiting_is_130_and_the_job_keeps_running(
    monkeypatch: pytest.MonkeyPatch, image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wire(monkeypatch, KeyboardInterrupt())

    assert flash(image, "--port", "p", "--wait") == 130
    assert "keeps running" in capsys.readouterr().out


def test_a_token_over_plain_http_is_refused_before_anything_happens(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    w = wire(monkeypatch)
    monkeypatch.setenv(client.TOKEN_ENV, "secret")

    assert cli.main(["flash", "--bench", "http://bench.example", "--port", "p", str(image)]) == 2
    assert w.calls == []


def test_a_non_url_bench_is_refused(monkeypatch: pytest.MonkeyPatch, image: Path) -> None:
    w = wire(monkeypatch)

    assert cli.main(["flash", "--bench", "bench.example", "--port", "p", str(image)]) == 2
    assert w.calls == []


def test_the_bench_may_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, image: Path
) -> None:
    w = wire(monkeypatch)
    monkeypatch.setenv(client.URL_ENV, URL)

    assert cli.main(["flash", "--port", "p", str(image)]) == 0
    assert w.calls[0][0] == f"{URL}/flash"


def test_without_a_bench_the_parser_refuses(
    image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["flash", "--port", "p", str(image)])

    assert stop.value.code == 2
    assert client.URL_ENV in capsys.readouterr().err


@pytest.mark.parametrize(
    "args", [[], ["--port", "p", "--part", "x"], ["--port", "p", "--reset", "x"]]
)
def test_the_parser_wants_exactly_one_target_and_a_known_reset(
    image: Path, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = wire(monkeypatch)

    with pytest.raises(SystemExit) as stop:
        cli.main(["flash", "--bench", URL, *args, str(image)])

    assert stop.value.code == 2
    assert w.calls == []
