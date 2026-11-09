from __future__ import annotations

import re
from pathlib import Path

import pytest

from rustyfarian_bench.config import BenchConfig, ConfigError, Step, load

BENCH = """
[commands]
build = ["just", "build-example", "{example}"]
flash = ["just", "flash", "{example}", "{port}"]

[boards.s3]
chip = "esp32s3"
name = "CrowPanel"
"""

B3F = """
examples = ["hal_s3_b3f", "idf_s3_b3f"]

[[step]]
say = "banner"
expect = "ready"

[[step]]
say = "rotate"
duration = 5
reject = "pressed"
"""


def write(repo: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = repo / "bench" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def test_load_and_find(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, {"bench.toml": BENCH, "s3/b3f.toml": B3F}))
    assert cfg.build == ("just", "build-example", "{example}")
    (s,) = cfg.find("idf_s3_b3f")
    assert s.name == "b3f"
    assert s.board.chip == "esp32s3"
    assert s.steps[0].expect == ("ready",)
    assert cfg.find("s3/b3f") == (s,)
    assert cfg.find("s3") == (s,)


def test_unknown_target(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, {"bench.toml": BENCH, "s3/b3f.toml": B3F}))
    with pytest.raises(ConfigError):
        cfg.find("hal_c3_b3f")


def test_step_needs_exactly_one_kind(tmp_path: Path) -> None:
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpect = "a"\nduration = 1\n'
    with pytest.raises(ConfigError, match="exactly one"):
        load(write(tmp_path, {"bench.toml": BENCH, "s3/bad.toml": bad}))


def test_unknown_step_key(tmp_path: Path) -> None:
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpcet = "a"\n'
    with pytest.raises(ConfigError, match="unknown keys"):
        load(write(tmp_path, {"bench.toml": BENCH, "s3/bad.toml": bad}))


def test_cli_board_and_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rustyfarian_bench import cli, runner

    write(tmp_path, {"bench.toml": BENCH, "s3/b3f.toml": B3F})
    seen: list[tuple[str, ...]] = []

    def fake_run(self: object, scenarios: tuple[object, ...], first_example: str) -> str:
        seen.append((first_example, *(s.name for s in scenarios)))  # type: ignore[attr-defined]
        return "pass"

    monkeypatch.setattr(runner.Session, "run", fake_run)
    assert cli.main(["--repo", str(tmp_path), "run", "s3", "b3f", "--no-build"]) == 0
    assert cli.main(["--repo", str(tmp_path), "run", "hal_s3_b3f"]) == 0
    assert seen == [("s3/b3f", "b3f"), ("hal_s3_b3f", "b3f")]


def test_step_arm_defaults_true_and_parses(tmp_path: Path) -> None:
    from rustyfarian_bench.config import _parse_step

    assert _parse_step({"say": "x", "expect": "a"}, "t").arm is True
    assert _parse_step({"say": "x", "expect": "a", "arm": False}, "t").arm is False


def test_board_checks_run_first_in_the_listed_order(tmp_path: Path) -> None:
    bench = BENCH + 'checks = ["pulse", "rainbow"]\n'
    files = {"bench.toml": bench} | {f"s3/{n}.toml": B3F for n in ("colors", "rainbow", "pulse")}
    cfg = load(write(tmp_path, files))
    assert [s.name for s in cfg.find("s3")] == ["pulse", "rainbow", "colors"]


def test_board_checks_must_name_existing_scenarios(tmp_path: Path) -> None:
    bench = BENCH + 'checks = ["pulse"]\n'
    with pytest.raises(ConfigError, match="pulse"):
        load(write(tmp_path, {"bench.toml": bench, "s3/b3f.toml": B3F}))


def scenario(*step_lines: str, examples: str = '["x"]') -> str:
    return f'examples = {examples}\n[[step]]\nsay = "s"\n' + "\n".join(step_lines) + "\n"


