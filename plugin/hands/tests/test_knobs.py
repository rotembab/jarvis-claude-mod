"""The sensitivity knobs as the protocol's ``config`` command sets them.

The table, validation, status, the schema and the mapper's gain.

The defaults and the pinned ranges below are written out here on purpose (not read from ``settings.KNOBS``): they are
the contract with the mod. The helper's ranges may be narrower than the pinned ones where the measurements said a
range end is unusable; they never reach beyond.
"""

from __future__ import annotations

import json
import math
from typing import Any

import jsonschema
import numpy as np
import pytest

from jarvis_hands import protocol
from jarvis_hands.desktop.base import Display
from jarvis_hands.geometry import Point, Rect, apply_homography, box_homography
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.settings import KNOB_BY_KEY, KNOBS, HandsSettings

from scripted import display

#: wire key -> (python attribute, pinned minimum, pinned maximum, default)
CONTRACT: dict[str, tuple[str, float, float, float]] = {
    "cursorSpeed": ("cursor_speed", 0.5, 3.0, 1.0),
    "smoothing": ("smoothing", 0.2, 5.0, 1.0),
    "pinch": ("pinch_sensitivity", 0.6, 1.6, 1.0),
    "fist": ("fist_sensitivity", 0.8, 1.2, 1.0),
    "engageSeconds": ("engage_s", 0.1, 2.0, 0.5),
    "dragDistance": ("drag_distance", 0.3, 4.0, 1.0),
    "flingSensitivity": ("fling_sensitivity", 0.5, 3.0, 1.0),
    "scrollSpeed": ("scroll_speed", 0.1, 10.0, 1.0),
    "deadZone": ("dead_zone_px", 0.0, 8.0, 1.0),
}
KEYS = list(CONTRACT)


def test_the_table_is_the_contracts_keys_attributes_and_defaults() -> None:
    assert [k.key for k in KNOBS] == KEYS
    for knob in KNOBS:
        attribute, low, high, default = CONTRACT[knob.key]
        assert knob.attr == attribute and knob.default == default
        assert low <= knob.low < knob.high <= high  # at most narrowed
        assert knob.low <= knob.default <= knob.high
    assert {k.key: k for k in KNOBS} == KNOB_BY_KEY


def test_a_fresh_settings_object_holds_the_defaults_and_reports_them() -> None:
    settings = HandsSettings()
    for attribute, _, _, default in CONTRACT.values():
        assert getattr(settings, attribute) == default
    status = settings.status()
    assert {key: status[key] for key in KEYS} == {key: c[3] for key, c in CONTRACT.items()}
    assert [key for key in status if key in KEYS] == KEYS  # reported in the table's order
    assert {"engage", "hand", "anchor", "overlay"} <= set(status)


@pytest.mark.parametrize("key", KEYS)
def test_the_range_ends_and_the_middle_are_accepted(key: str) -> None:
    knob = KNOB_BY_KEY[key]
    for value in (knob.low, knob.high, (knob.low + knob.high) / 2, knob.default):
        settings = HandsSettings()
        settings.apply_config({key: value})
        assert getattr(settings, knob.attr) == value
        assert isinstance(getattr(settings, knob.attr), float)
        assert settings.status()[key] == value


@pytest.mark.parametrize("key", KEYS)
def test_an_integer_is_a_number(key: str) -> None:
    knob = KNOB_BY_KEY[key]
    value = math.ceil(knob.low) if math.ceil(knob.low) <= knob.high else knob.high
    settings = HandsSettings()
    settings.apply_config({key: int(value)})
    assert getattr(settings, knob.attr) == value and isinstance(getattr(settings, knob.attr), float)


