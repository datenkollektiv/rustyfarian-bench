"""Remote flash only: the app-up line, the synthesized job, target resolution and `POST /flash`.

A flash job is a one-step check run by the unchanged `run` child; no board is needed here, the
child is `python -c` (see `test_remote.child`) and the port listing is a lambda.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rustyfarian_bench import config, flash_image, remote, server, steps
from rustyfarian_bench.boards import PortInfo, Target, UnknownPort
from rustyfarian_bench.config import ConfigError

from .fakes import (
    FACTORY,
    NVS,
    PHY_INIT,
    FakeLines,
    make_image,
    table_bytes,
    wait_until,
    with_header,
)
from .test_boards import Bench, native, port_info
from .test_remote import SCENARIO, TOKEN, child, meta

FIXTURES = Path(__file__).parent / "fixtures"
MAC = "aa:bb:cc:dd:ee:01"
PORT = "/dev/ttyUSB0"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def corrupt_table() -> bytes:
    """A partition table whose MD5 entry does not match its entries."""
    raw = bytearray(table_bytes((NVS, PHY_INIT, FACTORY)))
    raw[3 * 32 + 16] ^= 0xFF
    return bytes(raw)


C3_IMAGE = with_header(5, 0, base=make_image((NVS, PHY_INIT, FACTORY)))


def app_up(line: str) -> bool:
    return re.search(remote.APP_UP, line) is not None


# -- the app-up line ----------------------------------------------------------------------------

LAST_BOOTLOADER_LINE = "I (229) boot: Disabling RNG early entropy source..."
FIRST_APP_LINE = "INFO - [app] start hal_c3_connect_async (esp-hal)"


def transcript() -> list[str]:
    return (FIXTURES / "boot-esp-hal-c3.log").read_text(encoding="utf-8").splitlines()


def test_the_first_app_line_of_a_real_c3_boot_is_the_first_match() -> None:
    lines = transcript()
    end = lines.index(LAST_BOOTLOADER_LINE)

    assert [line for line in lines[: end + 1] if app_up(line)] == []
    assert next(line for line in lines if app_up(line)) == FIRST_APP_LINE


@pytest.mark.parametrize(
    "line",
    [
        "ets Jun  8 2016 00:22:57",
        "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
        "configsip: 0, SPIWP:0xee",
        "clk_drv:0x00,q_drv:0x00,d_drv:0x00,cs0_drv:0x00,hd_drv:0x00,wp_drv:0x00",
        "ho 0 tail 12 room 4",
        "load:0x40078000,len:13964",
        "rst:0x15 (USB_UART_CHIP_RESET),boot:0x10 (DOWNLOAD(USB/UART0))",
        "waiting for download",
        "\x1b[0;32mI (25) boot: ESP-IDF v5.3 2nd stage bootloader\x1b[0m",
        "W (40) boot.esp32: PRO CPU has been reset by WDT.",
        "I (31) boot.esp32c3: SPI Mode : DIO",
        "",
        "   ",
        "\x1b[0m",
        "12 34 56",
        "flash read err, 1000",
        "ets_main.c 371 ",
        "invalid header: 0xffffffff",
        "Rebooting...",
        "E (35) bootloader_flash: read failed",
        "E (40) flash_parts: partition 3 invalid",
        "Guru Meditation Error: Core 0 panic'ed",
        "thread 'main' panicked at src/main.rs:3",
        "abort() was called at PC 0x4",
    ],
)
def test_rom_and_bootloader_output_is_not_the_app(line: str) -> None:
    assert not app_up(line)


@pytest.mark.parametrize(
    "line",
    [
        "I (320) cpu_start: Starting scheduler on PRO CPU.",
        "\x1b[0;32mI (300) app_init: Application information:\x1b[0m",
        "I (350) main_task: Calling app_main()",
        FIRST_APP_LINE,
    ],
)
def test_an_application_line_is_the_app(line: str) -> None:
    assert app_up(line)


def test_the_crash_pattern_catches_the_three_crash_shapes() -> None:
    for line in (
        "Guru Meditation Error: Core 0 panic'ed",
        "thread 'main' panicked at src/main.rs:3",
        "abort() was called at PC 0x4",
    ):
        assert re.search(remote.CRASH, line)
    assert not re.search(remote.CRASH, FIRST_APP_LINE)


def test_the_chip_table_matches_the_servers_known_chips() -> None:
    assert set(flash_image.CHIP_IDS.values()) == remote.CHIPS


def test_the_flash_constants() -> None:
    assert (remote.EXPECT_MAX, remote.FLASH, remote.FLASH_BAUD, remote.FLASH_RESET) == (
        200,
        "flash",
        115200,
        "rts",
    )
    assert (remote.FLASH_SETTLE, remote.FLASH_TIMEOUT) == (2.0, 30.0)


# -- the generated scenario ---------------------------------------------------------------------


def make(outbox: Path, image: bytes = C3_IMAGE, **kw: Any) -> remote.Job:
    kw.setdefault("port", PORT)
    return remote.create_flash_job(outbox, image, **kw)


def layout(outbox: Path, job: remote.Job) -> Path:
    return remote.job_path(outbox, job.id)


def loaded(outbox: Path, job: remote.Job) -> config.Scenario:
    cfg = config.load(layout(outbox, job))
    (found,) = cfg.scenarios
    assert remote.remote_violations(found) == []
    return found


def test_the_default_scenario_expects_the_app_up_line(tmp_path: Path) -> None:
    found = loaded(tmp_path, make(tmp_path))

    (step,) = found.steps
    assert found.examples == ("flash",)
    assert step.expect == (remote.APP_UP,)
    assert step.reject == remote.CRASH
    assert step.arm is False  # arming drains the backlog, which may hold the banner
    assert step.settle == remote.FLASH_SETTLE
    assert step.timeout == remote.FLASH_TIMEOUT
    assert step.attended is False


@pytest.mark.parametrize(
    "expect",
    ['say "hi" \\d+ ünï', "ready", "a'b", "back\\\\slash", r"tab\tstop", "[A-Z]{3} \\(ok\\)"],
)
def test_a_custom_expect_reaches_the_step_verbatim(tmp_path: Path, expect: str) -> None:
    found = loaded(tmp_path, make(tmp_path, expect=expect))

    assert found.steps[0].expect == (expect,)
    assert found.steps[0].arm is False


def test_flash_scenario_is_toml_for_both_defaults_and_a_custom_expect() -> None:
    for text in (remote.flash_scenario(), remote.flash_scenario('say "hi" \\d+ ünï')):
        doc = tomllib.loads(text)
        assert doc["examples"] == ["flash"]
        assert len(doc["step"]) == 1
        assert doc["step"][0]["arm"] is False
    assert tomllib.loads(remote.flash_scenario('say "hi" \\d+ ünï'))["step"][0]["expect"] == [
        'say "hi" \\d+ ünï'
    ]


@pytest.mark.parametrize(
    "expect",
    ["", "a\nb", "a\x00b", "a\x1bb", "x" * (remote.EXPECT_MAX + 1), "(", "(?P=nope)", "[a-"],
    ids=["empty", "newline", "nul", "escape", "too-long", "bad-regex", "bad-ref", "open-class"],
)
def test_a_bad_expect_is_refused_and_leaves_no_job(tmp_path: Path, expect: str) -> None:
    with pytest.raises(ConfigError):
        make(tmp_path, expect=expect)

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_a_baud_of_zero_is_refused_not_defaulted(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make(tmp_path, baud=0)

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_an_app_image_without_a_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="partition table"):
        make(tmp_path, with_header(5, 0))

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_an_expect_of_exactly_the_maximum_is_accepted(tmp_path: Path) -> None:
    found = loaded(tmp_path, make(tmp_path, expect="x" * remote.EXPECT_MAX))

    assert len(found.steps[0].expect[0]) == remote.EXPECT_MAX


# -- the synthesized job ------------------------------------------------------------------------


def bench_doc(outbox: Path, job: remote.Job) -> dict[str, Any]:
    return tomllib.loads((layout(outbox, job) / "bench" / "bench.toml").read_text(encoding="utf-8"))


def test_a_port_job_is_keyed_by_chip_and_named_by_port(tmp_path: Path) -> None:
    job = make(tmp_path)

    job_dir = layout(tmp_path, job)
    board = bench_doc(tmp_path, job)["boards"]["esp32c3"]
    assert (job.kind, job.port, job.board, job.chip) == ("flash", PORT, "esp32c3", "esp32c3")
    assert (job.check, job.example) == ("flash", "flash")
    assert (board["name"], board["baud"], board["reset"]) == (PORT, 115200, "rts")
    assert "mac" not in board
    assert (job_dir / "image.bin").read_bytes() == C3_IMAGE
    assert (job_dir / "bench" / "esp32c3" / "flash.toml").is_file()
    assert job.flash == flash_image.describe(flash_image.plan(C3_IMAGE))
    assert remote.read_job(tmp_path, job.id) == job


def test_a_part_job_is_keyed_and_named_by_part_and_pins_the_mac(tmp_path: Path) -> None:
    job = make(tmp_path, part="bench-one", mac=MAC, baud=921600, reset="dtr", submitter="me")

    board = bench_doc(tmp_path, job)["boards"]["bench-one"]
    assert (job.board, job.chip, job.port, job.submitter) == ("bench-one", "esp32c3", PORT, "me")
    assert (board["name"], board["mac"], board["baud"], board["reset"]) == (
        "bench-one",
        MAC,
        921600,
        "dtr",
    )
    assert (layout(tmp_path, job) / "bench" / "bench-one" / "flash.toml").is_file()


def test_the_image_decides_the_chip(tmp_path: Path) -> None:
    job = make(tmp_path, with_header(0, 0x1000, base=make_image((NVS, PHY_INIT, FACTORY))))

    assert (job.board, job.chip) == ("esp32", "esp32")


@pytest.mark.parametrize(
    "image",
    [
        b"",
        b"\xff" * 0x9000,
        b"\xe9merged",
        with_header(5, 0, base=make_image((NVS,), table=corrupt_table())),
    ],
    ids=["empty", "erased", "short", "bad-table"],
)
def test_an_unusable_image_is_refused_and_leaves_no_job(tmp_path: Path, image: bytes) -> None:
    with pytest.raises(ConfigError):
        make(tmp_path, image)

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_a_job_json_from_before_flash_jobs_loads_as_a_check(tmp_path: Path) -> None:
    job_dir = tmp_path / "20260101-000000-abcdef"
    job_dir.mkdir()
    old = {
        "id": job_dir.name,
        "board": "c6",
        "chip": "esp32c6",
        "check": "connect",
        "example": "ex1",
        "created": "2026-01-01T00:00:00Z",
    }
    (job_dir / "job.json").write_text(json.dumps(old))

    job = remote.read_job(tmp_path, job_dir.name)

    assert job is not None
    assert (job.kind, job.port) == ("check", "")


def test_recover_closes_a_queued_flash_job(tmp_path: Path) -> None:
    job = make(tmp_path)

    remote.recover(tmp_path)

    closed = remote.read_job(tmp_path, job.id)
    assert closed is not None
    assert (closed.status, closed.verdict, closed.detail) == ("done", "error", remote.RESTARTED)
    assert closed.kind == "flash"


# -- the child ----------------------------------------------------------------------------------


def test_the_child_argv_selects_the_port_only_when_given() -> None:
    plain = remote.child_argv("flash")
    pinned = remote.child_argv("flash", 5.0, "/dev/ttyACM0")

    assert "--port" not in plain
    assert pinned[pinned.index("--port") : pinned.index("--port") + 2] == ["--port", "/dev/ttyACM0"]
    assert "--unattended" in pinned
    assert "--no-build" in pinned
    assert "--no-twins" in pinned
    assert pinned[pinned.index("--detect-timeout") + 1] == "5"


def test_run_job_hands_the_jobs_port_to_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make(tmp_path, port="/dev/ttyACM3")
    seen: list[tuple[str, float | None, str | None]] = []

    def argv(example: str, detect_timeout: float | None = None, port: str | None = None) -> Any:
        seen.append((example, detect_timeout, port))
        return child("pass", "up", board=job.board)

    monkeypatch.setattr(remote, "child_argv", argv)
    remote.run_job(layout(tmp_path, job), 30, detect_timeout=7.0)

    assert seen == [("flash", 7.0, "/dev/ttyACM3")]


def test_a_check_job_gets_no_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = remote.create_job(tmp_path, meta(), SCENARIO, C3_IMAGE)
    seen: list[str | None] = []

    def argv(example: str, detect_timeout: float | None = None, port: str | None = None) -> Any:
        seen.append(port)
        return child("pass", board="c6")

    monkeypatch.setattr(remote, "child_argv", argv)
    remote.run_job(layout(tmp_path, job), 30)

    assert seen == [None]


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [
        (child("pass", "banner seen", board="esp32c3"), "pass"),
        (child("unverified", "no", board="esp32c3"), "unverified"),
        (child(None, board="esp32c3"), "error"),
        (child("fail", "crash", board="esp32c3"), "fail"),
    ],
    ids=["pass", "unverified", "no-row", "fail"],
)
def test_a_flash_job_ends_with_the_childs_verdict(
    tmp_path: Path, argv: list[str], verdict: str
) -> None:
    job = make(tmp_path)

    remote.run_job(layout(tmp_path, job), 30, argv)

    done = remote.read_job(tmp_path, job.id)
    assert done is not None
    assert (done.status, done.verdict, done.kind) == ("done", verdict, "flash")


def test_a_flash_job_that_hangs_is_an_error(tmp_path: Path) -> None:
    job = make(tmp_path)

    remote.run_job(
        layout(tmp_path, job), 0.5, [sys.executable, "-c", "import time; time.sleep(30)"]
    )

    done = remote.read_job(tmp_path, job.id)
    assert done is not None
    assert (done.verdict, "timed out" in done.detail) == ("error", True)


# -- resolving a target -------------------------------------------------------------------------


def parts_file(tmp_path: Path, *entries: tuple[str, str]) -> Path:
    path = tmp_path / "parts.toml"
    path.write_text("".join(f'[[parts]]\nname = "{n}"\nmac = "{m}"\n' for n, m in entries))
    return path


def test_a_listed_unverified_port_resolves_without_identity() -> None:
    bench = Bench()
    bench.add(port_info(PORT))

    assert bench.registry().resolve_target(PORT, None) == Target(PORT, None, None)


def test_a_verified_port_carries_its_mac_and_name(tmp_path: Path) -> None:
    bench = Bench()
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    assert reg.resolve_target("/dev/ttyACM0", None) == Target("/dev/ttyACM0", MAC, "bench-one")


def test_a_verified_port_without_a_part_has_a_mac_and_no_name() -> None:
    bench = Bench()
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))

    assert bench.registry().resolve_target("/dev/ttyACM0", None) == Target(
        "/dev/ttyACM0", MAC, None
    )


def test_a_part_resolves_to_its_one_verified_board(tmp_path: Path) -> None:
    bench = Bench()
    bench.add(port_info(PORT))
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    assert reg.resolve_target(None, "bench-one") == Target("/dev/ttyACM0", MAC, "bench-one")


def test_a_replugged_board_is_no_longer_that_part(tmp_path: Path) -> None:
    bench = Bench()
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))
    assert reg.resolve_target(None, "bench-one").port == "/dev/ttyACM0"

    bench.remove("/dev/ttyACM0")
    bench.add(port_info("/dev/ttyACM0", 0x10C4, 0xEA60, "0001"))  # a bridge in its place

    with pytest.raises(UnknownPort):
        reg.resolve_target(None, "bench-one")


@pytest.mark.parametrize(
    ("port", "part"),
    [(None, None), (PORT, "bench-one")],
    ids=["neither", "both"],
)
def test_exactly_one_of_port_and_part(tmp_path: Path, port: str | None, part: str | None) -> None:
    bench = Bench()
    bench.add(port_info(PORT))
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    with pytest.raises(UnknownPort):
        reg.resolve_target(port, part)


def test_an_unlisted_port_is_unknown() -> None:
    with pytest.raises(UnknownPort):
        Bench().registry().resolve_target("/dev/ttyUSB9", None)


def test_a_part_without_a_parts_file_is_unknown() -> None:
    bench = Bench()
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))

    with pytest.raises(UnknownPort):
        bench.registry().resolve_target(None, "bench-one")


def test_a_part_that_is_not_attached_is_unknown(tmp_path: Path) -> None:
    bench = Bench()
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    with pytest.raises(UnknownPort):
        reg.resolve_target(None, "bench-one")


def test_a_part_on_an_unverified_port_is_unknown(tmp_path: Path) -> None:
    bench = Bench()
    bench.add(port_info(PORT))  # a bridge: the name needs a probe first
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    with pytest.raises(UnknownPort):
        reg.resolve_target(None, "bench-one")


def test_a_part_on_two_verified_boards_is_ambiguous(tmp_path: Path) -> None:
    bench = Bench()
    bench.add(native(MAC.upper(), "/dev/ttyACM0"))
    bench.add(native(MAC.upper(), "/dev/ttyACM1"))
    reg = bench.registry(parts_file=parts_file(tmp_path, ("bench-one", MAC)))

    with pytest.raises(UnknownPort):
        reg.resolve_target(None, "bench-one")


def test_a_broken_parts_file_is_a_config_error_not_an_unknown_port(tmp_path: Path) -> None:
    path = tmp_path / "parts.toml"
    path.write_text("[[parts")
    bench = Bench()
    bench.add(port_info(PORT))

    with pytest.raises(ConfigError) as caught:
        bench.registry(parts_file=path).resolve_target(PORT, None)
    assert not isinstance(caught.value, UnknownPort)


# -- POST /flash --------------------------------------------------------------------------------


def b64(image: bytes = C3_IMAGE) -> str:
    return base64.b64encode(image).decode("ascii")


def run_child(argv: list[str]) -> Callable[[Path], None]:
    return partial(remote.run_job, timeout=30, argv=argv)


def idle(job_dir: Path) -> None:
    """A runner that never runs: the job stays queued."""


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    return tmp_path / "outbox"


@pytest.fixture
def parts(tmp_path: Path) -> Path:
    return parts_file(tmp_path, ("bench-one", MAC))


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.toml"
    path.write_text(f'fluffi-mbp = "{TOKEN}"\n')
    path.chmod(0o600)
    return path


LISTING = [
    PortInfo(PORT, None, 0x10C4, 0xEA60, "0001", "1-1"),
    PortInfo("/dev/ttyACM0", None, 0x303A, 0x1001, MAC.upper(), "1-2"),
]


@contextmanager
def serving(
    outbox: Path,
    runner: Callable[[Path], None],
    *,
    parts: Path | None = None,
    token_file: Path | None = None,
) -> Iterator[TestClient]:
    app = server.create_app(
        outbox, runner=runner, parts_file=parts, token_file=token_file, lister=lambda: LISTING
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def queued(outbox: Path, parts: Path) -> Iterator[TestClient]:
    with serving(outbox, idle, parts=parts) as c:
        yield c


def nothing_queued(outbox: Path) -> bool:
    return not outbox.exists() or list(outbox.iterdir()) == []


def done(c: TestClient, job_id: str) -> bool:
    return bool(c.get(f"/jobs/{job_id}").json()["job"]["status"] == "done")


def test_a_port_flash_is_201_and_a_queued_flash_job(queued: TestClient, outbox: Path) -> None:
    posted = queued.post("/flash", json={"port": PORT, "image": b64()})

    assert posted.status_code == 201
    got = posted.json()
    assert (got["kind"], got["port"], got["status"], got["board"]) == (
        "flash",
        PORT,
        "queued",
        "esp32c3",
    )
    listed = queued.get(f"/jobs/{got['id']}").json()["job"]
    assert (listed["kind"], listed["port"]) == ("flash", PORT)
    assert (outbox / got["id"] / "image.bin").read_bytes() == C3_IMAGE


def test_a_part_flash_resolves_the_port_and_pins_the_mac(queued: TestClient, outbox: Path) -> None:
    posted = queued.post(
        "/flash",
        json={"part": "bench-one", "image": b64(), "baud": 921600, "reset": "none", "expect": "up"},
    )

    assert posted.status_code == 201
    got = posted.json()
    assert (got["port"], got["board"]) == ("/dev/ttyACM0", "bench-one")
    job_dir = outbox / got["id"]
    doc = tomllib.loads((job_dir / "bench" / "bench.toml").read_text(encoding="utf-8"))
    board = doc["boards"]["bench-one"]
    assert (board["mac"], board["baud"], board["reset"]) == (MAC, 921600, "none")
    scenario = (job_dir / "bench" / "bench-one" / "flash.toml").read_text(encoding="utf-8")
    assert tomllib.loads(scenario)["step"][0]["expect"] == ["up"]


def test_a_flash_to_a_verified_port_pins_its_mac_too(queued: TestClient, outbox: Path) -> None:
    got = queued.post("/flash", json={"port": "/dev/ttyACM0", "image": b64()}).json()

    doc = tomllib.loads((outbox / got["id"] / "bench" / "bench.toml").read_text(encoding="utf-8"))
    assert got["board"] == "bench-one"  # the parts file names this verified board
    assert (doc["boards"]["bench-one"]["mac"], doc["boards"]["bench-one"]["name"]) == (
        MAC,
        "bench-one",
    )


def test_an_unverified_port_is_keyed_by_chip(queued: TestClient) -> None:
    got = queued.post("/flash", json={"port": PORT, "image": b64()}).json()

    assert got["board"] == "esp32c3"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"port": PORT, "part": "bench-one"},
        {"port": "/dev/ttyUSB9"},
        {"port": "--help"},
        {"part": "nobody"},
        {"port": PORT, "baud": 0},
        {"port": PORT, "baud": 4_000_001},
        {"port": PORT, "reset": "bogus"},
        {"port": PORT, "expect": ""},
        {"port": PORT, "expect": "a\nb"},
        {"port": PORT, "expect": "x" * (remote.EXPECT_MAX + 1)},
        {"port": PORT, "expect": "("},
        {"port": PORT, "expect": "(?P=nope)"},
        {"port": PORT, "image": "not base64 !!"},
        {"port": PORT, "image": ""},
        {"port": PORT, "image": base64.b64encode(b"\xff" * 0x9000).decode()},
        {"port": PORT, "image": base64.b64encode(with_header(5, 0)).decode()},
    ],
    ids=[
        "neither",
        "both",
        "unlisted-port",
        "option-as-port",
        "unknown-part",
        "baud-0",
        "baud-too-high",
        "bad-reset",
        "empty-expect",
        "newline-expect",
        "long-expect",
        "bad-regex",
        "bad-ref",
        "not-base64",
        "empty-image",
        "no-header",
        "no-table",
    ],
)
def test_a_bad_flash_request_is_422_and_leaves_nothing(
    queued: TestClient, outbox: Path, fields: dict[str, Any]
) -> None:
    reply = queued.post("/flash", json={"image": b64()} | fields)

    assert reply.status_code == 422
    assert nothing_queued(outbox)


def test_a_flash_without_an_image_is_422(queued: TestClient, outbox: Path) -> None:
    assert queued.post("/flash", json={"port": PORT}).status_code == 422
    assert nothing_queued(outbox)


def test_a_part_without_a_parts_file_is_422(outbox: Path) -> None:
    with serving(outbox, idle) as c:
        assert c.post("/flash", json={"part": "bench-one", "image": b64()}).status_code == 422
    assert nothing_queued(outbox)


def test_a_parts_file_that_broke_is_503_and_leaves_nothing(outbox: Path, parts: Path) -> None:
    with serving(outbox, idle, parts=parts) as c:
        parts.write_text("[[parts")

        reply = c.post("/flash", json={"part": "bench-one", "image": b64()})

        assert reply.status_code == 503
        assert str(parts) not in reply.text
    assert nothing_queued(outbox)


def test_an_oversize_flash_image_is_413(
    queued: TestClient, outbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_IMAGE", len(C3_IMAGE) - 1)

    assert queued.post("/flash", json={"port": PORT, "image": b64()}).status_code == 413
    assert nothing_queued(outbox)


def test_the_flash_route_needs_the_token(outbox: Path, parts: Path, token_file: Path) -> None:
    with serving(outbox, idle, parts=parts, token_file=token_file) as c:
        body = {"port": PORT, "image": b64()}

        assert c.post("/flash", json=body).status_code == 401
        assert nothing_queued(outbox)
        ok = c.post("/flash", json=body, headers=BEARER)
        assert ok.status_code == 201
        assert ok.json()["submitter"] == "fluffi-mbp"


def test_a_posted_flash_runs_to_a_pass(outbox: Path, parts: Path) -> None:
    runner = run_child(child("pass", "boot banner seen", board="esp32c3"))
    with serving(outbox, runner, parts=parts) as c:
        job_id = c.post("/flash", json={"port": PORT, "image": b64()}).json()["id"]

        assert wait_until(lambda: done(c, job_id), 10.0)
        job = c.get(f"/jobs/{job_id}").json()["job"]
        assert (job["verdict"], job["kind"], job["exit_code"]) == ("pass", "flash", 0)
        assert job["detail"] == "boot banner seen"


def test_a_posted_part_flash_runs_to_a_pass(outbox: Path, parts: Path) -> None:
    runner = run_child(child("pass", "up", board="bench-one"))
    with serving(outbox, runner, parts=parts) as c:
        job_id = c.post("/flash", json={"part": "bench-one", "image": b64()}).json()["id"]

        assert wait_until(lambda: done(c, job_id), 10.0)
        assert c.get(f"/jobs/{job_id}").json()["job"]["verdict"] == "pass"


def test_flash_and_check_jobs_share_one_queue_one_at_a_time(outbox: Path, parts: Path) -> None:
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()
    finished: list[str] = []

    def runner(job_dir: Path) -> None:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
            finished.append(job_dir.name)

    check = {
        "board": {"key": "c6", "chip": "esp32c6", "name": "C6", "baud": 115200, "reset": "rts"},
        "check": "connect",
        "example": "ex1",
        "scenario": SCENARIO,
        "image": b64(),
    }
    with serving(outbox, runner, parts=parts) as c:
        ids = [
            c.post("/flash", json={"port": PORT, "image": b64()}).json()["id"],
            c.post("/jobs", json=check).json()["id"],
            c.post("/flash", json={"part": "bench-one", "image": b64()}).json()["id"],
        ]

        assert wait_until(lambda: len(finished) == 3, 10.0)
        assert finished == ids  # queue order
    assert state["peak"] == 1


# -- the generated step against boot transcripts --------------------------------------------------
# Driven through `steps.run_step`, the function `runner.Session` calls for each step, with the
# parsed step of a real generated scenario and a fake line source. A whole Session would add the
# board detection, flash and reset fakes, none of which is what is being judged here.

ROM = [
    "ESP-ROM:esp32c3-api1-20210207",
    "Build:Feb  7 2021",
    "rst:0x1 (POWERON),boot:0xd (SPI_FAST_FLASH_BOOT)",
    "SPIWP:0xee",
    "mode:DIO, clock div:2",
    "load:0x3fcd5830,len:0x155c",
    "entry 0x403cbf1a",
    "I (32) boot: ESP-IDF v6.1 2nd stage bootloader",
    "I (229) boot: Loaded app from partition at offset 0x10000",
]
PANIC = "Guru Meditation Error: Core 0 panic'ed (LoadProhibited)."
RUST_PANIC = "thread 'main' panicked at src/main.rs:3"


def judge(tmp_path: Path, *lines: str) -> steps.StepResult:
    job = make(tmp_path)
    (step,) = loaded(tmp_path, job).steps
    quick = dataclasses.replace(step, settle=0.3, timeout=0.5)
    assert quick.arm is False
    return steps.run_step(quick, FakeLines(*lines), lambda _: None)


def test_the_real_c3_boot_passes(tmp_path: Path) -> None:
    res = judge(tmp_path, *transcript())

    assert res.ok, res.detail
    assert res.evidence == (FIRST_APP_LINE,)


def test_a_panic_right_after_the_first_app_line_fails_within_the_settle(tmp_path: Path) -> None:
    lines = transcript()
    at = lines.index(FIRST_APP_LINE) + 1

    res = judge(tmp_path, *lines[:at], PANIC, *lines[at:])

    assert not res.ok
    assert "settling" in res.detail


def test_a_panic_as_the_first_app_line_fails(tmp_path: Path) -> None:
    res = judge(tmp_path, *ROM, RUST_PANIC)

    assert not res.ok
    assert "rejected" in res.detail


def test_a_panic_line_is_never_taken_for_the_app_coming_up(tmp_path: Path) -> None:
    res = judge(tmp_path, *ROM, PANIC, FIRST_APP_LINE)

    assert not res.ok
    assert "rejected" in res.detail


def test_rom_and_bootloader_output_alone_times_out(tmp_path: Path) -> None:
    res = judge(tmp_path, *ROM)

    assert not res.ok
    assert "timeout" in res.detail


def test_a_boot_that_cannot_read_flash_times_out(tmp_path: Path) -> None:
    res = judge(tmp_path, "flash read err, 1000", "ets_main.c 371 ", "Rebooting...")

    assert not res.ok
    assert "timeout" in res.detail