def load_scenario(repo: Path, text: str, bench: str = BENCH) -> BenchConfig:
    return load(write(repo, {"bench.toml": bench, "s3/c.toml": text}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("optional", '"false"'),
        ("optional", "1"),
        ("arm", '"true"'),
        ("arm", "0"),
        ("attended", '"true"'),
        ("attended", "1"),
    ],
)
def test_bool_fields_reject_non_bool(tmp_path: Path, field: str, value: str) -> None:
    with pytest.raises(ConfigError, match=field):
        load_scenario(tmp_path, scenario('expect = "a"', f"{field} = {value}"))


@pytest.mark.parametrize("field", ["timeout", "duration", "settle"])
@pytest.mark.parametrize("value", ['"5"', "true", "[1]", "nan", "inf", "-inf"])
def test_numeric_fields_need_finite_real_numbers(tmp_path: Path, field: str, value: str) -> None:
    kind = 'reject = "r"' if field == "settle" else ""
    lines = ['expect = "a"', f"{field} = {value}"]
    if field == "duration":
        lines = ['reject = "r"', f"duration = {value}"]
    with pytest.raises(ConfigError, match=field):
        load_scenario(tmp_path, scenario(*lines, kind))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", "0"),
        ("timeout", "-1"),
        ("duration", "0"),
        ("duration", "-2.5"),
        ("settle", "-1"),
    ],
)
def test_numeric_ranges(tmp_path: Path, field: str, value: str) -> None:
    lines = ['expect = "a"', f"{field} = {value}"]
    if field == "duration":
        lines = [f"duration = {value}"]
    with pytest.raises(ConfigError, match=field):
        load_scenario(tmp_path, scenario(*lines))


def test_numeric_boundaries_load(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, scenario('expect = "a"', "settle = 0", "timeout = 5"))
    cfg2 = load(write(tmp_path, {"s3/d.toml": scenario("duration = 0.001")}))
    assert cfg2.scenarios[-1].steps[0].duration == 0.001
    assert cfg.scenarios[0].steps[0].timeout == 5.0


@pytest.mark.parametrize(
    "line",
    [
        "reject = 5",
        "reject = ['a']",
        "reject = ''",
        "confirm = 5",
        "confirm = true",
        "confirm = ''",
        "expect = []",
        "expect = [1]",
        "expect = ''",
    ],
)
def test_string_fields_are_type_checked(tmp_path: Path, line: str) -> None:
    key = line.split(" =")[0]
    extra = 'expect = "a"' if key == "reject" else ""
    with pytest.raises(ConfigError, match=key):
        load_scenario(tmp_path, scenario(line, extra))


@pytest.mark.parametrize("say", ["say = 5", 'say = ""'])
def test_say_must_be_non_empty_string(tmp_path: Path, say: str) -> None:
    text = f'examples = ["x"]\n[[step]]\n{say}\nexpect = "a"\n'
    with pytest.raises(ConfigError, match="say"):
        load_scenario(tmp_path, text)


@pytest.mark.parametrize("pattern", ["(", "[a-"])
def test_invalid_regex_fails_at_load(tmp_path: Path, pattern: str) -> None:
    for line in (f"reject = '{pattern}'", f"expect = ['ok', '{pattern}']"):
        extra = 'expect = "a"' if line.startswith("reject") else ""
        with pytest.raises(ConfigError, match=r"c\.toml step 1"):
            load_scenario(tmp_path, scenario(line, extra))


def test_examples_as_a_single_string_still_loads(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, scenario('expect = "a"', examples='"solo"'))
    assert cfg.scenarios[0].examples == ("solo",)


@pytest.mark.parametrize("examples", ['""', '"a/b"'])
def test_examples_single_string_is_checked_too(tmp_path: Path, examples: str) -> None:
    with pytest.raises(ConfigError, match="examples"):
        load_scenario(tmp_path, scenario('expect = "a"', examples=examples))


@pytest.mark.parametrize("examples", ["[]", "[1]", '["a/b"]', '["../x"]', '[""]'])
def test_examples_must_be_safe_names(tmp_path: Path, examples: str) -> None:
    with pytest.raises(ConfigError, match="examples"):
        load_scenario(tmp_path, scenario('expect = "a"', examples=examples))