@pytest.mark.parametrize("key", KEYS)
def test_just_outside_the_range_is_refused_naming_the_key_and_the_range(key: str) -> None:
    knob = KNOB_BY_KEY[key]
    for value in (
        math.nextafter(knob.low, -math.inf),
        math.nextafter(knob.high, math.inf),
        knob.low - 1,
        knob.high + 1,
    ):
        settings = HandsSettings()
        with pytest.raises(ValueError) as caught:
            settings.apply_config({key: value})
        message = str(caught.value)
        assert key in message and f"{knob.low:g}" in message and f"{knob.high:g}" in message
        assert settings == HandsSettings()


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "bad",
    [True, False, "1", "", None, [], [1], {}, {"v": 1}, float("nan"), float("inf"), float("-inf"), 10**400, -(10**400)],
    ids=repr,
)
def test_what_is_not_a_finite_number_in_range_is_refused(key: str, bad: Any) -> None:
    settings = HandsSettings()
    with pytest.raises(ValueError, match=key):
        settings.apply_config({key: bad})
    assert settings == HandsSettings()


def test_a_command_with_one_bad_knob_applies_nothing() -> None:
    """Atomic: the good knobs and the other settings in the same body wait for the bad one."""
    good = {k.key: k.high for k in KNOBS if k.key != "deadZone"} | {
        "engage": "always",
        "hand": "left",
        "overlay": False,
    }
    for bad_key, bad in (("deadZone", 99), ("deadZone", float("nan")), ("deadZone", True), ("hand", "both")):
        settings = HandsSettings()
        with pytest.raises(ValueError):
            settings.apply_config({**good, bad_key: bad})
        assert settings == HandsSettings()
    settings = HandsSettings()
    settings.apply_config({**good, "deadZone": 8})
    assert (
        settings.dead_zone_px == 8.0
        and settings.engage == "always"
        and settings.pinch_sensitivity == KNOB_BY_KEY["pinch"].high
    )


def test_an_absent_knob_keeps_its_value_and_a_second_command_adds_to_the_first() -> None:
    settings = HandsSettings()
    settings.apply_config({"cursorSpeed": 2.0, "smoothing": 1.5})
    settings.apply_config({"deadZone": 3})
    assert (settings.cursor_speed, settings.smoothing, settings.dead_zone_px) == (2.0, 1.5, 3.0)
    settings.apply_config({})
    assert (settings.cursor_speed, settings.smoothing, settings.dead_zone_px) == (2.0, 1.5, 3.0)


def test_the_protocols_checks_and_the_settings_checks_agree_on_every_value() -> None:
    """The control server checks with ``protocol.validate_command`` and the settings check again: one range for both."""
    for knob in KNOBS:
        values = [
            knob.low,
            knob.high,
            knob.default,
            knob.low - 0.01,
            knob.high + 0.01,
            True,
            "1",
            None,
            float("nan"),
            float("inf"),
        ]
        for value in values:
            try:
                protocol.validate_command("config", {knob.key: value})
                by_protocol = True
            except protocol.ValidationError:
                by_protocol = False
            try:
                HandsSettings().apply_config({knob.key: value})
                by_settings = True
            except ValueError:
                by_settings = False
            assert by_protocol == by_settings, (knob.key, value)


# --------------------------------------------------------------------------- the schema


def schema() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(protocol.schema_path().read_text(encoding="utf-8"))
    return document


def test_the_schema_lists_every_knob_with_the_helpers_range() -> None:
    properties = schema()["$defs"]["ConfigCommand"]["properties"]
    for knob in KNOBS:
        entry = properties[knob.key]
        assert entry["type"] == "number"
        assert (entry["minimum"], entry["maximum"]) == (knob.low, knob.high)
        assert entry["description"]


def test_the_status_schema_requires_every_knob_and_the_settings_object_validates() -> None:
    document = schema()
    status = document["$defs"]["StatusResponse"]["properties"]["settings"]
    assert set(KEYS) <= set(status["required"]) and set(KEYS) <= set(status["properties"])
    assert all(status["properties"][key] == {"type": "number"} for key in KEYS)
    validator = jsonschema.Draft202012Validator({**status, "$defs": document["$defs"]})
    validator.validate(HandsSettings().status())
    settings = HandsSettings()
    settings.apply_config({k.key: k.high for k in KNOBS})
    validator.validate(settings.status())
    missing = HandsSettings().status()
    del missing["pinch"]
    assert not validator.is_valid(missing)


