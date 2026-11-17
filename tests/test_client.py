"""`submit`: scenario selection, the build/image order, refusals, exit codes and the wire.

The network is faked two ways: a recorder standing in for `client._request`, and (for the
end-to-end tests) an adapter that sends urllib's requests to an in-process app, no socket.
"""

from __future__ import annotations

import base64
import http.client
import http.server
import io
import json
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, NoReturn
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from rustyfarian_bench import cli, client, device, remote, runner, server

from .test_remote import IMAGE, SCENARIO, TOKEN, child


@pytest.fixture(autouse=True)
def no_bench_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own shell must not leak a bench URL, token or CA into the tests."""
    for name in (client.URL_ENV, client.TOKEN_ENV, client.CA_ENV):
        monkeypatch.delenv(name, raising=False)


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


def repo(tmp_path: Path, bench: str = BENCH, local: str | None = None, **checks: str) -> Path:
    """A consumer repo with board c6 and one scenario `connect` unless `checks` say otherwise.

    `local` is the text of an optional `bench/bench.local.toml`.
    """
    root = tmp_path / "consumer"
    (root / "bench/c6").mkdir(parents=True)
    (root / "bench/bench.toml").write_text(bench)
    if local is not None:
        (root / "bench/bench.local.toml").write_text(local)
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


def test_the_committed_mac_is_uploaded_not_the_local_one(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    bench = BENCH.replace('name = "C6 bench"', 'name = "C6 bench"\nmac = "11:22:33:44:55:66"')
    local = '[boards.c6]\nmac = "AA:BB:CC:DD:EE:FF"\n'

    assert submit(repo(tmp_path, bench, local)) == 0

    assert wire.posted["board"]["mac"] == "11:22:33:44:55:66"


def test_a_local_only_mac_is_not_uploaded(tmp_path: Path, wire: Wire, commands: Commands) -> None:
    local = '[boards.c6]\nmac = "aa:bb:cc:dd:ee:ff"\n'

    assert submit(repo(tmp_path, local=local)) == 0

    assert "mac" not in wire.posted["board"]


def test_the_committed_baud_is_uploaded_not_the_local_one(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    bench = BENCH.replace('name = "C6 bench"', 'name = "C6 bench"\nbaud = 115200')
    local = "[boards.c6]\nbaud = 9600\n"

    assert submit(repo(tmp_path, bench, local)) == 0

    assert wire.posted["board"]["baud"] == 115_200


def test_a_local_only_baud_uploads_the_default_baud(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    local = "[boards.c6]\nbaud = 9600\n"

    assert submit(repo(tmp_path, local=local)) == 0

    assert wire.posted["board"]["baud"] == 115_200


def test_a_non_default_committed_baud_is_uploaded_without_a_local_file(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    bench = BENCH.replace('name = "C6 bench"', 'name = "C6 bench"\nbaud = 57600')

    assert submit(repo(tmp_path, bench)) == 0

    assert wire.posted["board"]["baud"] == 57_600


def test_a_malformed_mac_in_bench_local_toml_is_exit_2_before_anything_is_built(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    local = '[boards.c6]\nmac = "not-a-mac"\n'

    assert submit(repo(tmp_path, local=local)) == 2

    assert commands.calls == []
    assert wire.calls == []
    assert "mac" in capsys.readouterr().out


def test_a_forbidden_key_in_bench_local_toml_is_exit_2_and_nothing_is_built(
    tmp_path: Path, wire: Wire, commands: Commands, capsys: pytest.CaptureFixture[str]
) -> None:
    local = '[boards.c6]\nchip = "esp32s3"\n'

    assert submit(repo(tmp_path, local=local)) == 2

    assert commands.calls == []
    assert wire.calls == []
    assert "bench.local.toml" in capsys.readouterr().out


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


class FakeOpener:
    """Stands in for an `OpenerDirector`: `open` hands the request to `fake` with the context."""

    def __init__(self, fake: Callable[..., Any], context: ssl.SSLContext):
        self.fake = fake
        self.context = context

    def open(self, req: urllib.request.Request, timeout: float) -> Any:
        return self.fake(req, timeout, self.context)


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, fake: Callable[..., Any]) -> None:
    """Route every `client._request` to `fake(req, timeout, context)`, no socket."""
    monkeypatch.setattr(
        client, "_opener", lambda url, context: FakeOpener(fake, context), raising=True
    )


def test_an_unreachable_host_is_exit_2(
    tmp_path: Path,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(req: object, timeout: float, context: object = None) -> NoReturn:
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    fake_urlopen(monkeypatch, refuse)

    assert submit(repo(tmp_path)) == 2
    assert "refused" in capsys.readouterr().out


def test_a_certificate_the_client_does_not_trust_is_exit_2_naming_the_ca_variable(
    tmp_path: Path,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def distrust(req: object, timeout: float, context: object = None) -> NoReturn:
        reason = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed")
        raise urllib.error.URLError(reason)

    fake_urlopen(monkeypatch, distrust)

    assert submit(repo(tmp_path)) == 2

    out = capsys.readouterr().out
    assert "certificate verify failed" in out
    assert client.CA_ENV in out


def test_a_server_that_goes_away_while_waiting_is_exit_2(
    tmp_path: Path,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [{"id": JOB}]

    def flaky(req: urllib.request.Request, timeout: float, context: object = None) -> Reply:
        if not replies:
            raise urllib.error.URLError("connection reset")
        return Reply(json.dumps(replies.pop()).encode())

    fake_urlopen(monkeypatch, flaky)
    monkeypatch.setattr(client, "POLL_INTERVAL", 0.001)

    assert submit(repo(tmp_path), "--wait") == 2


def test_a_reply_cut_short_is_exit_2(
    tmp_path: Path, commands: Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cut(req: object, timeout: float, context: object = None) -> NoReturn:
        raise http.client.IncompleteRead(b"{", 10)

    fake_urlopen(monkeypatch, cut)

    assert submit(repo(tmp_path)) == 2


class Reply:
    """What an opener returns: a context manager with a readable body."""

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
    """A fake opener that hands each request to a TestClient: the whole wire, minus the socket."""

    def __init__(self, app: TestClient):
        self.app = app
        self.requests: list[tuple[str, str]] = []
        self.auth: list[str | None] = []  # the Authorization header of each request
        self.contexts: list[object] = []  # what the opener got as `context`

    def __call__(
        self, req: urllib.request.Request, timeout: float, context: object = None
    ) -> Reply:
        parts = urlsplit(req.full_url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        self.requests.append((req.get_method(), target))
        self.auth.append(req.get_header("Authorization"))
        self.contexts.append(context)
        headers = dict(req.header_items())
        if req.data is None:
            resp = self.app.get(target, headers=headers)
        else:
            assert isinstance(req.data, bytes)
            resp = self.app.post(target, content=req.data, headers=headers)
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
        fake_urlopen(monkeypatch, b)
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


# -- the token, the CA and the transport ------------------------------------------------------


@pytest.fixture
def guarded_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bridge]:
    """A bridge to an app that wants a bearer token."""
    token_file = tmp_path / "tokens.toml"
    token_file.write_text(f'fluffi-mbp = "{TOKEN}"\n')
    token_file.chmod(0o600)
    argv = child("pass", "all good")
    runner_ = partial(remote.run_job, timeout=30, argv=argv)
    app = server.create_app(tmp_path / "outbox", runner=runner_, token_file=token_file)
    with TestClient(app) as c:
        b = Bridge(c)
        fake_urlopen(monkeypatch, b)
        monkeypatch.setattr(client, "POLL_INTERVAL", 0.01)
        yield b


def image_file(tmp_path: Path) -> Path:
    mine = tmp_path / "mine.bin"
    mine.write_bytes(IMAGE)
    return mine


def test_the_token_from_the_environment_opens_a_guarded_bench_end_to_end(
    tmp_path: Path,
    guarded_bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)
    argv = ["--repo", str(repo(tmp_path)), "submit", "c6", "ex1"]
    argv += ["--bench", "http://127.0.0.1:8000", "--image", str(image_file(tmp_path)), "--wait"]

    code = cli.main(argv)  # loopback, as through an ssh tunnel; the bridge ignores the host

    out = capsys.readouterr().out
    assert code == 0, out
    assert "pass: all good" in out
    # the header rides on the upload and on every poll
    assert len(guarded_bridge.auth) > 1
    assert set(guarded_bridge.auth) == {f"Bearer {TOKEN}"}
    (job_dir,) = (tmp_path / "outbox").iterdir()
    assert json.loads((job_dir / "job.json").read_text())["submitter"] == "fluffi-mbp"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_without_a_token_a_guarded_bench_is_exit_2_naming_the_variable(
    tmp_path: Path,
    guarded_bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    if value is not None:
        monkeypatch.setenv(client.TOKEN_ENV, value)

    assert submit(repo(tmp_path), "--image", str(image_file(tmp_path))) == 2

    out = capsys.readouterr().out
    assert "401" in out
    assert client.TOKEN_ENV in out
    assert guarded_bridge.auth == [None]
    assert list((tmp_path / "outbox").iterdir()) == []


def test_without_a_token_no_header_is_sent_to_an_open_bench(tmp_path: Path, bridge: Bridge) -> None:
    assert submit(repo(tmp_path), "--image", str(image_file(tmp_path)), "--wait") == 0

    assert set(bridge.auth) == {None}


def test_every_request_verifies_tls_with_a_default_context(tmp_path: Path, bridge: Bridge) -> None:
    assert submit(repo(tmp_path), "--image", str(image_file(tmp_path))) == 0

    [context] = bridge.contexts
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


CA = "/workspace/.devcontainer/bench-ca.pem"


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("", None), (CA, CA)], ids=["unset", "empty", "file"]
)
def test_the_ca_variable_names_the_file_the_context_trusts(
    tmp_path: Path,
    bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    expected: str | None,
) -> None:
    seen: list[str | None] = []
    real = ssl.create_default_context

    def spy(*, cafile: str | None = None) -> ssl.SSLContext:
        seen.append(cafile)
        return real()

    monkeypatch.setattr(ssl, "create_default_context", spy)
    if value is not None:
        monkeypatch.setenv(client.CA_ENV, value)

    assert submit(repo(tmp_path), "--image", str(image_file(tmp_path))) == 0

    assert seen == [expected]


def test_the_token_is_dropped_when_the_bench_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # urllib copies a request's headers onto the redirected one, to whatever host and scheme.
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)
    sent: list[urllib.request.Request] = []

    def capture(req: urllib.request.Request, timeout: float, context: object = None) -> Reply:
        sent.append(req)
        return Reply(json.dumps({"id": JOB}).encode())

    fake_urlopen(monkeypatch, capture)
    argv = ["--repo", str(repo(tmp_path)), "submit", "c6", "ex1", "--bench", "https://bench.test"]

    assert cli.main([*argv, "--image", str(image_file(tmp_path))]) == 0

    [req] = sent
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    moved = urllib.request.HTTPRedirectHandler().redirect_request(
        req, io.BytesIO(), 303, "See Other", http.client.HTTPMessage(), "http://evil.test/jobs"
    )
    assert moved is not None
    assert moved.get_header("Authorization") is None


@pytest.mark.parametrize("content", [None, b"not a certificate"], ids=["missing", "junk"])
def test_a_ca_file_that_cannot_be_loaded_is_exit_2_naming_the_variable(
    tmp_path: Path,
    bridge: Bridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: bytes | None,
) -> None:
    ca = tmp_path / "bench-ca.pem"
    if content is not None:
        ca.write_bytes(content)
    monkeypatch.setenv(client.CA_ENV, str(ca))

    assert submit(repo(tmp_path), "--image", str(image_file(tmp_path))) == 2

    out = capsys.readouterr().out
    assert client.CA_ENV in out
    assert bridge.requests == []


@pytest.mark.parametrize(
    "url", ["http://192.168.1.50:8000", "http://bench.example.net", "http://bench.example.net:80"]
)
def test_a_token_is_never_sent_over_plain_http_off_loopback(
    tmp_path: Path,
    wire: Wire,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)
    root = repo(tmp_path)

    code = cli.main(["--repo", str(root), "submit", "c6", "ex1", "--bench", url])

    assert code == 2
    out = capsys.readouterr().out
    assert "https://" in out
    assert TOKEN not in out
    assert commands.names == []  # refused before anything is built
    assert wire.calls == []


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000", "https://bench.test"],
    ids=["ipv4", "localhost", "ipv6", "https"],
)
def test_a_token_may_travel_to_loopback_or_over_https(
    tmp_path: Path, wire: Wire, commands: Commands, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)
    root = repo(tmp_path)

    assert cli.main(["--repo", str(root), "submit", "c6", "ex1", "--bench", url]) == 0

    assert wire.urls == [f"{url}/jobs"]


@pytest.mark.parametrize(
    "token",
    [f"{TOKEN}\r", f"{TOKEN}\r\n", f"{TOKEN}\nX-Evil: 1", f"{TOKEN}\t", f"{TOKEN}€"],
    ids=["cr", "crlf", "lf-header", "tab", "non-ascii"],
)
def test_a_malformed_token_is_exit_2_without_showing_it(
    tmp_path: Path,
    wire: Wire,
    commands: Commands,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token: str,
) -> None:
    # http.client would reject a CR or LF with an error quoting the whole header
    monkeypatch.setenv(client.TOKEN_ENV, token)

    assert submit(repo(tmp_path)) == 2

    out = capsys.readouterr().out
    assert client.TOKEN_ENV in out
    assert TOKEN not in out
    assert commands.names == []  # refused before anything is built
    assert wire.calls == []


def test_without_a_token_plain_http_off_loopback_still_goes_through(
    tmp_path: Path, wire: Wire, commands: Commands
) -> None:
    assert submit(repo(tmp_path)) == 0  # URL is http://bench.test

    assert wire.urls == [f"{URL}/jobs"]


# -- loopback and proxies ----------------------------------------------------------------------


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET and POST with `{"id": "..."}` and records path and Authorization."""

    def _answer(self) -> None:
        seen: list[tuple[str, str | None]] = self.server.seen  # type: ignore[attr-defined]
        seen.append((self.path, self.headers.get("Authorization")))
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"id": JOB}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, format: str, *args: Any) -> None:
        return None


