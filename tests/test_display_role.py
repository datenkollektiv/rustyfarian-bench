"""Static checks of the display role's touch rule and unit: no Ansible, no Pi."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parent.parent / "deploy" / "ansible" / "roles" / "display"


def _read(rel: str) -> str:
    return (ROLE / rel).read_text(encoding="utf-8")


def _rule_line() -> str:
    lines = [
        ln
        for ln in _read("templates/91-rustyfarian-bench-touch.rules.j2").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert len(lines) == 1
    return lines[0]


def _unit_lines(prefix: str) -> list[str]:
    unit = _read("templates/rustyfarian-bench-display.service.j2")
    return [ln for ln in unit.splitlines() if ln.startswith(prefix)]


def test_touch_rule_overrides_group_and_mode() -> None:
    rule = _rule_line()
    assert 'GROUP:="{{ display_user }}"' in rule
    assert 'MODE:="0640"' in rule


def test_touch_rule_is_keyed_on_the_touch_name() -> None:
    assert 'ATTRS{name}=="{{ display_touch_name }}"' in _rule_line()


def test_touch_rule_never_opens_the_node_wider() -> None:
    rule = _rule_line()
    assert 'GROUP="input"' not in rule
    assert "input" not in re.findall(r'GROUP:?="([^"]*)"', rule)
    assert "0666" not in rule
    assert not re.search(r'MODE\s*[:=]*\s*"?06[0-7][0-7]"?', rule.replace("0640", ""))


def test_unit_allows_char_input_read_only() -> None:
    allows = _unit_lines("DeviceAllow=")
    assert "DeviceAllow=char-input r" in allows
    assert "DeviceAllow=char-fb rw" in allows
    assert len(allows) == 2


def test_unit_keeps_the_closed_device_policy() -> None:
    assert "DevicePolicy=closed" in _unit_lines("DevicePolicy=")


def test_unit_has_no_input_group() -> None:
    unit = _read("templates/rustyfarian-bench-display.service.j2")
    assert not re.search(r"^SupplementaryGroups=.*\binput\b", unit, re.MULTILINE)


def test_exec_start_carries_sleep_after_from_the_default() -> None:
    (exec_start,) = _unit_lines("ExecStart=")
    assert "--sleep-after {{ display_sleep_after }}" in exec_start


def test_unit_passes_the_touch_name_from_the_default() -> None:
    (env,) = _unit_lines('Environment="BENCH_TOUCH_MATCH=')
    assert "{{ display_touch_name" in env


@pytest.mark.parametrize(
    ("key", "value"),
    [("display_sleep_after", "900"), ("display_touch_name", '"ADS7846 Touchscreen"')],
)
def test_defaults(key: str, value: str) -> None:
    match = re.search(rf"^{key}: (.+)$", _read("defaults/main.yml"), re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == value