@pytest.mark.parametrize(
    ("build", "flash", "field"),
    [
        ('["just", "{board}"]', '["f", "{example}"]', "commands.build"),
        ('["just", "{port}"]', '["f", "{example}"]', "commands.build"),
        ('["just", "{example"]', '["f"]', "commands.build"),
        ('["just", "{0}"]', '["f"]', "commands.build"),
        ("[]", '["f"]', "commands.build"),
        ('["b"]', '["f", "{chip}"]', "commands.flash"),
        ('["b"]', "[]", "commands.flash"),
    ],
)
def test_command_placeholders_validated_at_load(
    tmp_path: Path, build: str, flash: str, field: str
) -> None:
    bench = BENCH.replace(
        'build = ["just", "build-example", "{example}"]', f"build = {build}"
    ).replace('flash = ["just", "flash", "{example}", "{port}"]', f"flash = {flash}")
    with pytest.raises(ConfigError, match=field):
        load_scenario(tmp_path, scenario('expect = "a"'), bench)


@pytest.mark.parametrize(
    "boards",
    [
        '[boards.s3]\nname = "x"\n',
        "[boards.s3]\nchip = 5\n",
        '[boards.s3]\nchip = ""\n',
        '[boards.s3]\nchip = "esp32s3"\nname = 5\n',
        '[boards.s3]\nchip = "esp32s3"\nmac = 5\n',
        '[boards.s3]\nchip = "esp32s3"\nchecks = [1]\n',
        "[boards]\ns3 = 'x'\n",
    ],
)
def test_board_fields_are_validated(tmp_path: Path, boards: str) -> None:
    bench = BENCH.split("[boards.s3]")[0] + boards
    with pytest.raises(ConfigError, match=r"s3|boards"):
        load_scenario(tmp_path, scenario('expect = "a"'), bench)


def test_malformed_toml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, {"bench.toml": "[commands\n"}))
    with pytest.raises(ConfigError, match=r"c\.toml"):
        load_scenario(tmp_path, "examples = [\n")


def test_unreadable_toml_is_a_config_error(tmp_path: Path) -> None:
    repo = write(tmp_path, {"bench.toml": BENCH, "s3/c.toml": scenario('expect = "a"')})
    (repo / "bench" / "s3" / "c.toml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError):
        load(repo)


@pytest.mark.parametrize("steps", ['step = {say = "s", expect = "a"}\n', "step = [1]\n"])
def test_step_must_be_array_of_tables(tmp_path: Path, steps: str) -> None:
    with pytest.raises(ConfigError, match="step"):
        load_scenario(tmp_path, 'examples = ["x"]\n' + steps)


def test_every_key_still_loads(tmp_path: Path) -> None:
    text = """
examples = ["a", "b"]

[[step]]
say = "banner"
expect = ["one", "two"]
timeout = 10
settle = 0.5
reject = "panic|abort"
arm = false

[[step]]
say = "watch"
duration = 5
reject = "pressed"
optional = true

[[step]]
say = "look"
confirm = "Is it green?"
"""
    (s,) = load_scenario(tmp_path, text).scenarios
    a, b, c = s.steps
    assert (a.expect, a.timeout, a.settle, a.reject, a.arm) == (
        ("one", "two"),
        10.0,
        0.5,
        "panic|abort",
        False,
    )
    assert (b.duration, b.optional, b.reject) == (5.0, True, "pressed")
    assert c.confirm == "Is it green?"
    assert s.examples == ("a", "b")


@pytest.mark.parametrize(
    ("checks", "listed"),
    [('["c", "c"]', "['c']"), ('["a", "b", "a", "b"]', "['a', 'b']")],
)
def test_duplicate_names_in_board_checks_are_rejected(
    tmp_path: Path, checks: str, listed: str
) -> None:
    bench = BENCH + f"checks = {checks}\n"
    with pytest.raises(ConfigError, match=r"s3.*checks.*duplicate.*" + re.escape(listed)):
        load_scenario(tmp_path, scenario('expect = "a"'), bench)


def test_distinct_names_in_board_checks_load(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, scenario('expect = "a"'), BENCH + 'checks = ["c"]\n')

    assert [s.name for s in cfg.scenarios] == ["c"]


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ('build = ["just", "{porrt}"]', "build"),
        ('build = ["just", "{port}"]', "build"),
        ("build = []", "build"),
        ('flash = ["f", "{chip}"]', "flash"),
        ("flash = []", "flash"),
    ],
)
def test_board_command_placeholders_validated_at_load(
    tmp_path: Path, line: str, field: str
) -> None:
    with pytest.raises(ConfigError, match=rf"s3\.{field}"):
        load_scenario(tmp_path, scenario('expect = "a"'), BENCH + line + "\n")


