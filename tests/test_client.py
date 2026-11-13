"""`submit`: scenario selection, the build/image order, refusals, exit codes and the wire.

The network is faked two ways: a recorder standing in for `client._request`, and (for the
end-to-end tests) an adapter that sends urllib's requests to an in-process app, no socket.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, NoReturn
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from rustyfarian_bench import cli, client, device, remote, runner, server

from .test_remote import IMAGE, SCENARIO, child

BENCH = """\
[commands]
build = ["build-cmd", "{example}"]
flash = ["flash-cmd", "{port}"]
image = ["image-cmd", "{example}", "{image}"]

[boards.c6]
chip = "esp32c6"
name = "C6 bench"
"""
BENCH_NO_IMAGE = BENCH.replace('image = ["image-cmd", "{example}", "{image}"]\n', "")
URL = "http://bench.test"
JOB = "20260101-000000-abcdef"


def repo(tmp_path: Path, bench: str = BENCH, **checks: str) -> Path:
    """A consumer repo with board c6 and one scenario `connect` unless `checks` say otherwise."""
    root = tmp_path / "consumer"
    (root / "bench/c6").mkdir(parents=True)
    (root / "bench/bench.toml").write_text(bench)
    for name, text in (checks or {"connect": SCENARIO}).items():
        (root / f"bench/c6/{name}.toml").write_text(text)
    return root


def submit(root: Path, *args: str) -> int:
    return cli.main(["--repo", str(root), "submit", "c6", "ex1", "--bench", URL, *args])


class Wire:
    """Stands in for `client._request`: records every call, answers POST and GET from scripts."""

    def __init__(self, *polls: dict[str, Any]):
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.polls = list(polls)

    def __call__(self, url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((url, body))
        if body is not None:
            return {"id": JOB, "status": "queued"}
        return self.polls.pop(0)

    @property
    def posted(self) -> dict[str, Any]:
        (body,) = (b for _, b in self.calls if b is not None)
        return body

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def poll(status: str, verdict: str | None = None, log: str = "", offset: int = 0) -> dict[str, Any]:
    job = {"status": status, "verdict": verdict, "detail": ""}
    return {"job": job, "log": log, "offset": offset}


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    w = Wire()
    monkeypatch.setattr(client, "_request", w)
    monkeypatch.setattr(client, "POLL_INTERVAL", 0.001)
    return w


class Commands:
    """Stands in for `runner._run` in the client: records argv and writes the image on request."""

    def __init__(self, image: bytes = IMAGE, *, fail: str | None = None):
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.image = image
        self.fail = fail

    def __call__(self, cmd: tuple[str, ...], where: Path, **subst: str) -> bool:
        self.calls.append((cmd, where, subst))
        if cmd[0] == self.fail:
            return False
        if cmd[0] == "image-cmd":
            Path(subst["image"]).write_bytes(self.image)
        return True

    @property
    def names(self) -> list[str]:
        return [c[0][0] for c in self.calls]


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> Commands:
    c = Commands()
    monkeypatch.setattr(runner, "_run", c)
    return c


# -- what is built and uploaded ----------------------------------------------------------------


def test_it_builds_then_makes_the_image_and_uploads_it(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(tmp_path)

    assert submit(root) == 0

    assert commands.names == ["build-cmd", "image-cmd"]
    assert commands.calls[0][2] == {"example": "ex1"}
    assert commands.calls[1][2]["example"] == "ex1"
    assert commands.calls[1][1] == root
    body = wire.posted
    assert base64.b64decode(body["image"]) == IMAGE
    assert (body["check"], body["example"], body["scenario"]) == ("connect", "ex1", SCENARIO)
    assert body["board"] == {
        "key": "c6",
        "chip": "esp32c6",
        "name": "C6 bench",
        "baud": 115200,
        "reset": "rts",
    }
    assert wire.urls == [f"{URL}/jobs"]
    assert JOB in capsys.readouterr().out


def test_a_board_mac_is_uploaded_when_the_consumer_board_has_one(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    bench = BENCH.replace('name = "C6 bench"', 'name = "C6 bench"\nmac = "AA:BB:CC:DD:EE:FF"')

    assert submit(repo(tmp_path, bench)) == 0

    assert wire.posted["board"]["mac"].lower() == "aa:bb:cc:dd:ee:ff"


def test_no_mac_key_is_uploaded_when_the_board_has_none(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    assert submit(repo(tmp_path)) == 0

    assert "mac" not in wire.posted["board"]


def test_a_malformed_local_mac_is_exit_2_before_anything_is_built(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = BENCH.replace('name = "C6 bench"', 'name = "C6 bench"\nmac = "not-a-mac"')

    assert submit(repo(tmp_path, bench)) == 2

    assert commands.calls == []  # neither build nor image ran
    assert wire.calls == []
    assert "mac" in capsys.readouterr().out


def test_the_image_temp_file_is_gone_afterwards(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    submit(repo(tmp_path))

    assert not Path(commands.calls[1][2]["image"]).exists()


def test_no_build_still_makes_the_image(tmp_path: Path, wire: Wire, commands: Commands) -> None:
    assert submit(repo(tmp_path), "--no-build") == 0

    assert commands.names == ["image-cmd"]


def test_an_image_flag_skips_the_build_and_the_image_command(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    mine = tmp_path / "mine.bin"
    mine.write_bytes(b"my image")

    assert submit(repo(tmp_path), "--image", str(mine)) == 0

    assert commands.calls == []
    assert base64.b64decode(wire.posted["image"]) == b"my image"


def test_an_image_flag_works_without_an_image_command(tmp_path: Path, wire: Wire) -> None:
    mine = tmp_path / "mine.bin"
    mine.write_bytes(b"my image")

    assert submit(repo(tmp_path, BENCH_NO_IMAGE), "--image", str(mine)) == 0


def test_a_missing_image_file_is_exit_2(
    tmp_path: Path, wire: Wire, capsys: pytest.CaptureFixture[str]
) -> None:
    assert submit(repo(tmp_path), "--image", str(tmp_path / "gone.bin")) == 2
    assert "gone.bin" in capsys.readouterr().out
    assert wire.calls == []


def test_no_image_command_and_no_image_flag_names_both_options(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert submit(repo(tmp_path, BENCH_NO_IMAGE)) == 2

    out = capsys.readouterr().out
    assert "--image" in out
    assert "`image` command" in out
    assert commands.calls == []  # nothing is built for an upload that cannot happen
    assert wire.calls == []


@pytest.mark.parametrize("failing", ["build-cmd", "image-cmd"])
def test_a_failing_build_or_image_command_is_exit_1_and_uploads_nothing(
    tmp_path: Path, wire: Wire, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    monkeypatch.setattr(runner, "_run", Commands(fail=failing))

    assert submit(repo(tmp_path)) == 1
    assert wire.calls == []


def test_an_image_command_that_writes_nothing_is_exit_1(
    tmp_path: Path, wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_run", lambda cmd, where, **subst: True)

    assert submit(repo(tmp_path)) == 1
    assert wire.calls == []


def test_a_missing_tool_is_exit_2(
    tmp_path: Path, wire: Wire, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(cmd: tuple[str, ...], where: Path, **subst: str) -> bool:
        raise device.MissingTool("cannot run 'build-cmd'")

    monkeypatch.setattr(runner, "_run", missing)

    assert submit(repo(tmp_path)) == 2
    assert "cannot run 'build-cmd'" in capsys.readouterr().out
    assert wire.calls == []


# -- refusals and selection --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'setup = [["echo", "hi"]]\n' + SCENARIO,
        'examples = ["ex1"]\n[[step]]\nsay = "s"\nexpect = "x"\nrun = ["x"]\n',
        'examples = ["ex1"]\n[[step]]\nsay = "s"\nconfirm = "ok?"\n',
    ],
    ids=["setup", "run-step", "only-attended"],
)
def test_a_scenario_the_server_would_refuse_is_refused_before_anything_is_built(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    assert submit(repo(tmp_path, connect=text)) == 2

    assert "cannot run remotely" in capsys.readouterr().out
    assert commands.calls == []
    assert wire.calls == []


def test_an_unknown_example_or_board_is_exit_2(
    tmp_path: Path, wire: Wire, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(tmp_path)

    assert cli.main(["--repo", str(root), "submit", "c6", "nope", "--bench", URL]) == 2
    assert cli.main(["--repo", str(root), "submit", "s3", "ex1", "--bench", URL]) == 2
    assert capsys.readouterr().out.count("no bench scenario") == 2


def test_several_checks_for_one_example_need_check(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(tmp_path, connect=SCENARIO, other=SCENARIO)

    assert submit(root) == 2
    assert "--check" in capsys.readouterr().out
    assert wire.calls == []

    assert submit(root, "--check", "other") == 0
    assert wire.posted["check"] == "other"


@pytest.mark.parametrize("url", ["bench.test", "ftp://bench.test", "/jobs"])
def test_a_bench_url_must_be_http(
    tmp_path: Path, wire: Wire, url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repo(tmp_path)

    assert cli.main(["--repo", str(root), "submit", "c6", "ex1", "--bench", url]) == 2
    assert "http" in capsys.readouterr().out
    assert wire.calls == []


# -- waiting -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("pass", 0), ("unverified", 3), ("incomplete", 3), ("fail", 1), ("error", 1), ("weird", 1)],
)
def test_wait_exits_with_the_verdict(
    tmp_path: Path, wire: Wire, commands: Commands, verdict: str, code: int
) -> None:
    wire.polls.append(poll("done", verdict))

    assert submit(repo(tmp_path), "--wait") == code


def test_wait_polls_from_the_returned_offset_and_echoes_the_log(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.polls += [
        poll("queued"),
        poll("running", log="boot\n", offset=5),
        poll("done", "pass", log="ready\n", offset=11),
    ]

    assert submit(repo(tmp_path), "--wait") == 0

    assert wire.urls[1:] == [f"{URL}/jobs/{JOB}?offset={o}" for o in (0, 0, 5)]
    out = capsys.readouterr().out
    assert out.index("boot") < out.index("ready")


def test_without_wait_it_does_not_poll(tmp_path: Path, wire: Wire, commands: Commands) -> None:
    assert submit(repo(tmp_path)) == 0
    assert len(wire.calls) == 1


def test_ctrl_c_while_waiting_says_the_job_keeps_running(
    tmp_path: Path,
    wire: Wire,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    wire.polls.append(poll("running"))
    monkeypatch.setattr("rustyfarian_bench.client.time.sleep", interrupted)

    assert submit(repo(tmp_path), "--wait") == 130
    assert f"job {JOB} keeps running" in capsys.readouterr().out


# -- the network -------------------------------------------------------------------------------


def test_an_unreachable_host_is_exit_2(
    tmp_path: Path,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(req: object, timeout: float) -> NoReturn:
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    assert submit(repo(tmp_path)) == 2
    assert "refused" in capsys.readouterr().out


def test_a_server_that_goes_away_while_waiting_is_exit_2(
    tmp_path: Path,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [{"id": JOB}]

    def flaky(req: urllib.request.Request, timeout: float) -> Reply:
        if not replies:
            raise urllib.error.URLError("connection reset")
        return Reply(json.dumps(replies.pop()).encode())

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(client, "POLL_INTERVAL", 0.001)

    assert submit(repo(tmp_path), "--wait") == 2


def test_a_reply_cut_short_is_exit_2(
    tmp_path: Path, commands: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cut(req: object, timeout: float) -> NoReturn:
        raise http.client.IncompleteRead(b"{", 10)

    monkeypatch.setattr(urllib.request, "urlopen", cut)

    assert submit(repo(tmp_path)) == 2


class Reply:
    """What `urlopen` returns: a context manager with a readable body."""

    def __init__(self, data: bytes):
        self.data = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self.data.read(n)

    def __enter__(self) -> Reply:
        return self

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


# -- end to end against an in-process app ------------------------------------------------------


class Bridge:
    """A `urlopen` that hands each request to a TestClient: the whole wire, minus the socket."""

    def __init__(self, app: TestClient):
        self.app = app
        self.requests: list[tuple[str, str]] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> Reply:
        parts = urlsplit(req.full_url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        self.requests.append((req.get_method(), target))
        if req.data is None:
            resp = self.app.get(target)
        else:
            assert isinstance(req.data, bytes)
            resp = self.app.post(target, content=req.data, headers=dict(req.header_items()))
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                resp.status_code,
                "error",
                resp.headers,
                io.BytesIO(resp.content),
            )
        return Reply(resp.content)


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bridge]:
    outbox = tmp_path / "outbox"
    argv = child("pass", "all good")
    app = server.create_app(outbox, runner=partial(remote.run_job, timeout=30, argv=argv))
    with TestClient(app) as c:
        b = Bridge(c)
        monkeypatch.setattr(urllib.request, "urlopen", b)
        monkeypatch.setattr(client, "POLL_INTERVAL", 0.01)
        yield b


def test_submit_wait_end_to_end(
    tmp_path: Path, bridge: Bridge, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = tmp_path / "mine.bin"
    mine.write_bytes(IMAGE)

    code = submit(repo(tmp_path), "--image", str(mine), "--wait")

    out = capsys.readouterr().out
    assert code == 0, out
    assert "child ran" in out
    assert "pass: all good" in out
    (job_dir,) = (tmp_path / "outbox").iterdir()
    assert (job_dir / "image.bin").read_bytes() == IMAGE
    assert bridge.requests[0] == ("POST", "/jobs")
    assert all(m == "GET" for m, _ in bridge.requests[1:])


def test_the_server_refusing_an_upload_is_exit_2_with_its_reason(
    tmp_path: Path,
    bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The client check is a convenience: with it out of the way the server still refuses.
    # (patching `remote` itself would also lift the server's check, which is the point of it)
    lenient = SimpleNamespace(remote_violations=lambda s: [], EXIT=remote.EXIT, DONE=remote.DONE)
    monkeypatch.setattr(client, "remote", lenient)
    mine = tmp_path / "mine.bin"
    mine.write_bytes(IMAGE)
    root = repo(tmp_path, connect='setup = [["echo", "hi"]]\n' + SCENARIO)

    assert submit(root, "--image", str(mine)) == 2

    assert "422" in capsys.readouterr().out
    assert list((tmp_path / "outbox").iterdir()) == []