def test_the_config_schema_refuses_what_the_helper_refuses() -> None:
    document = schema()
    validator = jsonschema.Draft202012Validator({"$ref": "#/$defs/ConfigCommand", "$defs": document["$defs"]})
    for knob in KNOBS:
        assert validator.is_valid({knob.key: knob.low}) and validator.is_valid({knob.key: knob.high})
        assert not validator.is_valid({knob.key: knob.low - 0.01}) and not validator.is_valid(
            {knob.key: knob.high + 0.01}
        )
        assert not validator.is_valid({knob.key: True}) and not validator.is_valid({knob.key: "1"})
    assert not validator.is_valid({"speed": 1})


# --------------------------------------------------------------------------- the mapper's gain

DESK = [display(1, 0, 0, 1920, 1080, primary=True)]
TWO = [display(1, 0, 0, 1920, 1080, primary=True), display(2, 1920, 0, 1280, 720)]
POINTS = [
    Point(x, y)
    for x in (-0.1, 0.05, 0.2, 0.35, 0.5, 0.63, 0.8, 0.97, 1.2)
    for y in (-0.2, 0.1, 0.2, 0.45, 0.7, 0.9, 1.1)
]


def mapper(displays: list[Display] = DESK, **config: float) -> ScreenMapper:
    settings = HandsSettings()
    settings.apply_config(config)
    return ScreenMapper(displays, settings)


def test_a_gain_of_one_changes_nothing_to_the_last_bit() -> None:
    plain, set_to_one = mapper(), mapper()
    set_to_one.set_cursor_gain(1.0)
    box = box_homography(0.2, 0.2, 0.8, 0.7)
    for p in POINTS:
        assert plain.to_desktop(p) == set_to_one.to_desktop(p)
        assert plain.to_target(p) == apply_homography(box, p)
        uv = apply_homography(box, p)
        assert plain.to_desktop(p) == plain.target_point(uv.x, uv.y)


def test_the_gain_scales_around_the_centre_of_the_target() -> None:
    for gain in (0.7, 0.85, 1.0, 1.6, 2.0, 3.0):
        m = mapper()
        m.set_cursor_gain(gain)
        assert m.cursor_gain == gain
        box = box_homography(0.2, 0.2, 0.8, 0.7)
        for p in POINTS:
            uv = apply_homography(box, p)
            gained = m.to_target(p)
            assert gained.x == pytest.approx(0.5 + (uv.x - 0.5) * gain) and gained.y == pytest.approx(
                0.5 + (uv.y - 0.5) * gain
            )
        assert m.to_target(Point(0.5, 0.45)) == Point(pytest.approx(0.5), pytest.approx(0.5))
        assert m.to_desktop(Point(0.5, 0.45)) == Point(pytest.approx(960), pytest.approx(540))


def test_the_hand_travel_that_crosses_the_screen_is_divided_by_the_gain() -> None:
    for gain in (0.7, 1.0, 1.6, 2.0, 3.0):
        m = mapper(cursorSpeed=gain)
        travel = 0.6 / gain  # frame widths: the default box is 0.6 wide
        left, right = m.to_desktop(Point(0.5 - travel / 2, 0.45)), m.to_desktop(Point(0.5 + travel / 2, 0.45))
        assert left.x == pytest.approx(0, abs=1e-6) and right.x == pytest.approx(1919.0, abs=1.5)
        beyond = m.to_desktop(Point(0.5 + travel, 0.45))
        assert beyond.x == right.x  # past the edge the cursor stays on the screen


def test_a_faster_cursor_moves_further_for_the_same_hand_motion() -> None:
    moves = []
    for gain in (0.7, 1.0, 1.5, 2.0, 3.0):
        m = mapper(cursorSpeed=gain)
        moves.append(m.to_desktop(Point(0.55, 0.45)).x - m.to_desktop(Point(0.5, 0.45)).x)
    assert moves == sorted(moves) and moves[0] > 0
    assert moves[-1] / moves[1] == pytest.approx(3.0)