def test_board_commands_override_the_global_ones(tmp_path: Path) -> None:
    bench = BENCH + 'build = ["just", "build-avr-bin", "{example}"]\ndetect = false\n'
    cfg = load(write(tmp_path, {"bench.toml": bench, "s3/b3f.toml": B3F}))
    board = cfg.boards["s3"]
    assert board.build == ("just", "build-avr-bin", "{example}")
    assert board.flash == ()
    assert board.detect is False
    assert cfg.build_for(board) == ("just", "build-avr-bin", "{example}")
    assert cfg.flash_for(board) == cfg.flash


def test_board_serial_settings_default_to_rts_at_115200(tmp_path: Path) -> None:
    board = load_scenario(tmp_path, scenario('expect = "a"')).boards["s3"]
    assert (board.baud, board.reset) == (115_200, "rts")


@pytest.mark.parametrize("reset", ["rts", "dtr", "none"])
def test_board_serial_settings_are_read(tmp_path: Path, reset: str) -> None:
    bench = BENCH + f'baud = 57600\nreset = "{reset}"\n'
    board = load_scenario(tmp_path, scenario('expect = "a"'), bench).boards["s3"]
    assert (board.baud, board.reset) == (57_600, reset)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("baud = 0", "baud"),
        ("baud = -9600", "baud"),
        ("baud = true", "baud"),
        ('baud = "9600"', "baud"),
        ("baud = 9600.0", "baud"),
        ('reset = "usb"', "reset"),
        ("reset = true", "reset"),
    ],
)
def test_board_serial_settings_rejected(tmp_path: Path, line: str, field: str) -> None:
    with pytest.raises(ConfigError, match=rf"s3\.{field}"):
        load_scenario(tmp_path, scenario('expect = "a"'), BENCH + line + "\n")


def test_attended_parses_and_defaults_to_false(tmp_path: Path) -> None:
    text = scenario('expect = "a"', "attended = true") + '[[step]]\nsay = "t"\nexpect = "b"\n'
    cfg = load_scenario(tmp_path, text)

    first, second = cfg.scenarios[0].steps
    assert first.attended is True
    assert second.attended is False


def test_needs_person_for_confirm_attended_and_plain_expect() -> None:
    assert Step(say="s", confirm="ok?").needs_person
    assert Step(say="s", expect=("a",), attended=True).needs_person
    assert not Step(say="s", expect=("a",)).needs_person


def test_board_usb_identity_is_parsed(tmp_path: Path) -> None:
    bench = BENCH + 'detect = false\nusb_id = "0403:6001"\nusb_serial = "AQ01L7M0"\n'
    board = load(write(tmp_path, {"bench.toml": bench, "s3/b3f.toml": B3F})).boards["s3"]
    assert (board.usb_id, board.usb_serial) == ("0403:6001", "AQ01L7M0")


@pytest.mark.parametrize(
    "extra",
    [
        'detect = false\nusb_id = "403:6001"\n',
        'detect = false\nusb_serial = "AQ01L7M0"\n',
        'usb_id = "0403:6001"\n',
    ],
    ids=["bad-format", "serial-without-id", "usb-on-a-detected-board"],
)
def test_board_usb_identity_is_checked(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError, match="usb_"):
        load(write(tmp_path, {"bench.toml": BENCH + extra, "s3/b3f.toml": B3F}))


