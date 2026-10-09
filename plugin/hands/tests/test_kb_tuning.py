"""``Tuning`` and ``load_tuning`` (DESIGN 3.2, 3.14): accuracy numbers only, clamped, and a file can never relax a rule.

U11 (clamps and floors, the pinch and plane fields; the air fields are X25's, here as well because one loader reads
both), S25 (fuzz), P8 (a corrupt, wrongly typed or oversized file gives the defaults and never raises).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import random
from pathlib import Path
from typing import Any

import pytest

from jarvis_hands.keyboard import limits, tuning
from jarvis_hands.keyboard.tuning import Tuning


def short_id(value: Any) -> str:
    """``repr`` for a test's id, cut when long: an id is also an environment value (PYTEST_CURRENT_TEST), and 10**400
    is 400 digits of every case's name."""
    text = repr(value)
    return text if len(text) <= 60 else f"{text[:24]}...[{len(text)} characters]"


# name: (default, low, high). An independent copy of the tables of 3.2; a change to the module fails here.
SCALARS: dict[str, tuple[Any, float, float]] = {
    "close": (0.28, 0.22, 0.36),
    "open": (0.40, 0.34, 0.50),
    "descent": (0.10, 0.06, 0.20),
    "descent_max_r": (0.80, 0.60, 1.00),
    "onset_window_s": (0.35, 0.20, 0.60),
    "recover": (0.05, 0.02, 0.10),
    "closing_timeout_s": (0.8, 0.40, 1.50),
    "margin": (0.10, 0.08, 0.25),
    "confirm_frames": (2, 2, 3),
    "curled_enter": (1.10, 1.00, 1.20),
    "curled_leave": (1.20, 1.05, 1.35),
    "others_delta": (0.20, 0.10, 0.40),
    "others_from_s": (0.18, 0.10, 0.20),
    "others_to_s": (0.30, 0.20, 0.40),
    "anchor_speed_max": (1.5, 1.0, 3.0),
    "aim_frames": (3, 2, 5),
    "warm_factor": (1.25, 1.10, 1.50),
    "pitch": (0.0495, 0.035, 0.075),
    "pitch_y_ratio": (1.25, 1.0, 1.6),
    "edge_tolerance": (0.35, 0.20, 0.50),
    "associate_radius": (0.25, 0.15, 0.35),
    "hand_hold_s": (0.20, 0.10, 0.30),
    "z_scale": (1.0, 0.3, 1.0),
    "still_speed": (0.15, 0.08, 0.30),
    "still_s": (0.6, 0.4, 1.5),
    "air_theta_k": (5.0, 4.0, 8.0),
    "air_theta_min": (0.10, 0.08, 0.20),
    "air_theta_max": (0.25, 0.18, 0.40),
    "air_depth_frac": (0.5, 0.4, 0.7),
    "air_back_s": (0.20, 0.15, 0.30),
    "air_rise_win_s": (0.16, 0.10, 0.25),
    "air_fall_win_s": (0.12, 0.08, 0.20),
    "air_return_frac": (0.5, 0.35, 0.65),
    "air_width_min_s": (0.04, 0.03, 0.08),
    "air_width_max_s": (0.30, 0.20, 0.45),
    "air_speed_gate": (0.5, 0.3, 1.0),
    "air_vmax_gate": (0.5, 0.3, 1.0),
    "air_veto_ratio": (0.7, 0.5, 0.85),
    "air_aim_speed": (0.05, 0.02, 0.15),
}
INT_FIELDS = {"confirm_frames", "aim_frames"}
LEVEL_PALM = (0.0, 0.10, 0.04, -0.15)
GROUPS = {
    "pinch": [
        "close",
        "open",
        "descent",
        "descent_max_r",
        "onset_window_s",
        "recover",
        "closing_timeout_s",
        "margin",
        "confirm_frames",
        "curled_enter",
        "curled_leave",
        "others_delta",
        "others_from_s",
        "others_to_s",
        "anchor_speed_max",
        "aim_frames",
        "warm_factor",
    ],
    "plane": ["pitch", "pitch_y_ratio", "edge_tolerance", "still_speed", "still_s"],
    "hands": ["level_palm", "associate_radius", "hand_hold_s", "z_scale"],
    "air": [name for name in SCALARS if name.startswith("air_")] + ["air_aim"],
}
GROUP_OF = {name: group for group, names in GROUPS.items() for name in names}
ALL_FIELDS = [*SCALARS, "level_palm", "air_aim"]


