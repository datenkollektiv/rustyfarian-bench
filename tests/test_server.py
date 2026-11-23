"""The HTTP face of the remote bench, through FastAPI's in-process TestClient (no socket)."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from rustyfarian_bench import flash_image, remote, server
from rustyfarian_bench.boards import PortInfo
from rustyfarian_bench.config import ConfigError
from rustyfarian_bench.status import StatusWriter

from .fakes import FACTORY, NVS, make_image, table_bytes, wait_until
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


def test_an_image_with_a_malformed_partition_table_is_422_and_leaves_nothing(
    queued_api: TestClient, outbox: Path
) -> None:
    image = make_image([], table=table_bytes([NVS, (1, 1, 0xA000, 0x1000, "phy_init")]))
    encoded = base64.b64encode(image).decode("ascii")

    reply = queued_api.post("/jobs", json=body(image=encoded))

    assert reply.status_code == 422
    assert list(outbox.iterdir()) == []


def test_a_job_reports_how_its_image_will_be_flashed(queued_api: TestClient) -> None:
    image = make_image((NVS, FACTORY), contents={0x10000: b"\xe9app"})
    encoded = base64.b64encode(image).decode("ascii")

    job_id = queued_api.post("/jobs", json=body(image=encoded)).json()["id"]

    flash = queued_api.get(f"/jobs/{job_id}").json()["job"]["flash"]
    assert flash == flash_image.describe(flash_image.plan(image))
    assert flash["mode"] == "segments"


def test_a_flat_image_is_reported_as_a_flat_flash(queued_api: TestClient) -> None:
    job_id = queued_api.post("/jobs", json=body()).json()["id"]

    assert queued_api.get(f"/jobs/{job_id}").json()["job"]["flash"]["mode"] == "flat"


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
    path.chmod(0o600)
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


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_a_token_file_loosened_after_start_is_503_for_everyone(
    guarded: TestClient, token_file: Path, outbox: Path
) -> None:
    token_file.chmod(0o644)

    reply = guarded.post("/jobs", json=body(), headers=BEARER)

    assert reply.status_code == 503
    assert str(token_file) not in reply.text
    assert list(outbox.iterdir()) == []


def test_a_submitter_is_compared_against_every_token(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = {TOKEN: "first", OTHER: "second", "x" * 32: "third"}
    real = hmac.compare_digest
    calls: list[bytes] = []

    def spy(a: bytes, b: bytes) -> bool:
        calls.append(a)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    scope: Any = {"headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}

    assert server._submitter(scope, tokens) == "first"
    assert len(calls) == 3


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


# -- the kiosk status file -----------------------------------------------------------------------


def status_of(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_the_status_file_follows_a_job_and_goes_with_the_server(
    outbox: Path, tmp_path: Path
) -> None:
    path = tmp_path / "run" / "status.json"
    path.parent.mkdir()
    gate = threading.Event()

    def gated(job_dir: Path) -> None:
        gate.wait(10.0)
        remote.run_job(job_dir, 30, child("pass", "all good"))

    writer = StatusWriter(path, outbox, version="9.9.9")
    try:
        with TestClient(server.create_app(outbox, runner=gated, status=writer)) as c:
            assert status_of(path)["state"] == "idle"

            first = c.post("/jobs", json=body()).json()["id"]
            assert wait_until(lambda: status_of(path)["state"] == "running", 5.0)
            assert status_of(path)["running"]["example"] == "ex1"

            second = c.post("/jobs", json=body()).json()["id"]
            assert status_of(path)["queued"] == 1

            gate.set()
            assert wait_until(lambda: finished(c, first) and finished(c, second), 10.0)
            assert wait_until(lambda: len(status_of(path)["history"]) == 2, 5.0)
            done = status_of(path)
            assert (done["state"], done["queued"]) == ("idle", 0)
            assert [h["verdict"] for h in done["history"]] == ["pass", "pass"]
        assert not path.exists()  # lifespan exit removed it
    finally:
        gate.set()
        writer.stop()


def test_a_status_writer_in_a_missing_directory_still_serves(
    outbox: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "no-such-dir" / "status.json"
    writer = StatusWriter(path, outbox, version="9.9.9")

    with TestClient(
        server.create_app(outbox, runner=run_child(child("pass", "all good")), status=writer)
    ) as c:
        job_id = c.post("/jobs", json=body()).json()["id"]
        assert wait_until(lambda: finished(c, job_id), 10.0)
        assert c.get(f"/jobs/{job_id}").json()["job"]["verdict"] == "pass"

    assert not path.exists()
    assert "status file" in capsys.readouterr().err


def test_serve_builds_a_status_writer_only_with_a_status_file(
    outbox: Path, tmp_path: Path, uvicorn_run: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[StatusWriter | None] = []
    real = server.create_app

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append(kw.get("status"))
        return real(*a, **kw)

    monkeypatch.setattr(server, "create_app", spy)

    server.serve("127.0.0.1", 8000, outbox, 600.0)
    server.serve("127.0.0.1", 8000, outbox, 600.0, status_file=tmp_path / "s.json")

    assert seen[0] is None
    assert isinstance(seen[1], StatusWriter)
    assert len(uvicorn_run) == 2
    assert not (tmp_path / "s.json").exists()  # uvicorn is faked: no lifespan ran


# -- the board list and the probe ------------------------------------------------------------------

BOARD_MAC = "02:00:00:12:00:b8"


def probe_child_argv(chip: str = "esp32", mac: str = BOARD_MAC, delay: float = 0.0) -> list[str]:
    code = (
        "import json, time; "
        f"time.sleep({delay}); "
        f"print(json.dumps({{'chip': {chip!r}, 'mac': {mac!r}, 'error': None}}))"
    )
    return [sys.executable, "-c", code]


@pytest.fixture
def board_port(tmp_path: Path) -> str:
    """A device node that exists, so the registry can stat it; the probe child never opens it."""
    node = tmp_path / "ttyUSB0"
    node.write_text("")
    return str(node)


@pytest.fixture
def boards_app(outbox: Path, token_file: Path, board_port: str) -> Iterator[TestClient]:
    info = PortInfo(board_port, None, 0x10C4, 0xEA60, "0001", "1-1")
    app = server.create_app(
        outbox,
        runner=idle,
        token_file=token_file,
        lister=lambda: [info],
        probe_argv=probe_child_argv(),
    )
    with TestClient(app) as c:
        yield c


def poll_probe(c: TestClient, probe_id: str) -> dict[str, Any]:
    assert wait_until(
        lambda: c.get(f"/boards/probe/{probe_id}", headers=BEARER).json()["state"] == "done", 10.0
    )
    got: dict[str, Any] = c.get(f"/boards/probe/{probe_id}", headers=BEARER).json()
    return got


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/boards"), ("post", "/boards/probe"), ("get", "/boards/probe/p-01234567")],
)
def test_the_board_routes_need_a_token(boards_app: TestClient, method: str, path: str) -> None:
    assert getattr(boards_app, method)(path).status_code == 401
    assert (
        getattr(boards_app, method)(path, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_the_list_shows_an_attached_port_unverified_without_a_name(
    boards_app: TestClient, board_port: str
) -> None:
    got = boards_app.get("/boards", headers=BEARER).json()

    assert got["schema"] == 1
    assert got["parts"] == {"loaded": False}
    [board] = got["boards"]
    assert (board["port"], board["verified"], board["mac"], board["name"]) == (
        board_port,
        False,
        None,
        None,
    )


@pytest.mark.parametrize("bad", ["/dev/ttyUSB9", "--help", "-x"])
def test_a_probe_of_a_port_outside_the_listing_is_422(
    boards_app: TestClient, outbox: Path, bad: str
) -> None:
    r = boards_app.post("/boards/probe", json={"ports": [bad]}, headers=BEARER)

    assert r.status_code == 422
    assert list(outbox.iterdir()) == []


def test_a_probe_is_202_then_polled_to_done_and_the_board_is_verified(
    boards_app: TestClient, board_port: str, outbox: Path
) -> None:
    r = boards_app.post("/boards/probe", json={"ports": [board_port]}, headers=BEARER)

    assert r.status_code == 202
    sent = r.json()
    assert set(sent) == {"id", "state", "ports", "reset", "note"}
    assert (sent["ports"], sent["reset"]) == ([board_port], True)
    done = poll_probe(boards_app, sent["id"])
    assert set(done) == {"id", "state", "reset", "results"}
    assert done["results"] == [
        {"port": board_port, "state": "done", "chip": "esp32", "mac": BOARD_MAC, "error": None}
    ]
    [board] = boards_app.get("/boards", headers=BEARER).json()["boards"]
    assert (board["verified"], board["mac"], board["chip"], board["mac_source"]) == (
        True,
        BOARD_MAC,
        "esp32",
        "probe",
    )
    assert list(outbox.iterdir()) == []  # a probe is not a job


def test_a_probe_without_a_body_probes_every_listed_port(
    boards_app: TestClient, board_port: str
) -> None:
    r = boards_app.post("/boards/probe", headers=BEARER)

    assert r.status_code == 202
    assert r.json()["ports"] == [board_port]
    poll_probe(boards_app, r.json()["id"])


def test_a_second_probe_request_while_one_runs_reuses_its_id(
    outbox: Path, token_file: Path, board_port: str
) -> None:
    info = PortInfo(board_port, None, 0x10C4, 0xEA60, "0001", "1-1")
    app = server.create_app(
        outbox,
        runner=idle,
        token_file=token_file,
        lister=lambda: [info],
        probe_argv=probe_child_argv(delay=0.5),
    )
    with TestClient(app) as c:
        first = c.post("/boards/probe", headers=BEARER).json()
        second = c.post("/boards/probe", headers=BEARER).json()

        assert second["id"] == first["id"]
        poll_probe(c, first["id"])


def test_an_unknown_probe_id_is_404(boards_app: TestClient) -> None:
    for probe_id in ("p-01234567", "nonsense"):
        assert boards_app.get(f"/boards/probe/{probe_id}", headers=BEARER).status_code == 404


def test_the_list_names_a_board_from_the_parts_file(
    outbox: Path, token_file: Path, tmp_path: Path
) -> None:
    parts = tmp_path / "parts.toml"
    parts.write_text(f'[[parts]]\nname = "bench-one"\nmac = "{BOARD_MAC.upper()}"\n')
    info = PortInfo("/dev/ttyACM0", None, 0x303A, 0x1001, BOARD_MAC.upper(), "1-2")
    app = server.create_app(
        outbox, runner=idle, token_file=token_file, parts_file=parts, lister=lambda: [info]
    )
    with TestClient(app) as c:
        got = c.get("/boards", headers=BEARER).json()

    assert got["parts"] == {"loaded": True}
    assert got["boards"][0]["name"] == "bench-one"
    assert got["boards"][0]["mac_source"] == "usb-serial"


def test_a_parts_file_that_broke_is_503_on_the_board_routes_only(
    outbox: Path, token_file: Path, tmp_path: Path
) -> None:
    parts = tmp_path / "parts.toml"
    parts.write_text('[[parts]]\nname = "bench-one"\nmac = "02:00:00:12:00:b8"\n')
    app = server.create_app(
        outbox, runner=idle, token_file=token_file, parts_file=parts, lister=lambda: []
    )
    with TestClient(app) as c:
        parts.write_text("[[parts")

        listed = c.get("/boards", headers=BEARER)
        probed = c.post("/boards/probe", headers=BEARER)
        job = c.get(f"/jobs/{JOB_ID}", headers=BEARER)

    assert (listed.status_code, probed.status_code, job.status_code) == (503, 503, 404)
    assert str(parts) not in listed.text


def test_serve_refuses_to_start_with_a_bad_parts_file(
    outbox: Path, tmp_path: Path, uvicorn_run: list[dict[str, Any]]
) -> None:
    parts = tmp_path / "parts.toml"
    parts.write_text("[[parts")

    with pytest.raises(ConfigError):
        server.serve("127.0.0.1", 8000, outbox, 600.0, parts_file=parts)

    assert uvicorn_run == []


def test_serve_hands_the_parts_file_to_the_app(
    outbox: Path,
    tmp_path: Path,
    uvicorn_run: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = tmp_path / "parts.toml"
    parts.write_text('[[parts]]\nname = "bench-one"\nmac = "02:00:00:12:00:b8"\n')
    seen: list[Path | None] = []
    real = server.create_app

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append(kw.get("parts_file"))
        return real(*a, **kw)

    monkeypatch.setattr(server, "create_app", spy)

    server.serve("127.0.0.1", 8000, outbox, 600.0, parts_file=parts)

    assert seen == [parts]


def test_a_probe_waits_behind_a_running_job_and_leaves_status_and_outbox_alone(
    outbox: Path, token_file: Path, board_port: str, tmp_path: Path
) -> None:
    path = tmp_path / "status.json"
    gate = threading.Event()
    info = PortInfo(board_port, None, 0x10C4, 0xEA60, "0001", "1-1")

    def gated(job_dir: Path) -> None:
        gate.wait(10.0)
        remote.run_job(job_dir, 30, child("pass", "all good"))

    writer = StatusWriter(path, outbox, version="9.9.9")
    app = server.create_app(
        outbox,
        runner=gated,
        token_file=token_file,
        status=writer,
        lister=lambda: [info],
        probe_argv=probe_child_argv(),
    )
    try:
        with TestClient(app) as c:
            job_id = c.post("/jobs", json=body(), headers=BEARER).json()["id"]
            assert wait_until(lambda: status_of(path)["state"] == "running", 5.0)

            probe = c.post("/boards/probe", headers=BEARER).json()
            got = c.get(f"/boards/probe/{probe['id']}", headers=BEARER).json()
            assert got["state"] == "queued"  # never concurrent with the running job

            gate.set()
            poll_probe(c, probe["id"])
            assert wait_until(
                lambda: c.get(f"/jobs/{job_id}", headers=BEARER).json()["job"]["status"] == "done",
                10.0,
            )
            assert wait_until(lambda: status_of(path)["state"] == "idle", 5.0)
            assert [h["verdict"] for h in status_of(path)["history"]] == ["pass"]
            assert [p.name for p in outbox.iterdir()] == [job_id]
    finally:
        gate.set()
        writer.stop()