POLES = """examples = ["x"]
[[step]]
say = "any pole"
expect = 'dev=\\d+\\s+(?P<pole>SOUTH|NORTH)'
[[step]]
say = "the other pole"
expect = 'dev=\\d+\\s+(?!(?P=pole))(?:SOUTH|NORTH)'
reject = 'PANIC (?P=pole)'
"""


def test_a_later_step_may_refer_to_an_earlier_capture(tmp_path: Path) -> None:
    cfg = load(write(tmp_path, {"bench.toml": BENCH, "s3/hall.toml": POLES}))
    assert len(cfg.find("s3")[0].steps) == 2


def test_a_reference_without_an_earlier_capture_is_a_config_error(tmp_path: Path) -> None:
    bad = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpect = \'(?!(?P=pole))NORTH\'\n'
    with pytest.raises(ConfigError, match=r"step 1: 'expect' refers to \(\?P=pole\)"):
        load(write(tmp_path, {"bench.toml": BENCH, "s3/bad.toml": bad}))


def test_a_back_reference_inside_its_own_pattern_stays_a_back_reference() -> None:
    from rustyfarian_bench.config import references, resolve

    assert references(r"(?P<d>\d)-(?P=d)") == set()
    assert resolve(r"(?P<d>\d)-(?P=d) (?P=pole)", {"pole": "a.b"}) == r"(?P<d>\d)-(?P=d) (?:a\.b)"


@pytest.mark.parametrize(
    "pattern",
    [
        r"[(?P=pole)]+",  # a character class: literal characters
        r"\(?P=pole\)",  # escaped parentheses
        "(?x) NORTH  # unlike (?P=pole)\n SOUTH",  # a verbose-mode comment
        r"[\](?P=pole)x]",  # an escaped ] does not end the class
    ],
)
def test_reference_syntax_inside_classes_escapes_and_comments_is_not_a_reference(
    pattern: str,
) -> None:
    from rustyfarian_bench.config import references, resolve

    assert references(pattern) == set()
    assert resolve(pattern, {}) == pattern


def test_resolve_substitutes_only_the_real_reference() -> None:
    from rustyfarian_bench.config import resolve

    assert resolve(r"[(?P=pole)] (?P=pole)", {"pole": "N"}) == r"[(?P=pole)] (?:N)"


def test_a_group_name_inside_a_class_is_not_a_definition() -> None:
    from rustyfarian_bench.config import references

    assert references(r"[(?P<pole>)] (?P=pole)") == {"pole"}


def test_a_character_class_that_looks_like_a_reference_loads(tmp_path: Path) -> None:
    ok = 'examples = ["x"]\n[[step]]\nsay = "s"\nexpect = \'[(?P=pole)]+\'\n'
    load(write(tmp_path, {"bench.toml": BENCH, "s3/ok.toml": ok}))


@pytest.mark.parametrize(
    ("pattern", "refs"),
    [
        ("(?x:NORTH # unlike (?P=pole)\n)", set()),  # a scoped verbose comment
        ("(?x)(?-x:# (?P=pole))", {"pole"}),  # verbose switched off: # is literal
        ("(?x:a)# (?P=pole)", {"pole"}),  # the verbose scope has ended
        ("(?x:(?P<p>a)# (?P=pole)\n)", set()),  # a named group inside the scope
        ("(?i)(?x) NORTH # unlike (?P=pole)\n", set()),  # verbose in a later global group
    ],
)
def test_scoped_verbose_flags_decide_what_is_a_comment(pattern: str, refs: set[str]) -> None:
    from rustyfarian_bench.config import references

    assert references(pattern) == refs


PROBE = 'probe = ["just", "mqtt-watch", "{example}"]'


def test_probe_fields_are_the_reserved_placeholders() -> None:
    from rustyfarian_bench.config import PROBE_FIELDS

    expected = frozenset({"port", "example", "board", "chip"})
    assert expected == PROBE_FIELDS


def test_placeholders_lists_the_names_a_command_uses() -> None:
    from rustyfarian_bench.config import placeholders

    assert placeholders(("just", "{example}", "{port}:{port}", "plain")) == {"example", "port"}
    assert placeholders(("echo", "{{literal}}")) == set()
    assert placeholders(()) == set()