class Listener:
    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.seen: list[tuple[str, str | None]] = []
        self.server.seen = self.seen  # type: ignore[attr-defined]
        self.port: int = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def listeners() -> Iterator[Callable[[], Listener]]:
    """Start recording servers on 127.0.0.1; all are shut down at teardown."""
    started: list[Listener] = []

    def start() -> Listener:
        started.append(Listener())
        return started[-1]

    yield start
    for listener in started:
        listener.close()


@pytest.fixture
def proxied(monkeypatch: pytest.MonkeyPatch) -> Callable[[Listener], None]:
    """Point the proxy environment at a listener, with NO_PROXY out of the way."""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    def point(proxy: Listener) -> None:
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")

    return point


def test_a_loopback_bench_bypasses_the_proxy_so_the_token_never_reaches_it(
    listeners: Callable[[], Listener],
    proxied: Callable[[Listener], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench, proxy = listeners(), listeners()
    proxied(proxy)
    monkeypatch.setenv(client.TOKEN_ENV, TOKEN)

    reply = client._request(f"http://127.0.0.1:{bench.port}/jobs", None, 5)

    assert reply == {"id": JOB}
    assert bench.seen == [("/jobs", f"Bearer {TOKEN}")]
    assert proxy.seen == []


def test_a_non_loopback_bench_still_goes_through_the_proxy(
    listeners: Callable[[], Listener], proxied: Callable[[Listener], None]
) -> None:
    proxy = listeners()
    proxied(proxy)

    reply = client._request("http://bench.test/jobs", None, 5)

    assert reply == {"id": JOB}
    assert [path for path, _ in proxy.seen] == ["http://bench.test/jobs"]


def proxy_maps(opener: urllib.request.OpenerDirector) -> list[dict[str, str]]:
    handlers: list[object] = getattr(opener, "handlers")  # noqa: B009  (untyped in typeshed)
    return [getattr(h, "proxies") for h in handlers if isinstance(h, urllib.request.ProxyHandler)]  # noqa: B009


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8000", "http://127.0.0.2", "https://[::1]:8443"],
    ids=["localhost", "127.0.0.2", "ipv6"],
)
def test_loopback_benches_skip_proxies(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://proxy.test:3128")

    opener = client._opener(url, ssl.create_default_context())

    # an empty ProxyHandler has no `<scheme>_open`, so the opener drops it: no proxy handler left
    assert proxy_maps(opener) == []


@pytest.mark.parametrize(
    "url",
    ["https://bench.test", "http://127.0.0.1.evil.test", "http://localhost.evil.test"],
    ids=["https", "lookalike-ip", "lookalike-localhost"],
)
def test_other_benches_keep_the_proxy(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://proxy.test:3128")

    opener = client._opener(url, ssl.create_default_context())

    assert any(proxies for proxies in proxy_maps(opener))