def value_at(name: str, x: float) -> Any:
    return int(x) if name in INT_FIELDS else x


def doc(**fields: Any) -> dict[str, Any]:
    """A tuning document with the given fields in their own groups."""
    out: dict[str, Any] = {"version": 1}
    for name, value in fields.items():
        out.setdefault(GROUP_OF[name], {})[name] = value
    return out


def parse(**fields: Any) -> Tuning:
    return tuning.parse_tuning(doc(**fields))


def is_valid(t: Tuning) -> list[str]:
    """What is wrong with a Tuning, by the independent tables above; empty when it is sound."""
    problems = []
    for name, (_, low, high) in SCALARS.items():
        value = getattr(t, name)
        want = int if name in INT_FIELDS else float
        if type(value) is not want:
            problems.append(f"{name}: type {type(value).__name__}")
        elif not (math.isfinite(value) and low - 1e-12 <= value <= high + 1e-12):
            problems.append(f"{name}: {value} outside {low}..{high}")
    if not (
        isinstance(t.level_palm, tuple)
        and len(t.level_palm) == 4
        and all(type(x) is float and -0.30 <= x <= 0.30 for x in t.level_palm)
    ):
        problems.append("level_palm")
    if t.air_aim not in ("auto", "onset", "commit"):
        problems.append("air_aim")
    if t.open < t.close + 0.06 - 1e-9:
        problems.append("open >= close + 0.06")
    if t.curled_leave < t.curled_enter + 0.05 - 1e-9:
        problems.append("curled_leave >= curled_enter + 0.05")
    if t.air_theta_min > t.air_theta_max:
        problems.append("air_theta_min <= air_theta_max")
    return problems


# --------------------------------------------------------------------------- the defaults and the clamps


def test_the_fields_are_exactly_the_tables() -> None:
    assert sorted(f.name for f in dataclasses.fields(Tuning)) == sorted(ALL_FIELDS)


@pytest.mark.parametrize("name", list(SCALARS))
def test_scalar_defaults_and_types(name: str) -> None:
    default = SCALARS[name][0]
    actual = getattr(Tuning(), name)
    assert actual == default
    assert type(actual) is (int if name in INT_FIELDS else float)


def test_vector_and_choice_defaults() -> None:
    t = Tuning()
    assert t.level_palm == LEVEL_PALM and type(t.level_palm) is tuple
    assert t.air_aim == "auto"
    assert is_valid(t) == []


def test_tuning_is_frozen() -> None:
    t = Tuning()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.margin = 0.0  # type: ignore[misc]
    assert dataclasses.replace(t, margin=0.2).margin == 0.2


def test_ranges_are_the_tables() -> None:
    assert set(tuning.RANGES) == set(SCALARS)
    for name, (_, low, high) in SCALARS.items():
        assert tuning.RANGES[name] == (low, high), name
    assert tuning.LEVEL_PALM_RANGE == (-0.30, 0.30)
    assert tuning.AIR_AIM_CHOICES == ("auto", "onset", "commit")


def test_every_default_lies_inside_its_clamp() -> None:
    for name, (default, low, high) in SCALARS.items():
        assert low <= default <= high, name


# Fields whose edges are tied to a partner field (a relation of 3.2): only the edges that hold against the partner's
# default.
EDGES_AGAINST_DEFAULT_PARTNER: dict[str, tuple[float, ...]] = {
    "close": (0.22, 0.30),  # open 0.40 needs close <= 0.34
    "curled_enter": (1.00, 1.10),  # leave 1.20 needs enter <= 1.15
    "curled_leave": (1.20, 1.35),  # enter 1.10 needs leave >= 1.15
}


