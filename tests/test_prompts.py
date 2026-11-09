"""Prompt placeholders: `say` and `confirm` take the probe fields, `{mac}` and earlier captures.

A value that is missing renders as a marker (`{ip}?`) and is recorded on the step result as
`lacked`; it never fails a prompt. A `probe` or `run` command is stricter and fails unspawned.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from rustyfarian_bench import ui
from rustyfarian_bench.config import Step

from .fakes import FakeLines, FakeProbeLines, FakeProbes
from .test_runner import MAC, MISSING_CAPTURE, _probed, _rows, _watch, _with_mac

NO_MAC = "no value for {mac}: the board's MAC address is unknown"
OLD_STEP_KEYS = {
    "say", "ok", "detail", "optional", "unverified", "note", "captured", "evidence",
}  # fmt: skip


def _shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect the step titles the operator is shown."""
    shown: list[str] = []

    def step(n: int, total: int, msg: str) -> None:
        shown.append(msg)

    monkeypatch.setattr(ui, "step", step)
    return shown


def _operator(monkeypatch: pytest.MonkeyPatch, retries: int = 0, note: str = "") -> list[str]:
    """Interactive answers: retry a failed step `retries` times; everything else is no.

    Returns the questions asked, in order.
    """
    asked: list[str] = []
    left = [retries]

    def ask(question: str, default: bool = True) -> bool:
        asked.append(question)
        if question == "Retry this step?" and left[0] > 0:
            left[0] -= 1
            return True
        return False

    monkeypatch.setattr(ui, "ask", ask)
    monkeypatch.setattr(ui, "ready", lambda msg: None)
    monkeypatch.setattr(ui, "note", lambda question: note)
    return asked


def _learn(name: str = "cid", *, attended: bool = False, optional: bool = False) -> Step:
    pattern = rf"client (?P<{name}>\w+)"
    return Step(
        say="learn",
        expect=(pattern,),
        arm=False,
        timeout=0.3,
        attended=attended,
        optional=optional,
    )


def _plain(say: str) -> Step:
    return Step(say=say, expect=("a",), arm=False, timeout=0.3)


# Rendering.


def test_a_prompt_renders_every_reserved_field_and_a_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn(), _plain("{port} {example} {board} {chip} {mac} {cid}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("client abc1", "a")
    )
    _with_mac(monkeypatch)

    result = session.run_example(scenario, "ex")

    want = f"/dev/x ex s3 esp32s3 {MAC} abc1"
    assert shown == ["learn", want]
    assert result.steps[1].say == want
    assert result.steps[1].lacked == ()
    assert result.verdict == "pass"


def test_the_terminal_the_result_and_results_jsonl_show_the_same_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, (_plain("on {port} ({chip})"),), serial=FakeLines("a")
    )

    result = session.run_example(scenario, "ex")

    [row] = _rows(tmp_path)
    [step] = cast(list[dict[str, object]], row["steps"])
    assert shown == ["on /dev/x (esp32s3)"]
    assert result.steps[0].say == shown[0]
    assert step["say"] == shown[0]


def test_a_rendered_confirm_question_is_what_gets_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    asked: list[str] = []

    def ask(question: str, default: bool = True) -> bool:
        asked.append(question)
        return True

    monkeypatch.setattr(ui, "ask", ask)
    monkeypatch.setattr(ui, "ready", lambda msg: None)
    steps = (Step(say="look at {port}", confirm="is {chip} lit?"),)
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, steps, yes=False)

    result = session.run_example(scenario, "ex")

    assert "is esp32s3 lit?" in asked
    assert shown == ["look at /dev/x"]
    assert result.steps[0].say == "look at /dev/x"
    assert result.verdict == "pass"


def test_doubled_braces_render_as_single_braces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, (_plain("press {{x}} on {port}"),), serial=FakeLines("a")
    )

    result = session.run_example(scenario, "ex")

    assert shown == ["press {x} on /dev/x"]
    assert result.steps[0].lacked == ()


