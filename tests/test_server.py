"""The HTTP face of the remote bench, through FastAPI's in-process TestClient (no socket)."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rustyfarian_bench import remote, server

from .fakes import wait_until
from .test_remote import IMAGE, JOB_ID, SCENARIO, child, meta


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