@pytest.mark.parametrize("name", list(SCALARS))
def test_the_edges_are_accepted(name: str) -> None:
    _, low, high = SCALARS[name]
    for edge in EDGES_AGAINST_DEFAULT_PARTNER.get(name, (low, high, (low + high) / 2)):
        value = value_at(name, edge)
        got = getattr(parse(**{name: value}), name)
        assert got == pytest.approx(value), (name, edge)
        assert type(got) is (int if name in INT_FIELDS else float)


@pytest.mark.parametrize("name", list(SCALARS))
def test_just_outside_the_clamp_falls_back_to_the_default(name: str) -> None:
    default, low, high = SCALARS[name]
    step = 1 if name in INT_FIELDS else 1e-6
    for outside in (low - step, high + step, low - 1000, high + 1000):
        assert getattr(parse(**{name: outside}), name) == default, (name, outside)


@pytest.mark.parametrize("name", list(SCALARS))
@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        False,
        "0.2",
        "",
        "zzqxjv",
        [],
        [0.2],
        {},
        {"v": 0.2},
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
        -(10**400),
    ],
    ids=short_id,
)
def test_wrongly_typed_or_non_finite_values_fall_back(name: str, bad: Any) -> None:
    assert getattr(parse(**{name: bad}), name) == SCALARS[name][0]


@pytest.mark.parametrize("name", sorted(INT_FIELDS))
@pytest.mark.parametrize("bad", [2.5, 3.0, 2.0, "2", 1e9])
def test_integer_fields_take_integers_only(name: str, bad: Any) -> None:
    assert getattr(parse(**{name: bad}), name) == SCALARS[name][0]


# A float field also takes a JSON integer (a writer may print 1 for 1.0); the value is stored as a float.
WHOLE_NUMBER_INSIDE: dict[str, int] = {
    name: whole
    for name, (_, low, high) in SCALARS.items()
    if name not in INT_FIELDS
    for whole in range(math.ceil(low), math.floor(high) + 1)[:1]
}


@pytest.mark.parametrize(("name", "whole"), WHOLE_NUMBER_INSIDE.items())
def test_a_json_integer_is_a_valid_number_for_a_float_field(name: str, whole: int) -> None:
    got = getattr(parse(**{name: whole}), name)
    assert got == whole and type(got) is float


# --------------------------------------------------------------------------- the floors no file can cross (U11)


def test_the_false_press_defences_have_floors_in_the_clamps() -> None:
    assert SCALARS["margin"][1] >= 0.08 and tuning.RANGES["margin"][0] >= 0.08
    assert tuning.RANGES["confirm_frames"][0] >= 2
    assert tuning.RANGES["descent"][0] >= 0.06
    assert tuning.RANGES["anchor_speed_max"][1] <= 3.0
    assert tuning.RANGES["others_delta"][1] <= 0.40
    assert tuning.RANGES["air_theta_k"][0] >= limits.AIR_THETA_K_FLOOR
    assert tuning.RANGES["air_theta_min"][0] >= limits.AIR_THETA_FLOOR
    assert tuning.RANGES["air_veto_ratio"][0] >= limits.AIR_VETO_FLOOR


@pytest.mark.parametrize(
    ("name", "below_floor"),
    [
        ("margin", 0.05),
        ("margin", 0.0),
        ("confirm_frames", 1),
        ("confirm_frames", 0),
        ("descent", 0.02),
        ("anchor_speed_max", 10.0),
        ("others_delta", 0.9),
        ("air_theta_k", 3.0),
        ("air_theta_k", limits.AIR_THETA_K_FLOOR - 0.01),
        ("air_theta_min", 0.03),
        ("air_theta_min", limits.AIR_THETA_FLOOR - 0.01),
        ("air_veto_ratio", 0.2),
        ("air_veto_ratio", limits.AIR_VETO_FLOOR - 0.01),
    ],
)
def test_a_value_beyond_a_floor_falls_back_to_the_default_not_to_the_floor(name: str, below_floor: Any) -> None:
    """A file that tries to loosen a defence gets the shipped value, so a forged file never lands above the floor."""
    assert getattr(parse(**{name: below_floor}), name) == SCALARS[name][0]


