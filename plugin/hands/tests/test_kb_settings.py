"""``KeyboardSettings`` (DESIGN 3.10): the defaults, the ranges, and ``apply`` as an all-or-nothing change.

U12 (apply is all-or-nothing), S26 (fuzz), and the agreement with the protocol's hand-written validator for
``configure`` (the same table twice must not drift).
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import Any

import pytest

from jarvis_hands import protocol
from jarvis_hands.keyboard.settings import KeyboardSettings

AIR_NEEDS_REVIEW = "The air-tap method only works with the review box (commit: review)."

DEFAULTS = {
    "enabled": False,
    "press": "air",
    "commit": "review",
    "layout": "auto",
    "size": 1.0,
    "reach": 1.0,
    "dock": "top",
    "idle_s": 30,
    "inject": "unicode",
    "enter": "twice",
}
WIRE_TO_FIELD = {
    "enabled": "enabled",
    "press": "press",
    "commit": "commit",
    "layout": "layout",
    "size": "size",
    "reach": "reach",
    "dock": "dock",
    "idleS": "idle_s",
    "inject": "inject",
    "enter": "enter",
}


def state(settings: KeyboardSettings) -> dict[str, Any]:
    return dataclasses.asdict(settings)


def test_defaults() -> None:
    s = KeyboardSettings()
    assert state(s) == DEFAULTS
    assert type(s.size) is float and type(s.reach) is float and type(s.idle_s) is int


def test_the_first_version_defaults_that_changed() -> None:
    """`press` is `air` (it was `pinch`) and `commit` is `review`: the air tap needs the review box."""
    s = KeyboardSettings()
    assert (s.press, s.commit, s.enabled) == ("air", "review", False)


def test_the_wire_keys() -> None:
    from jarvis_hands.keyboard import settings as module

    assert dict(module.WIRE_KEYS) == WIRE_TO_FIELD


@pytest.mark.parametrize(
    ("body", "changed"),
    [
        ({"enabled": True}, {"enabled": True}),
        ({"press": "pinch"}, {"press": "pinch"}),
        ({"press": "windows"}, {"press": "windows"}),
        ({"commit": "review"}, {}),
        ({"layout": "he"}, {"layout": "he"}),
        ({"layout": "en"}, {"layout": "en"}),
        ({"size": 0.6}, {"size": 0.6}),
        ({"size": 1.6}, {"size": 1.6}),
        ({"size": 1}, {"size": 1.0}),
        ({"reach": 0.8}, {"reach": 0.8}),
        ({"reach": 1.5}, {"reach": 1.5}),
        ({"dock": "bottom"}, {"dock": "bottom"}),
        ({"idleS": 5}, {"idle_s": 5}),
        ({"idleS": 300}, {"idle_s": 300}),
        ({"idleS": 30.0}, {"idle_s": 30}),  # 30.0 is an integer in JSON Schema, like the protocol validator
        ({"inject": "vk"}, {"inject": "vk"}),
        ({"enter": "off"}, {"enter": "off"}),
        ({}, {}),
        (
            {"press": "pinch", "commit": "direct", "layout": "en", "size": 1.2, "dock": "bottom"},
            {"press": "pinch", "commit": "direct", "layout": "en", "size": 1.2, "dock": "bottom"},
        ),
    ],
    ids=repr,
)
def test_a_valid_body_changes_exactly_what_it_names(body: dict[str, Any], changed: dict[str, Any]) -> None:
    s = KeyboardSettings()
    assert s.apply(body) is None
    assert state(s) == {**DEFAULTS, **changed}
    assert type(s.size) is float and type(s.reach) is float and type(s.idle_s) is int


def test_apply_is_incremental() -> None:
    s = KeyboardSettings()
    s.apply({"enabled": True})
    s.apply({"size": 1.3})
    s.apply({"idleS": 60})
    assert state(s) == {**DEFAULTS, "enabled": True, "size": 1.3, "idle_s": 60}


BAD_BODIES: list[Any] = [
    {"enabled": 1},
    {"enabled": "true"},
    {"enabled": None},
    {"press": "Air"},
    {"press": "osk"},
    {"press": ""},
    {"press": None},
    {"press": ["air"]},
    {"press": {"air": 1}},
    {"press": 1},
    {"commit": "auto"},
    {"commit": "Review"},
    {"commit": None},
    {"layout": "fr"},
    {"layout": "EN"},
    {"size": 0.59},
    {"size": 1.61},
    {"size": 0},
    {"size": -1},
    {"size": True},
    {"size": "1"},
    {"size": None},
    {"size": [1.0]},
    {"size": float("nan")},
    {"size": float("inf")},
    {"size": float("-inf")},
    {"size": 10**400},
    {"size": -(10**400)},
    {"reach": 0.79},
    {"reach": 1.51},
    {"reach": False},
    {"reach": "1"},
    {"reach": float("nan")},
    {"dock": "left"},
    {"dock": 0},
    {"idleS": 4},
    {"idleS": 301},
    {"idleS": 30.5},
    {"idleS": True},
    {"idleS": False},
    {"idleS": "30"},
    {"idleS": None},
    {"idleS": float("nan")},
    {"idleS": float("inf")},
    {"idleS": 10**400},
    {"inject": "sendkeys"},
    {"inject": "both"},
    {"enter": "once"},
    {"enter": True},
    {"idle_s": 30},  # the wire name is idleS
    {"Enabled": True},
    {"text": "x"},
    {"insert": True},
    {"decoder": "off"},  # the follow-on decoder's key does not exist in step 1
    {"enabled": True, "extra": 1},
    {"enabled": True, "size": 99},  # one bad value rejects the whole body
    {"press": "pinch", "commit": "nope"},
    {1: "x"},
    {None: 1},
    {("a",): 1},
    [],
    [("enabled", True)],
    "enabled",
    None,
    5,
    True,
]


@pytest.mark.parametrize("body", BAD_BODIES, ids=repr)
def test_a_bad_body_raises_value_error_and_changes_nothing(body: Any) -> None:
    s = KeyboardSettings()
    s.apply({"enabled": True, "size": 1.25, "press": "pinch"})
    before = state(s)
    with pytest.raises(ValueError):
        s.apply(body)
    assert state(s) == before


def test_a_late_failure_does_not_leave_the_early_changes_behind() -> None:
    """All-or-nothing: the valid keys before the bad one are not applied (dict order is the worst case for a loop)."""
    s = KeyboardSettings()
    body = {"enabled": True, "size": 1.4, "reach": 1.2, "dock": "bottom", "idleS": 90, "enter": "off", "inject": "vk"}
    for bad_key, bad_value in (("layout", "xx"), ("press", "nope"), ("idleS", 2), ("commit", 3)):
        s.apply({"enabled": False})
        before = state(s)
        with pytest.raises(ValueError):
            s.apply({**body, bad_key: bad_value})
        assert state(s) == before


# --------------------------------------------------------------------------- air needs review (SR29)


def test_air_with_direct_is_refused_with_the_fixed_text() -> None:
    s = KeyboardSettings()
    with pytest.raises(ValueError) as caught:
        s.apply({"commit": "direct"})  # press is already air
    assert str(caught.value) == AIR_NEEDS_REVIEW
    assert state(s) == DEFAULTS


@pytest.mark.parametrize(
    ("start", "body", "ok"),
    [
        ({}, {"commit": "direct"}, False),  # air + direct
        ({}, {"press": "air", "commit": "direct"}, False),
        ({}, {"press": "pinch", "commit": "direct"}, True),  # the switch in ONE body, as the mod sends it
        ({}, {"press": "windows", "commit": "direct"}, True),  # commit does not apply to windows
        ({"press": "pinch", "commit": "direct"}, {"press": "air"}, False),
        ({"press": "pinch", "commit": "direct"}, {"press": "air", "commit": "review"}, True),
        ({"press": "pinch", "commit": "direct"}, {"commit": "review"}, True),
        ({"press": "pinch"}, {"press": "air"}, True),
        ({"press": "windows", "commit": "direct"}, {"press": "air"}, False),
        ({"press": "pinch", "commit": "direct"}, {"enabled": True}, True),
        ({}, {"enabled": True}, True),
    ],
    ids=repr,
)
def test_the_merged_result_is_checked(start: dict[str, Any], body: dict[str, Any], ok: bool) -> None:
    s = KeyboardSettings()
    if start:
        s.apply(start)
    before = state(s)
    if ok:
        s.apply(body)
        assert state(s) == {**before, **{WIRE_TO_FIELD[k]: v for k, v in body.items()}}
    else:
        with pytest.raises(ValueError, match="review box"):
            s.apply(body)
        assert state(s) == before


def test_the_errors_name_the_key_and_never_echo_the_value() -> None:
    sentinel = "zzqxjv"
    for key in ("press", "commit", "layout", "dock", "inject", "enter", "size", "reach", "idleS", "enabled"):
        with pytest.raises(ValueError) as caught:
            KeyboardSettings().apply({key: sentinel})
        assert key in str(caught.value)
        assert sentinel not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        KeyboardSettings().apply({sentinel: 1})
    assert "unknown" in str(caught.value)
    assert len(str(caught.value)) < 200  # a key of any length cannot make the message huge
    with pytest.raises(ValueError) as caught:
        KeyboardSettings().apply({"x" * 5000: 1})
    assert len(str(caught.value)) < 200


# --------------------------------------------------------------------------- S26: fuzz


POOL: dict[str, list[Any]] = {
    "enabled": [True, False, 1, 0, "true", None],
    "press": ["air", "pinch", "windows", "osk", "", None, 1],
    "commit": ["review", "direct", "auto", None],
    "layout": ["auto", "en", "he", "fr", None],
    "size": [0.6, 1.0, 1.6, 1, 0.59, 1.61, True, "1", None, float("nan"), 10**400],
    "reach": [0.8, 1.0, 1.5, 0.79, 1.51, False, "1", None],
    "dock": ["top", "bottom", "left", None],
    "idleS": [5, 30, 300, 4, 301, 30.0, 30.5, True, "30", None, 10**400],
    "inject": ["unicode", "vk", "both", None],
    "enter": ["twice", "off", "once", None],
    "bogus": [1],
    "text": ["x"],
}


def test_fuzz_apply_is_all_or_nothing() -> None:
    rng = random.Random(20261008)
    applied = refused = 0
    s = KeyboardSettings()
    for _ in range(3000):
        body = {key: rng.choice(POOL[key]) for key in rng.sample(list(POOL), rng.randint(0, 5))}
        before = state(s)
        try:
            s.apply(body)
        except ValueError:
            refused += 1
            assert state(s) == before, body
        else:
            applied += 1
            after = state(s)
            assert set(after) == set(before)
            assert not (after["press"] == "air" and after["commit"] == "direct"), body
            # exactly the keys of the body may differ
            assert {k for k in after if after[k] != before[k]} <= {WIRE_TO_FIELD[k] for k in body}, body
        assert 0.6 <= s.size <= 1.6 and 0.8 <= s.reach <= 1.5 and 5 <= s.idle_s <= 300
        assert type(s.idle_s) is int and type(s.enabled) is bool
        assert math.isfinite(s.size) and math.isfinite(s.reach)
    assert applied > 200 and refused > 200


# --------------------------------------------------------------------------- the protocol validator agrees


def settings_body_ok_for_protocol(body: Any) -> bool:
    try:
        protocol.validate_command("keyboard", {"action": "configure", "settings": body})
    except protocol.ValidationError:
        return False
    return True


def test_apply_and_the_protocol_validator_accept_the_same_bodies() -> None:
    """Two hand-written tables for one contract: a body is valid for both or for neither.

    The merged check (air with direct) needs the state, which the protocol cannot know, so the comparison starts from
    a state where that check cannot fire on its own (press pinch, review) and leaves out the one combination it refuses.
    """
    rng = random.Random(41)
    checked = 0
    for _ in range(3000):
        body = {key: rng.choice(POOL[key]) for key in rng.sample(list(POOL), rng.randint(0, 5))}
        if body.get("press") == "air" and body.get("commit") == "direct":
            continue
        s = KeyboardSettings()
        s.apply({"press": "pinch"})
        try:
            s.apply(body)
            by_settings = True
        except ValueError:
            by_settings = False
        assert by_settings is settings_body_ok_for_protocol(body), body
        checked += 1
    assert checked > 2000


def test_the_only_disagreement_is_the_merged_check() -> None:
    assert settings_body_ok_for_protocol({"press": "air", "commit": "direct"})
    with pytest.raises(ValueError, match="review box"):
        KeyboardSettings().apply({"press": "air", "commit": "direct"})