def test_a_prompt_without_braces_is_shown_verbatim_and_lacks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, (_plain("plain prompt"),), serial=FakeLines("a")
    )

    result = session.run_example(scenario, "ex")

    assert shown == ["plain prompt"]
    assert result.steps[0].lacked == ()


# A missing value is a marker, not a failure.


def test_a_prompt_field_nothing_captured_renders_a_marker_and_the_step_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    # The first step declares `cid` but its optional group does not take part, so nothing
    # is captured; it consumes one line and the second step the other.
    declares = Step(say="learn", expect=(r"(?:client (?P<cid>\w+))?a",), arm=False, timeout=0.3)
    steps = (declares, _plain("ip {cid}"))
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, steps, serial=FakeLines("a", "a"))

    result = session.run_example(scenario, "ex")

    assert shown == ["learn", "ip {cid}?"]
    assert result.steps[1].say == "ip {cid}?"
    assert result.steps[1].ok
    assert result.steps[1].lacked == ("cid",)
    assert result.steps[0].lacked == ()


def test_a_capture_skipped_by_the_mode_gives_a_marker_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn(attended=True), _plain("ip {cid}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("a"), mode="unattended"
    )

    result = session.run_example(scenario, "ex")

    assert shown == ["ip {cid}?"]
    assert result.steps[0].ok
    assert result.steps[0].lacked == ("cid",)
    assert result.verdict == "pass"


def test_a_step_that_recaptures_a_name_leaves_no_stale_value_for_a_prompt_after_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn(), replace(_learn(attended=True), say="relearn"), _plain("ip {cid}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("client abc1", "a"), mode="unattended"
    )

    session.run_example(scenario, "ex")

    assert shown == ["learn", "ip {cid}?"]


def test_one_entry_lists_every_missing_name_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_plain("{a} {b} {port} {a}"),)
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, steps, serial=FakeLines("a"))

    result = session.run_example(scenario, "ex")

    assert shown == ["{a}? {b}? /dev/x {a}?"]
    assert result.steps[0].lacked == ("a", "b")


def test_a_mac_nobody_detected_renders_a_marker_and_the_step_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, (_plain("pause {mac}"),), serial=FakeLines("a")
    )

    result = session.run_example(scenario, "ex")

    assert shown == ["pause {mac}?"]
    assert result.steps[0].lacked == ("mac",)
    assert result.verdict == "pass"


def test_a_missing_capture_in_expect_still_fails_and_shows_a_rendered_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    step = Step(say="echo on {port} for {ip}", expect=(r"echo (?P=tok)",), timeout=0.3, arm=False)
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, (step,), serial=FakeLines("echo 7"))

    result = session.run_example(scenario, "ex")

    assert shown == ["echo on /dev/x for {ip}?"]
    assert result.steps[0].say == "echo on /dev/x for {ip}?"
    assert result.steps[0].detail == MISSING_CAPTURE  # the old wording, unchanged
    assert result.steps[0].lacked == ("ip",)
    assert result.verdict == "fail"


# Reserved names win over a capture of the same name.


def test_a_reserved_name_shadowed_by_a_capture_shows_the_reserved_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn("port"), _plain("on {port}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("client zzz", "a")
    )

    session.run_example(scenario, "ex")

    assert shown == ["learn", "on /dev/x"]


def test_a_capture_named_mac_does_not_stand_in_for_a_mac_nobody_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn("mac"), _plain("pause {mac}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("client zz", "a")
    )

    result = session.run_example(scenario, "ex")

    assert shown == ["learn", "pause {mac}?"]
    assert result.steps[1].lacked == ("mac",)


def test_a_detected_mac_wins_over_a_capture_named_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    steps = (_learn("mac"), _plain("pause {mac}"))
    session, scenario, _, _ = _probed(
        tmp_path, monkeypatch, steps, serial=FakeLines("client zz", "a")
    )
    _with_mac(monkeypatch)

    result = session.run_example(scenario, "ex")

    assert shown == ["learn", f"pause {MAC}"]
    assert result.steps[1].lacked == ()


# Per attempt, and how the record is kept.


