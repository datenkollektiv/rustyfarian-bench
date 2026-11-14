"""The HTTP face of the remote bench, through FastAPI's in-process TestClient (no socket)."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from rustyfarian_bench import remote, server
from rustyfarian_bench.config import ConfigError

from .fakes import wait_until
from .test_remote import (
    IMAGE,
    JOB_ID,
    MARKER,
    OTHER,
    SCENARIO,
    TOKEN,
    child,
    meta,
    plant_outside_job,
)


def body(**over: Any) -> dict[str, Any]:
    """A valid `POST /jobs` body; keyword arguments replace top-level fields."""
    m = meta()
    fields = {
        "board": m["board"],
        "check": m["check"],
        "example": m["example"],
        "scenario": SCENARIO,
        "image": base64.b64encode(IMAGE).decode("ascii"),
    }
    return fields | over


def run_child(argv: list[str]) -> Callable[[Path], None]:
    return partial(remote.run_job, timeout=30, argv=argv)


def idle(job_dir: Path) -> None:
    """A runner that never runs: the job stays queued."""


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    return tmp_path / "outbox"


def client_for(outbox: Path, runner: Callable[[Path], None]) -> Iterator[TestClient]:
    with TestClient(server.create_app(outbox, runner=runner)) as c:
        yield c


@pytest.fixture
def api(outbox: Path) -> Iterator[TestClient]:
    yield from client_for(outbox, run_child(child("pass", "all good")))


@pytest.fixture
def queued_api(outbox: Path) -> Iterator[TestClient]:
    yield from client_for(outbox, idle)


def finished(c: TestClient, job_id: str) -> bool:
    return bool(c.get(f"/jobs/{job_id}").json()["job"]["status"] == "done")


def test_a_posted_job_runs_to_a_verdict(api: TestClient, outbox: Path) -> None:
    posted = api.post("/jobs", json=body())

    assert posted.status_code == 201
    job_id = posted.json()["id"]
    assert posted.json()["status"] == "queued"
    assert wait_until(lambda: finished(api, job_id), 10.0)
    got = api.get(f"/jobs/{job_id}").json()
    assert (got["job"]["verdict"], got["job"]["detail"], got["job"]["exit_code"]) == (
        "pass",
        "all good",
        0,
    )
    assert "child ran" in got["log"]
    assert (outbox / job_id / "image.bin").read_bytes() == IMAGE


@pytest.mark.parametrize(
    "bad",
    [
        {"check": "../evil"},
        {"example": "-x"},
        {"board": {**meta()["board"], "key": "a/b"}},
        {"board": {**meta()["board"], "chip": "esp32;rm"}},
        {"board": {**meta()["board"], "mac": 'aa:bb"\n[commands]'}},
        {"board": {**meta()["board"], "mac": "aa:bb:cc"}},
        {"scenario": 'setup = [["echo", "hi"]]\n' + SCENARIO},
        {"scenario": 'examples = ["ex1"]\n[[step]]\nsay = "s"\nrun = ["x"]\nexpect = "y"\n'},
    ],
    ids=[
        "check",
        "example",
        "board-key",
        "chip",
        "mac-injection",
        "mac-short",
        "setup",
        "run-step",
    ],
)
def test_an_unsafe_upload_is_422_and_leaves_nothing(
    queued_api: TestClient, outbox: Path, bad: dict[str, Any]
) -> None:
    reply = queued_api.post("/jobs", json=body(**bad))

    assert reply.status_code == 422
    assert list(outbox.iterdir()) == []


def test_a_job_with_a_mac_is_accepted_and_the_mac_is_lowercased(
    queued_api: TestClient, outbox: Path
) -> None:
    board = {**meta()["board"], "mac": "AA:BB:CC:DD:EE:FF"}

    reply = queued_api.post("/jobs", json=body(board=board))

    assert reply.status_code == 201
    toml = (outbox / reply.json()["id"] / "bench/bench.toml").read_text()
    assert 'mac = "aa:bb:cc:dd:ee:ff"' in toml.splitlines()


def test_a_job_without_a_mac_is_still_accepted(queued_api: TestClient, outbox: Path) -> None:
    reply = queued_api.post("/jobs", json=body())

    assert reply.status_code == 201
    toml = (outbox / reply.json()["id"] / "bench/bench.toml").read_text()
    assert "mac" not in toml


@pytest.mark.parametrize(
    "bad",
    [
        {"board": {**meta()["board"], "baud": 0}},
        {"board": {**meta()["board"], "reset": "usb"}},
        {"image": 5},
        {"scenario": None},
    ],
    ids=["baud", "reset", "image-type", "scenario-type"],
)
def test_a_malformed_body_is_422(queued_api: TestClient, outbox: Path, bad: dict[str, Any]) -> None:
    assert queued_api.post("/jobs", json=body(**bad)).status_code == 422
    assert list(outbox.iterdir()) == []


def test_an_image_that_is_not_base64_is_422(queued_api: TestClient, outbox: Path) -> None:
    reply = queued_api.post("/jobs", json=body(image="not base64!!"))

    assert reply.status_code == 422
    assert "base64" in reply.json()["detail"]
    assert list(outbox.iterdir()) == []


def test_an_image_over_the_cap_is_413(
    queued_api: TestClient, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", len(IMAGE) - 1)

    assert queued_api.post("/jobs", json=body()).status_code == 413
    assert list(outbox.iterdir()) == []


def test_a_huge_base64_string_is_refused_before_decoding(
    queued_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", 3)

    # Invalid base64, but too long to be looked at: the size check comes first.
    assert queued_api.post("/jobs", json=body(image="!" * 100)).status_code == 413


def test_a_body_over_the_budget_is_413_before_it_is_parsed(
    queued_api: TestClient, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", 3)
    monkeypatch.setattr(server, "MAX_SCENARIO", 10)
    big = b"x" * (server._body_budget() + 1)

    assert queued_api.post("/jobs", content=big).status_code == 413
    assert list(outbox.iterdir()) == []


def test_a_chunked_body_over_the_budget_is_413(
    queued_api: TestClient, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", 3)
    monkeypatch.setattr(server, "MAX_SCENARIO", 10)
    chunk = b"x" * 4096
    chunks = (chunk for _ in range(server._body_budget() // len(chunk) + 2))

    assert queued_api.post("/jobs", content=chunks).status_code == 413
    assert list(outbox.iterdir()) == []


def test_a_scenario_over_the_cap_is_413(
    queued_api: TestClient, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_SCENARIO", 10)

    assert queued_api.post("/jobs", json=body()).status_code == 413
    assert list(outbox.iterdir()) == []


@pytest.mark.parametrize(
    "job_id",
    [JOB_ID, "nope", "..%2F..%2Fetc", "..%2F", "20260101-000000-ABCDEF", "%2e%2e"],
)
def test_an_unknown_or_malformed_job_id_is_404(queued_api: TestClient, job_id: str) -> None:
    assert queued_api.get(f"/jobs/{job_id}").status_code == 404


def test_a_negative_offset_is_422(queued_api: TestClient) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]

    assert queued_api.get(f"/jobs/{job_id}?offset=-1").status_code == 422


def test_an_entry_linking_outside_the_outbox_is_404_and_leaks_nothing(
    queued_api: TestClient, outbox: Path
) -> None:
    plant_outside_job(outbox)

    got = queued_api.get(f"/jobs/{JOB_ID}")

    assert got.status_code == 404
    assert MARKER not in got.text


def test_a_run_log_linking_outside_the_job_sends_no_log(
    queued_api: TestClient, outbox: Path
) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]
    outside = outbox.parent / "elsewhere.log"
    outside.write_text(f"{MARKER}\n")
    (outbox / job_id / "run.log").symlink_to(outside)

    got = queued_api.get(f"/jobs/{job_id}")

    assert got.status_code == 200
    assert (got.json()["log"], got.json()["offset"]) == ("", 0)
    assert MARKER not in got.text


def _log(outbox: Path, job_id: str) -> Path:
    return outbox / job_id / "run.log"


def test_a_running_job_sends_only_whole_lines(queued_api: TestClient, outbox: Path) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]
    _log(outbox, job_id).write_bytes(b"one\ntwo\npar")

    first = queued_api.get(f"/jobs/{job_id}").json()

    assert (first["log"], first["offset"]) == ("one\ntwo\n", 8)
    with _log(outbox, job_id).open("ab") as f:
        f.write(b"tial\nlast")
    second = queued_api.get(f"/jobs/{job_id}?offset={first['offset']}").json()
    assert (second["log"], second["offset"]) == ("partial\n", 16)


def test_a_carriage_return_frame_counts_as_a_line_break(
    queued_api: TestClient, outbox: Path
) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]
    _log(outbox, job_id).write_bytes(b"10%\r20%\rrest")

    got = queued_api.get(f"/jobs/{job_id}").json()

    assert (got["log"], got["offset"]) == ("10%\r20%\r", 8)


def test_nothing_is_sent_until_a_line_is_whole(queued_api: TestClient, outbox: Path) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]
    _log(outbox, job_id).write_bytes(b"half a li")

    got = queued_api.get(f"/jobs/{job_id}?offset=0").json()

    assert (got["log"], got["offset"]) == ("", 0)


def test_a_missing_log_is_empty_not_an_error(queued_api: TestClient) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]

    got = queued_api.get(f"/jobs/{job_id}?offset=7").json()

    assert (got["log"], got["offset"]) == ("", 7)


def test_a_done_job_returns_the_whole_remaining_tail(queued_api: TestClient, outbox: Path) -> None:
    posted = queued_api.post("/jobs", json=body()).json()
    job_id = posted["id"]
    _log(outbox, job_id).write_bytes(b"one\nno newline at the end")
    job = remote.read_job(outbox, job_id)
    assert job is not None
    job.status, job.verdict = remote.DONE, "pass"
    remote.write_job(outbox / job_id, job)

    got = queued_api.get(f"/jobs/{job_id}?offset=4").json()

    assert got["log"] == "no newline at the end"
    assert got["offset"] == len(b"one\nno newline at the end")
    assert got["job"]["status"] == "done"


def test_jobs_left_unfinished_by_a_previous_server_are_closed_at_start(outbox: Path) -> None:
    job = remote.create_job(outbox, meta(), SCENARIO, IMAGE)

    with TestClient(server.create_app(outbox, runner=idle)) as c:
        got = c.get(f"/jobs/{job.id}").json()["job"]

    assert (got["status"], got["verdict"], got["detail"]) == ("done", "error", remote.RESTARTED)


def test_a_relative_outbox_is_resolved_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with TestClient(server.create_app(Path("outbox"), runner=idle)) as c:
        monkeypatch.chdir(tmp_path / "outbox")  # a later chdir must not move the outbox
        job_id = c.post("/jobs", json=body()).json()["id"]

        assert c.get(f"/jobs/{job_id}").status_code == 200

    assert (tmp_path / "outbox" / job_id / "job.json").is_file()


# -- bearer tokens -----------------------------------------------------------------------------


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.toml"
    path.write_text(f'fluffi-mbp = "{TOKEN}"\n')
    return path


@pytest.fixture
def guarded(outbox: Path, token_file: Path) -> Iterator[TestClient]:
    with TestClient(server.create_app(outbox, runner=idle, token_file=token_file)) as c:
        yield c


BEARER = {"Authorization": f"Bearer {TOKEN}"}


def test_a_request_without_a_token_is_401_and_leaves_nothing(
    guarded: TestClient, outbox: Path
) -> None:
    reply = guarded.post("/jobs", json=body())

    assert reply.status_code == 401
    assert reply.headers["WWW-Authenticate"] == "Bearer"
    assert list(outbox.iterdir()) == []


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {OTHER}",
        f"Basic {TOKEN}",
        "Bearer",
        "Bearer ",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        TOKEN,
    ],
    ids=["other", "basic", "bare", "empty", "longer", "shorter", "no-scheme"],
)
def test_a_wrong_token_or_scheme_is_401(guarded: TestClient, outbox: Path, header: str) -> None:
    reply = guarded.post("/jobs", json=body(), headers={"Authorization": header})

    assert reply.status_code == 401
    assert list(outbox.iterdir()) == []


def test_the_right_token_submits_and_the_job_names_its_submitter(
    guarded: TestClient, outbox: Path
) -> None:
    posted = guarded.post("/jobs", json=body(), headers=BEARER)

    assert posted.status_code == 201
    assert posted.json()["submitter"] == "fluffi-mbp"
    job_id = posted.json()["id"]
    assert guarded.get(f"/jobs/{job_id}").status_code == 401
    got = guarded.get(f"/jobs/{job_id}", headers=BEARER)
    assert got.status_code == 200
    assert got.json()["job"]["submitter"] == "fluffi-mbp"
    assert TOKEN not in (outbox / job_id / "job.json").read_text()


def test_a_stranger_with_an_oversized_body_gets_401_not_413(
    guarded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", 3)
    monkeypatch.setattr(server, "MAX_SCENARIO", 10)
    big = b"x" * (server._body_budget() + 1)

    assert guarded.post("/jobs", content=big).status_code == 401


def test_a_token_added_to_the_file_is_accepted_without_a_restart(
    guarded: TestClient, token_file: Path
) -> None:
    other = {"Authorization": f"Bearer {OTHER}"}
    assert guarded.post("/jobs", json=body(), headers=other).status_code == 401

    with token_file.open("a") as f:
        f.write(f'ci = "{OTHER}"\n')

    assert guarded.post("/jobs", json=body(), headers=other).json()["submitter"] == "ci"


def test_a_token_file_that_went_bad_is_503_for_everyone(
    guarded: TestClient, token_file: Path, outbox: Path
) -> None:
    token_file.write_text("n = \n")

    reply = guarded.post("/jobs", json=body(), headers=BEARER)

    assert reply.status_code == 503
    assert str(token_file) not in reply.text
    assert list(outbox.iterdir()) == []


def test_an_app_without_a_token_file_stays_open(queued_api: TestClient) -> None:
    posted = queued_api.post("/jobs", json=body())

    assert posted.status_code == 201
    assert posted.json()["submitter"] == ""


# -- serve: what reaches uvicorn -------------------------------------------------------------


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Each `uvicorn.run` call as its keyword arguments plus the app under `app`."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


@pytest.fixture
def tls(tmp_path: Path) -> tuple[Path, Path]:
    """A certificate and key file; uvicorn is faked, so their content does not matter."""
    cert, key = tmp_path / "bench.pem", tmp_path / "bench-key.pem"
    cert.write_text("cert")
    key.write_text("key")
    return cert, key


def stranger_status(app: Any) -> int:
    """The status a request without a token gets from `app`."""
    return int(TestClient(app).get(f"/jobs/{JOB_ID}").status_code)


def test_serve_on_loopback_needs_neither_tokens_nor_tls(
    outbox: Path, uvicorn_run: list[dict[str, Any]]
) -> None:
    server.serve("127.0.0.1", 8000, outbox, 600.0)

    [kw] = uvicorn_run
    assert (kw["host"], kw["port"]) == ("127.0.0.1", 8000)
    assert (kw["ssl_certfile"], kw["ssl_keyfile"]) == (None, None)
    assert stranger_status(kw["app"]) == 404  # open: the unknown job, not the token, decides


def test_serve_hands_the_certificate_and_key_to_uvicorn_and_guards_the_app(
    outbox: Path,
    token_file: Path,
    tls: tuple[Path, Path],
    uvicorn_run: list[dict[str, Any]],
) -> None:
    cert, key = tls

    server.serve(
        "0.0.0.0", 8443, outbox, 600.0, token_file=token_file, ssl_certfile=cert, ssl_keyfile=key
    )

    [kw] = uvicorn_run
    assert (kw["host"], kw["port"]) == ("0.0.0.0", 8443)
    assert (kw["ssl_certfile"], kw["ssl_keyfile"]) == (cert, key)
    assert stranger_status(kw["app"]) == 401


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({}, "--token-file"),
        ({"tokens": True}, "--ssl-certfile"),
        ({"tokens": True, "cert": True}, "--ssl-keyfile"),
        ({"tokens": True, "key": True}, "--ssl-certfile"),
    ],
    ids=["nothing", "no-tls", "cert-only", "key-only"],
)
def test_serve_off_loopback_refuses_to_start_without_tokens_over_tls(
    outbox: Path,
    token_file: Path,
    tls: tuple[Path, Path],
    uvicorn_run: list[dict[str, Any]],
    kw: dict[str, bool],
    match: str,
) -> None:
    given: dict[str, Any] = {}
    if kw.get("tokens"):
        given["token_file"] = token_file
    if kw.get("cert"):
        given["ssl_certfile"] = tls[0]
    if kw.get("key"):
        given["ssl_keyfile"] = tls[1]

    with pytest.raises(ConfigError, match=match):
        server.serve("0.0.0.0", 8443, outbox, 600.0, **given)

    assert uvicorn_run == []


@pytest.mark.parametrize("missing", ["cert", "key"])
def test_serve_refuses_a_tls_file_that_is_not_there(
    outbox: Path,
    token_file: Path,
    tls: tuple[Path, Path],
    uvicorn_run: list[dict[str, Any]],
    missing: str,
) -> None:
    cert, key = tls
    gone = cert if missing == "cert" else key
    gone.unlink()
    flag = "--ssl-certfile" if missing == "cert" else "--ssl-keyfile"

    with pytest.raises(ConfigError, match=f"{flag} .*{gone.name}"):
        server.serve(
            "0.0.0.0",
            8443,
            outbox,
            600.0,
            token_file=token_file,
            ssl_certfile=cert,
            ssl_keyfile=key,
        )

    assert uvicorn_run == []


@pytest.mark.parametrize("host", ["localhost", "::1", "127.0.0.2"])
def test_serve_counts_every_loopback_name_as_loopback(
    outbox: Path, uvicorn_run: list[dict[str, Any]], host: str
) -> None:
    server.serve(host, 8000, outbox, 600.0)

    assert len(uvicorn_run) == 1


def test_serve_with_a_broken_token_file_does_not_start(
    outbox: Path, tmp_path: Path, uvicorn_run: list[dict[str, Any]]
) -> None:
    with pytest.raises(ConfigError, match=r"tokens\.toml"):
        server.serve("127.0.0.1", 8000, outbox, 600.0, token_file=tmp_path / "tokens.toml")

    assert uvicorn_run == []


def test_serve_on_loopback_still_takes_a_token_file(
    outbox: Path, token_file: Path, uvicorn_run: list[dict[str, Any]]
) -> None:
    # A reverse proxy, `tailscale serve` or an ssh tunnel in front: tokens, no TLS of its own.
    server.serve("127.0.0.1", 8000, outbox, 600.0, token_file=token_file)

    [kw] = uvicorn_run
    assert stranger_status(kw["app"]) == 401


@pytest.mark.parametrize(("kw", "expected"), [({}, 30.0), ({"detect_timeout": 5.0}, 5.0)])
def test_create_app_hands_the_detect_timeout_to_the_worker(
    outbox: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], expected: float
) -> None:
    seen: list[dict[str, Any]] = []
    real_init = remote.Worker.__init__

    def spy(self: remote.Worker, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(remote.Worker, "__init__", spy)

    server.create_app(outbox, job_timeout=12.0, **kw)

    [kwargs] = seen
    assert kwargs["detect_timeout"] == expected
    assert kwargs["timeout"] == 12.0