# --------------------------------------------------------------------------- relations between two fields


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"close": 0.30, "open": 0.36}, {"close": 0.30, "open": 0.36}),  # exactly 0.06 apart
        ({"close": 0.22, "open": 0.34}, {"close": 0.22, "open": 0.34}),
        ({"close": 0.30, "open": 0.35}, {"close": 0.28, "open": 0.40}),  # 0.05 apart: both default
        ({"close": 0.36}, {"close": 0.28, "open": 0.40}),  # the default open is under 0.42: both default
        ({"open": 0.34}, {"close": 0.28, "open": 0.34}),  # 0.34 >= 0.28 + 0.06
        ({"curled_enter": 1.10, "curled_leave": 1.15}, {"curled_enter": 1.10, "curled_leave": 1.15}),
        ({"curled_enter": 1.20, "curled_leave": 1.22}, {"curled_enter": 1.10, "curled_leave": 1.20}),
        ({"curled_enter": 1.20}, {"curled_enter": 1.10, "curled_leave": 1.20}),  # the default leave is under 1.25
        ({"curled_leave": 1.05}, {"curled_enter": 1.10, "curled_leave": 1.20}),  # under the default enter + 0.05
        ({"air_theta_min": 0.20, "air_theta_max": 0.18}, {"air_theta_min": 0.10, "air_theta_max": 0.25}),
        ({"air_theta_min": 0.18, "air_theta_max": 0.18}, {"air_theta_min": 0.18, "air_theta_max": 0.18}),
    ],
)
def test_relations_between_fields(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    got = parse(**fields)
    for name, value in expected.items():
        assert getattr(got, name) == pytest.approx(value), name
    assert is_valid(got) == []


# --------------------------------------------------------------------------- level_palm and air_aim


def test_level_palm() -> None:
    assert parse(level_palm=[0.0, 0.1, -0.3, 0.3]).level_palm == (0.0, 0.1, -0.3, 0.3)
    assert parse(level_palm=[0, 0, 0, 0]).level_palm == (0.0, 0.0, 0.0, 0.0)
    assert all(type(x) is float for x in parse(level_palm=[0, 0, 0, 0]).level_palm)
    for bad in (
        [0.0, 0.1, 0.04],
        [0.0, 0.1, 0.04, -0.15, 0.0],
        [0.0, 0.1, 0.04, 0.31],
        [0.0, 0.1, 0.04, -0.31],
        [0.0, 0.1, 0.04, float("nan")],
        [0.0, 0.1, 0.04, True],
        [0.0, 0.1, 0.04, "0"],
        [0.0, 0.1, 0.04, None],
        [[0.0], 0.1, 0.04, 0.0],
        0.0,
        "0,0.1,0.04,-0.15",
        {"a": 1},
        None,
        10**400,
        [0.0, 0.1, 0.04, 10**400],
    ):
        assert parse(level_palm=bad).level_palm == LEVEL_PALM, bad


@pytest.mark.parametrize("choice", ["auto", "onset", "commit"])
def test_air_aim_accepts_its_three_names(choice: str) -> None:
    assert parse(air_aim=choice).air_aim == choice


@pytest.mark.parametrize("bad", ["peak", "AUTO", "", " auto", "auto ", None, 1, True, ["auto"], {"auto": 1}, "zzqxjv"])
def test_air_aim_refuses_anything_else(bad: Any) -> None:
    assert parse(air_aim=bad).air_aim == "auto"


# --------------------------------------------------------------------------- the file's layout


def test_wire_names() -> None:
    assert tuning.wire_name("close") == "close"
    assert tuning.wire_name("pitch_y_ratio") == "pitchYRatio"
    assert tuning.wire_name("air_theta_k") == "airThetaK"
    assert tuning.wire_name("descent_max_r") == "descentMaxR"


def test_groups_are_the_layout_of_the_file() -> None:
    assert {g: sorted(names) for g, names in tuning.GROUPS.items()} == {g: sorted(n) for g, n in GROUPS.items()}
    flat = [n for names in tuning.GROUPS.values() for n in names]
    assert len(flat) == len(set(flat)) == len(ALL_FIELDS) and set(flat) == set(ALL_FIELDS)


def test_the_example_file_of_the_design_loads() -> None:
    document = {
        "version": 1,
        "pinch": {"close": 0.30, "margin": 0.12},
        "plane": {"pitch": 0.05, "pitchYRatio": 1.3},
        "hands": {"level_palm": [0.0, 0.1, 0.04, -0.15]},
        "air": {"air_theta_k": 5.5, "airAim": "commit"},
    }
    t = tuning.parse_tuning(document)
    assert (t.close, t.margin, t.pitch, t.pitch_y_ratio, t.air_theta_k, t.air_aim) == (
        0.30,
        0.12,
        0.05,
        1.3,
        5.5,
        "commit",
    )
    assert is_valid(t) == []


def test_snake_case_and_camel_case_name_the_same_field() -> None:
    assert tuning.parse_tuning({"plane": {"pitch_y_ratio": 1.4}}).pitch_y_ratio == 1.4
    assert tuning.parse_tuning({"plane": {"pitchYRatio": 1.4}}).pitch_y_ratio == 1.4


def test_a_field_belongs_to_one_group_only() -> None:
    """A name in the wrong group is an unknown key: ignored."""
    assert tuning.parse_tuning({"air": {"margin": 0.2}}).margin == 0.10
    assert tuning.parse_tuning({"pinch": {"air_theta_k": 6.0}}).air_theta_k == 5.0
    assert tuning.parse_tuning({"margin": 0.2}).margin == 0.10  # a flat file is not the layout either


def test_unknown_keys_and_groups_are_ignored() -> None:
    t = tuning.parse_tuning(
        {"version": 1, "pinch": {"margin": 0.2, "nope": 1}, "extra": {"x": 1}, "air": 5, "plane": []}
    )
    assert t.margin == 0.2
    assert dataclasses.replace(t, margin=Tuning().margin) == Tuning()


@pytest.mark.parametrize("group", ["pinch", "plane", "hands", "air"])
@pytest.mark.parametrize("bad", [None, 5, "x", [], [1], True, 1.5])
def test_a_group_that_is_not_an_object_is_ignored(group: str, bad: Any) -> None:
    assert tuning.parse_tuning({group: bad}) == Tuning()


@pytest.mark.parametrize("bad", [None, 5, "x", [], [{"pinch": {"margin": 0.2}}], True, 1.5])
def test_a_document_that_is_not_an_object_gives_the_defaults(bad: Any) -> None:
    assert tuning.parse_tuning(bad) == Tuning()


# --------------------------------------------------------------------------- the file: P8


def write(tmp_path: Path, data: bytes | str) -> Path:
    path = tuning.tuning_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def test_the_path_is_under_the_hands_data_dir(tmp_path: Path) -> None:
    assert tuning.tuning_path(tmp_path) == tmp_path / "hands" / "keyboard-tuning.json"


def test_no_file_gives_the_defaults_quietly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert tuning.load_tuning(tmp_path) == Tuning()
        assert tuning.load_tuning(tmp_path / "does" / "not" / "exist") == Tuning()
    assert caplog.records == []  # the normal case writes no line


def test_a_valid_file_changes_the_values_it_names(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    write(tmp_path, json.dumps({"version": 1, "pinch": {"margin": 0.15}, "air": {"air_theta_k": 6.0}}))
    with caplog.at_level(logging.DEBUG):
        t = tuning.load_tuning(tmp_path)
    assert (t.margin, t.air_theta_k) == (0.15, 6.0)
    assert dataclasses.replace(t, margin=0.10, air_theta_k=5.0) == Tuning()
    assert caplog.records == []


def test_a_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    write(tmp_path, b"\xef\xbb\xbf" + json.dumps({"pinch": {"margin": 0.15}}).encode())
    assert tuning.load_tuning(tmp_path).margin == 0.15


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"{",
        b'{"pinch": {"margin": 0.2}',  # truncated
        b"\xff\xfe\x00garbage",
        b"[]",
        b'"text"',
        b"5",
        b"null",
        b'{"version": 2, "pinch": {"margin": 0.2}}',  # a version this code does not know
        b'{"version": "1", "pinch": {"margin": 0.2}}',
        b'{"version": true, "pinch": {"margin": 0.2}}',
        b'{"pinch": {"margin": NaN}}',
        b"[" * 20000,  # deep enough to overflow the parser's recursion
        b'{"a":' * 20000,
    ],
    ids=lambda b: repr(b)[:30],
)
def test_a_corrupt_or_unknown_file_gives_the_defaults_with_one_line(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    write(tmp_path, content)
    with caplog.at_level(logging.DEBUG):
        t = tuning.load_tuning(tmp_path)
    assert t == Tuning()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_an_oversized_file_is_not_read(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    padding = " " * (tuning.MAX_FILE_BYTES + 1)
    write(tmp_path, json.dumps({"pinch": {"margin": 0.2}}) + padding)
    with caplog.at_level(logging.DEBUG):
        assert tuning.load_tuning(tmp_path) == Tuning()
    assert len(caplog.records) == 1


def test_a_file_just_inside_the_size_limit_is_read(tmp_path: Path) -> None:
    body = json.dumps({"pinch": {"margin": 0.2}})
    write(tmp_path, body + " " * (tuning.MAX_FILE_BYTES - len(body)))
    assert tuning.load_tuning(tmp_path).margin == 0.2


def test_the_limit_is_small(tmp_path: Path) -> None:
    assert tuning.MAX_FILE_BYTES <= 1 << 20  # a tuning file is a few hundred bytes


def test_a_directory_in_the_files_place_gives_the_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    tuning.tuning_path(tmp_path).mkdir(parents=True)
    with caplog.at_level(logging.DEBUG):
        assert tuning.load_tuning(tmp_path) == Tuning()
    assert len(caplog.records) == 1


def test_one_line_however_many_values_fell_back(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    write(tmp_path, json.dumps({"pinch": {"margin": 0.01, "descent": 9, "close": "x"}, "air": {"air_theta_k": 1}}))
    with caplog.at_level(logging.DEBUG):
        t = tuning.load_tuning(tmp_path)
    assert t == Tuning()
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "margin" in line  # the line names the fields (they are ours, not the file's)


def test_loading_writes_nothing(tmp_path: Path) -> None:
    write(tmp_path, b"corrupt")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    tuning.load_tuning(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
    assert tuning.tuning_path(tmp_path).read_bytes() == b"corrupt"


def test_the_log_line_never_holds_what_the_file_said(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Exception text and file content are data too (F4): the line names our own fields and a fixed reason."""
    sentinel = "zzqxjv"
    for content in (
        json.dumps({"pinch": {"margin": sentinel}, sentinel: 1, "air": {"air_aim": sentinel}}),
        '{"pinch": {"margin": "' + sentinel + '"',  # corrupt, with the sentinel inside the broken part
        sentinel,
        json.dumps({"version": sentinel}),
    ):
        write(tmp_path, content)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            tuning.load_tuning(tmp_path)
        assert sentinel not in caplog.text
        assert all(r.exc_info is None for r in caplog.records)  # no traceback carries the text either
        assert all(sentinel not in str(r.args) for r in caplog.records)


IGNORED = "keyboard tuning file ignored: {}; the built-in values are used"
BUILT_IN = "keyboard tuning: the built-in value is used for: {}"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", tuning._NOT_JSON),
        (b"not json", tuning._NOT_JSON),
        (b'{"pinch": {"margin": 0.2}', tuning._NOT_JSON),
        (b"\xff\xfe\x00garbage", tuning._NOT_JSON),  # the decoder's own text quotes the byte and its place
        (b'{"a": "zzqxjv\x01"}', tuning._NOT_JSON),  # the parser's own text quotes the place
        (b"[" * 20000, tuning._NOT_JSON),
        (b"[]", tuning._NOT_AN_OBJECT),
        (b'"zzqxjv"', tuning._NOT_AN_OBJECT),
        (b"5", tuning._NOT_AN_OBJECT),
        (b'{"version": 2}', tuning._UNKNOWN_VERSION),
        (b'{"version": "zzqxjv"}', tuning._UNKNOWN_VERSION),
        (b'{"version": true}', tuning._UNKNOWN_VERSION),
    ],
    ids=lambda v: repr(v)[:30],
)
def test_a_file_that_is_set_aside_says_one_fixed_reason(
    tmp_path: Path, content: bytes, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason is a word of ours: not the parser's text, which quotes the file's bytes and where they are (F4)."""
    write(tmp_path, content)
    with caplog.at_level(logging.DEBUG):
        assert tuning.load_tuning(tmp_path) == Tuning()
    assert [r.getMessage() for r in caplog.records] == [IGNORED.format(reason)]
    assert "%" not in reason and "{" not in reason  # a fixed word, nothing to fill in


def test_the_other_reasons_for_setting_a_file_aside_are_fixed_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "zzqxjv"

    def says(expected: str) -> None:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            assert tuning.load_tuning(tmp_path) == Tuning()
        assert [r.getMessage() for r in caplog.records] == [IGNORED.format(expected)]
        assert sentinel not in caplog.text and all(r.exc_info is None for r in caplog.records)

    # a directory where the file should be is "not a file", not an unreadable one
    tuning.tuning_path(tmp_path).mkdir(parents=True)
    says(tuning._NOT_A_FILE)
    tuning.tuning_path(tmp_path).rmdir()

    write(tmp_path, json.dumps({"pinch": {"margin": 0.2}}))

    def refuse(self: Path) -> bytes:
        raise PermissionError(f"cannot read {sentinel}")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    says(tuning._UNREADABLE)

    def grown(self: Path) -> bytes:  # the file grew between the size check and the read
        return b" " * (tuning.MAX_FILE_BYTES + 1)

    monkeypatch.setattr(Path, "read_bytes", grown)
    says(tuning._TOO_LARGE)
    monkeypatch.undo()
    write(tmp_path, json.dumps({"pinch": {"margin": 0.2}}) + " " * (tuning.MAX_FILE_BYTES + 1))
    says(tuning._TOO_LARGE)


def test_the_line_for_values_that_fell_back_names_only_our_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "zzqxjv"
    write(
        tmp_path,
        json.dumps(
            {"pinch": {"margin": 0.01, "descent": 9, "close": sentinel}, "air": {"air_theta_k": 1}, sentinel: 1}
        ),
    )
    with caplog.at_level(logging.DEBUG):
        assert tuning.load_tuning(tmp_path) == Tuning()
    assert [r.getMessage() for r in caplog.records] == [BUILT_IN.format("close, descent, margin, air_theta_k")]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# --------------------------------------------------------------------------- S25: fuzz


SAFETY_NAMES = sorted(name for name in vars(limits) if name.isupper())


def random_json(rng: random.Random, depth: int = 0) -> Any:
    kinds = ["int", "float", "str", "bool", "null", "list", "dict"] if depth < 3 else ["int", "float", "str", "null"]
    kind = rng.choice(kinds)
    if kind == "int":
        return rng.choice([0, 1, -1, 2, 3, 5, 10**6, -(10**6), 10**400, -(10**400), rng.randint(-5, 5)])
    if kind == "float":
        return rng.choice([0.0, 0.1, 0.25, 0.5, 1.0, 1.5, -0.1, 1e300, -1e300, 1e-300, rng.uniform(-1, 2), 0.06, 0.08])
    if kind == "str":
        return rng.choice(["", "auto", "onset", "commit", "x", "zzqxjv", "NaN", "inf", "1"])
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "null":
        return None
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {rng.choice(KEYS): random_json(rng, depth + 1) for _ in range(rng.randint(0, 6))}


KEYS = [
    *ALL_FIELDS,
    *(tuning.wire_name(n) for n in ALL_FIELDS),
    *GROUPS,
    "version",
    "",
    "x",
    "__proto__",
    *SAFETY_NAMES,
    *(n.lower() for n in SAFETY_NAMES),
]


def targeted_document(rng: random.Random) -> dict[str, Any]:
    """Mostly sensible structure with hostile values, so that the fuzz reaches the clamps and the relations."""
    document: dict[str, Any] = {"version": rng.choice([1, 1, 1, 1, 1, 2, None, "1"])}
    for name in rng.sample(ALL_FIELDS, rng.randint(1, 12)):
        value = plausible(rng, name) if rng.random() < 0.5 else random_json(rng, 2)
        document.setdefault(GROUP_OF[name], {})[rng.choice([name, tuning.wire_name(name)])] = value
    return document


def plausible(rng: random.Random, name: str) -> Any:
    """A value near the field's clamp: inside, on an edge or a hair outside, so the fuzz reaches accepted values too."""
    if name == "air_aim":
        return rng.choice(["auto", "onset", "commit", "peak"])
    if name == "level_palm":
        return [rng.uniform(-0.35, 0.35) for _ in range(4)]
    _, low, high = SCALARS[name]
    pick = rng.choice([low, high, rng.uniform(low, high), rng.uniform(low, high), low - 1e-3, high + 1e-3])
    return round(pick) if name in INT_FIELDS else pick


def test_fuzz_never_raises_and_always_yields_a_sound_tuning() -> None:
    rng = random.Random(20261008)
    safety_before = {name: getattr(limits, name) for name in SAFETY_NAMES}
    changed = 0
    for i in range(4000):
        document = random_json(rng) if i % 3 == 0 else targeted_document(rng)
        result = tuning.parse_tuning(document)
        assert is_valid(result) == [], (document, is_valid(result))
        changed += result != Tuning()
    assert changed > 200  # the fuzz reaches accepted values as well as rejected ones
    assert {name: getattr(limits, name) for name in SAFETY_NAMES} == safety_before


def test_fuzz_through_the_file_loader(tmp_path: Path) -> None:
    rng = random.Random(7)
    for _ in range(200):
        write(tmp_path, json.dumps(targeted_document(rng)))
        assert is_valid(tuning.load_tuning(tmp_path)) == []


@pytest.mark.parametrize("name", SAFETY_NAMES)
def test_no_safety_constant_is_representable_in_the_file(name: str) -> None:
    """S25: a key named after a safety constant is an unknown key, wherever the file puts it."""
    assert name.lower() not in {f.name for f in dataclasses.fields(Tuning)}
    before = getattr(limits, name)
    for document in (
        {name: 1, name.lower(): 1},
        {"pinch": {name: 1, name.lower(): 1}},
        {"air": {name: 1, name.lower(): 1, tuning.wire_name(name.lower()): 1}},
        {"plane": {name: 0, name.lower(): 0}, "hands": {name: 0}},
    ):
        assert tuning.parse_tuning(document) == Tuning()
    assert getattr(limits, name) == before


def test_a_hostile_file_cannot_change_anything_but_accuracy_numbers() -> None:
    document = {
        "version": 1,
        "pinch": {"margin": 0.0, "confirm_frames": 0, "descent": 0.0, "anchor_speed_max": 99, "others_delta": 99},
        "air": {"air_theta_k": 0, "air_theta_min": 0, "air_veto_ratio": 0, "BACKSTOP_N": 10**6},
    }
    assert tuning.parse_tuning(document) == Tuning()