def test_the_gain_applies_after_a_calibrated_homography() -> None:
    calibrated = box_homography(0.1, 0.1, 0.9, 0.8)
    m = ScreenMapper(DESK, HandsSettings(), calibrated)
    m.set_cursor_gain(2.0)
    p = Point(0.7, 0.3)
    uv = apply_homography(calibrated, p)
    assert m.to_target(p) == Point(pytest.approx(0.5 + (uv.x - 0.5) * 2), pytest.approx(0.5 + (uv.y - 0.5) * 2))
    assert m.calibrated and np.allclose(m.homography, calibrated)  # the homography itself is not touched


def test_the_gain_centres_on_the_middle_of_all_the_chosen_displays() -> None:
    m = mapper(TWO, cursorSpeed=2.0)
    centre = m.region.center
    assert m.to_desktop(Point(0.5, 0.45)) == Point(pytest.approx(centre.x), pytest.approx(centre.y))
    one = mapper(TWO)
    assert m.to_desktop(Point(0.6, 0.45)).x - centre.x == pytest.approx(
        2 * (one.to_desktop(Point(0.6, 0.45)).x - centre.x)
    )


def test_scrolling_throwing_and_the_calibration_targets_do_not_take_the_gain() -> None:
    slow, fast = mapper(), mapper(cursorSpeed=3.0)
    for p in POINTS:
        assert slow.to_region(p) == fast.to_region(p)
    for u, v in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.3, 0.8)):
        assert slow.target_point(u, v) == fast.target_point(u, v)
    assert fast.target_point(0.0, 0.0) == Point(0, 0)  # the corner target is the corner, whatever the gain


def test_the_mapper_starts_from_the_settings_and_refuses_a_bad_gain() -> None:
    assert mapper(cursorSpeed=2.5).cursor_gain == 2.5
    assert mapper().cursor_gain == 1.0
    m = mapper()
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            m.set_cursor_gain(bad)
    assert m.cursor_gain == 1.0


def test_the_gain_is_set_from_another_thread_while_the_engine_maps() -> None:
    import threading

    m = mapper()
    stop = threading.Event()
    seen: list[Point] = []

    def reader() -> None:
        while not stop.is_set():
            seen.append(m.to_desktop(Point(0.6, 0.4)))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(2000):
            m.set_cursor_gain(1.0 + (i % 5) * 0.5)
    finally:
        stop.set()
        thread.join()
    desk = Rect(0, 0, 1920, 1080)
    assert seen and all(desk.contains(p) for p in seen)


def test_mod_rows_match_the_helper_knobs() -> None:
    """The mod's ``ROWS`` table (plugin/hooks/hands.ts) and the plugin.json settings quote the helper's ranges.

    The mod checks a value before it sends it so the user hears the range; a mod that allows more than the helper
    gets every change refused, and one that allows less hides a range the helper accepts.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    source = (root / "plugin" / "hooks" / "hands.ts").read_text(encoding="utf-8")
    rows = {
        m.group("key"): (float(m.group("min")), float(m.group("max")), float(m.group("default")))
        for m in re.finditer(
            r"\{ key: '(?P<key>\w+)', name: '[^']*', label: '[^']*', min: (?P<min>[\d.]+), max: (?P<max>[\d.]+), "
            r"default: (?P<default>[\d.]+)",
            source,
        )
    }
    assert set(rows) == {knob.key for knob in KNOBS}
    for knob in KNOBS:
        assert rows[knob.key] == (knob.low, knob.high, knob.default), knob.key

    config = json.loads((root / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["userConfig"]
    for setting, key in (
        ("handCursorSpeed", "cursorSpeed"),
        ("handSmoothing", "smoothing"),
        ("handPinch", "pinch"),
        ("handScrollSpeed", "scrollSpeed"),
    ):
        knob = KNOB_BY_KEY[key]
        text = config[setting]["description"]
        assert f"from {knob.low:g} to {knob.high:g}" in text, (setting, text)