def test_probe_parses_with_expect(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, scenario(PROBE, 'expect = "online"'))
    (step,) = cfg.scenarios[0].steps
    assert step.probe == ("just", "mqtt-watch", "{example}")
    assert step.expect == ("online",)


def test_probe_parses_with_duration(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, scenario(PROBE, "duration = 5", 'reject = "refused"'))
    (step,) = cfg.scenarios[0].steps
    assert step.probe == ("just", "mqtt-watch", "{example}")
    assert step.duration == 5.0


def test_a_step_without_probe_has_an_empty_one() -> None:
    assert Step(say="s", expect=("a",)).probe == ()


def test_a_probe_step_needs_no_person() -> None:
    assert not Step(say="s", probe=("true",), expect=("a",)).needs_person


def test_a_probe_alone_is_not_a_step_kind(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="exactly one"):
        load_scenario(tmp_path, scenario(PROBE))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("[]", "must not be empty"),
        ('[1, "x"]', "expected a string or a list of strings"),
        ('["just", "{nope}"]', "unknown placeholder {nope}"),
        ('["just", "{unclosed"]', "bad placeholder"),
    ],
    ids=["empty", "non-string", "unknown-placeholder", "bad-brace"],
)
def test_a_malformed_probe_is_a_config_error(tmp_path: Path, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_scenario(tmp_path, scenario(f"probe = {value}", 'expect = "a"'))


@pytest.mark.parametrize(
    ("other", "word"),
    [
        ('confirm = "ok?"', "confirm"),
        ("attended = true", "attended"),
        ("reset = true", "reset"),
    ],
)
def test_a_probe_step_cannot_also_need_a_person_or_reset(
    tmp_path: Path, other: str, word: str
) -> None:
    lines = [PROBE, other] if word == "confirm" else [PROBE, 'expect = "a"', other]
    with pytest.raises(ConfigError, match=word):
        load_scenario(tmp_path, scenario(*lines))


@pytest.mark.parametrize("name", ["port", "example", "board", "chip"])
def test_probe_accepts_each_reserved_placeholder(tmp_path: Path, name: str) -> None:
    cfg = load_scenario(tmp_path, scenario(f'probe = ["watch", "{{{name}}}"]', 'expect = "a"'))
    assert cfg.scenarios[0].steps[0].probe == ("watch", f"{{{name}}}")


CAPTURING = """examples = ["x"]
[[step]]
say = "learn the id"
expect = 'client (?P<cid>\\w+)'
[[step]]
say = "watch the broker"
probe = ["watch", "{cid}"]
expect = "online"
"""


def test_probe_accepts_a_name_an_earlier_step_captured(tmp_path: Path) -> None:
    cfg = load_scenario(tmp_path, CAPTURING)
    assert cfg.scenarios[0].steps[1].probe == ("watch", "{cid}")


def test_probe_rejects_a_name_only_a_later_step_captures(tmp_path: Path) -> None:
    text = (
        'examples = ["x"]\n[[step]]\nsay = "watch"\nprobe = ["watch", "{cid}"]\nexpect = "a"\n'
        "[[step]]\nsay = \"learn\"\nexpect = 'client (?P<cid>\\w+)'\n"
    )
    with pytest.raises(ConfigError, match=r"unknown placeholder \{cid\}"):
        load_scenario(tmp_path, text)


def test_probe_rejects_a_name_its_own_step_captures(tmp_path: Path) -> None:
    text = scenario('probe = ["watch", "{cid}"]', "expect = 'client (?P<cid>\\w+)'")
    with pytest.raises(ConfigError, match=r"unknown placeholder \{cid\}"):
        load_scenario(tmp_path, text)


def test_a_capture_named_like_a_reserved_field_still_loads(tmp_path: Path) -> None:
    text = CAPTURING.replace("(?P<cid>", "(?P<port>").replace("{cid}", "{port}")
    cfg = load_scenario(tmp_path, text)
    assert cfg.scenarios[0].steps[1].probe == ("watch", "{port}")