def test_a_retried_step_renders_its_prompt_again_and_lacks_a_name_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    asked = _operator(monkeypatch, retries=1)
    step = Step(say="ip {ip} on {port}", expect=("zzz",), arm=False, timeout=0.3)
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, (step,), yes=False)

    result = session.run_example(scenario, "ex")

    assert asked.count("Retry this step?") == 2
    assert shown == ["ip {ip}? on /dev/x"] * 2
    assert result.steps[0].lacked == ("ip",)  # one entry per step, not per attempt


def test_lacked_survives_the_note_a_failed_step_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shown(monkeypatch)
    _operator(monkeypatch, note="operator said so")
    step = Step(say="ip {ip}", expect=("zzz",), arm=False, timeout=0.3)
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, (step,), yes=False)

    result = session.run_example(scenario, "ex")

    assert result.steps[0].failed
    assert result.steps[0].note == "operator said so"
    assert result.steps[0].lacked == ("ip",)
    [row] = _rows(tmp_path)
    [recorded] = cast(list[dict[str, object]], row["steps"])
    assert recorded["note"] == "operator said so"
    assert recorded["lacked"] == ["ip"]


def test_results_jsonl_records_lacked_and_keeps_every_old_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shown(monkeypatch)
    steps = (_plain("fine"), _plain("ip {ip}"))
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, steps, serial=FakeLines("a", "a"))

    session.run_example(scenario, "ex")

    [row] = _rows(tmp_path)
    fine, lacking = cast(list[dict[str, object]], row["steps"])
    assert fine.keys() >= OLD_STEP_KEYS
    assert lacking.keys() >= OLD_STEP_KEYS
    assert fine["lacked"] == []
    assert lacking["lacked"] == ["ip"]


def test_a_lacked_name_is_warned_about_once_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shown(monkeypatch)
    warnings: list[str] = []
    monkeypatch.setattr(ui, "warn", warnings.append)
    steps = (_plain("{a} and {b}"), _plain("fine"))
    session, scenario, _, _ = _probed(tmp_path, monkeypatch, steps, serial=FakeLines("a", "a"))

    session.run_example(scenario, "ex")

    [warning] = [w for w in warnings if w.startswith("prompt lacked")]
    assert "{a}" in warning
    assert "{b}" in warning


# {mac} in a command is not a marker: the step fails before anything is spawned.


def test_a_probe_with_mac_unknown_fails_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = replace(_watch(), probe=("watch", "{port}", "{mac}"))
    session, scenario, _, probes = _probed(tmp_path, monkeypatch, (step,))

    result = session.run_example(scenario, "ex")

    assert probes.calls == []
    assert result.verdict == "fail"
    assert result.steps[0].detail == NO_MAC


def test_a_probe_takes_the_detected_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    step = replace(_watch(), probe=("watch", "{port}", "{mac}"))
    probes = FakeProbes(FakeProbeLines("online"))
    session, scenario, _, probes = _probed(tmp_path, monkeypatch, (step,), probes=probes)
    _with_mac(monkeypatch)

    result = session.run_example(scenario, "ex")

    assert [c.argv for c in probes.calls] == [["watch", "/dev/x", MAC]]
    assert result.verdict == "pass"


def test_a_probe_step_may_name_the_mac_in_its_prompt_when_it_is_unknown_only_in_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _shown(monkeypatch)
    step = replace(_watch(), say="watch {mac}", probe=("watch", "{port}"))
    probes = FakeProbes(FakeProbeLines("online"))
    session, scenario, _, probes = _probed(tmp_path, monkeypatch, (step,), probes=probes)

    result = session.run_example(scenario, "ex")

    assert shown == ["watch {mac}?"]
    assert len(probes.calls) == 1
    assert result.steps[0].lacked == ("mac",)
    assert result.verdict == "pass"


def x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    step = replace(_watch(), probe=("watch", "{cid}"))
    session, scenario, _, probes = _probed(tmp_path, monkeypatch, (step,))

    result = session.run_example(scenario, "ex")

    assert probes.calls == []
    assert result.steps[0].detail.startswith("nothing captured for cid")
