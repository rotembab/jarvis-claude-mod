"""The pinned types of ``keyboard/types.py`` (DESIGN 3.1, 3.16.1) and the additions to ``overlay/base.py`` (3.11).

Names, enum members, field order and defaults are the contract the other tracks import and build on; a change here is
a joint decision (6.3), so each one fails a test.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any, get_args

import numpy as np
import pytest

from jarvis_hands.keyboard import types as kt
from jarvis_hands.overlay import base as ob

LITERALS: dict[str, list[str]] = {
    "Side": ["left", "right"],
    "Lang": ["en", "he"],
    "KeyKind": [
        "char",
        "shift",
        "lang",
        "private",
        "home",
        "backspace",
        "space",
        "enter",
        "close",
        "insert",
        "clear",
        "chip",
    ],
    "Hold": ["blocked", "password", "covered", "overlay", "focus", "yield", "slow"],
    "Phase": ["placing", "warmup", "typing"],
    "Mode": ["live", "practice"],
    "Commit": ["review", "direct"],
    "CloseReason": [
        "command",
        "close_key",
        "fists",
        "idle",
        "paused",
        "desktop_locked",
        "runaway",
        "no_overlay",
        "camera",
        "disabled",
        "error",
        "input_blocked",
        "air_unreliable",
    ],
    "SendResult": ["sent", "hold", "limited", "failed"],
    "InsertResult": ["sent", "maybe", "hold", "limited", "failed"],
    "PressName": ["pinch", "air", "windows"],
    "PressLevel": ["ok", "degraded"],
    "TipState": ["open", "closing", "pressed", "latched"],
    "LitKind": ["ghost", "target", "ok", "drop", "armed", "on"],
    "Dock": ["top", "bottom"],
    "ReviewState": ["composing", "inserting", "aborted"],
    "GuardKind": ["insert", "clear", "send", "close"],
    "InsertAbort": ["blocked", "password", "covered", "overlay", "focus", "yield", "stopped", "timeout", "failed"],
    "RunKind": ["text", "enter"],
}


@pytest.mark.parametrize(("name", "members"), LITERALS.items(), ids=list(LITERALS))
def test_literal_members_and_their_order(name: str, members: list[str]) -> None:
    assert list(get_args(getattr(kt, name))) == members


def test_the_hold_order_is_the_priority_order() -> None:
    """2.7 step 3 takes the first hold of this list, so the order is part of the contract."""
    assert get_args(kt.Hold)[0] == "blocked" and get_args(kt.Hold)[-1] == "slow"


# --------------------------------------------------------------------------- the records


def field_table(cls: type) -> list[tuple[str, bool, Any]]:
    """(name, has a default, the default) in declaration order."""
    rows = []
    for f in dataclasses.fields(cls):
        has_default = f.default is not dataclasses.MISSING
        rows.append((f.name, has_default, f.default if has_default else None))
    return rows


RECORDS: dict[type, list[tuple[str, bool, Any]]] = {
    kt.FingerSample: [
        ("finger", False, None),
        ("aim", False, None),
        ("reach", False, None),
        ("ratio", False, None),
        ("curled", False, None),
        ("lift", True, 0.0),
    ],
    kt.HandSample: [
        ("hand", False, None),
        ("side", False, None),
        ("t", False, None),
        ("palm", False, None),
        ("anchor", False, None),
        ("speed", False, None),
        ("fingers", False, None),
        ("score", True, 1.0),
    ],
    kt.PressEvent: [
        ("t", False, None),
        ("onset_t", False, None),
        ("hand", False, None),
        ("side", False, None),
        ("finger", False, None),
        ("aim", False, None),
        ("ratio", False, None),
        ("margin", False, None),
        ("depth", True, 0.0),
        ("conf", True, 0.0),
    ],
    kt.FingerView: [
        ("hand", False, None),
        ("side", False, None),
        ("finger", False, None),
        ("aim", False, None),
        ("state", False, None),
        ("fill", True, 0.0),
        ("note", True, ""),
    ],
    kt.PressQuality: [("fps", False, None), ("noise", False, None), ("gaps", True, 0)],
    kt.Touch: [
        ("u", False, None),
        ("v", False, None),
        ("finger", False, None),
        ("side", False, None),
        ("t", False, None),
        ("conf", True, 1.0),
    ],
    kt.DecodeRequest: [
        ("seq", False, None),
        ("version", False, None),
        ("start", False, None),
        ("end", False, None),
        ("head", False, None),
        ("touches", False, None),
        ("press", False, None),
        ("apply", False, None),
    ],
    kt.DecodeResult: [
        ("seq", False, None),
        ("version", False, None),
        ("start", False, None),
        ("end", False, None),
        ("cands", False, None),
        ("verdict", False, None),
        ("p_top", False, None),
        ("ms", False, None),
    ],
}


@pytest.mark.parametrize("cls", RECORDS, ids=lambda c: c.__name__)
def test_record_fields_order_and_defaults(cls: type) -> None:
    assert field_table(cls) == RECORDS[cls]


@pytest.mark.parametrize("cls", RECORDS, ids=lambda c: c.__name__)
def test_records_are_frozen(cls: type) -> None:
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen


def test_the_air_additions_are_appended_with_defaults_so_positional_calls_keep_compiling() -> None:
    """The first version's constructors (and the synthetic scaffolding) pass these positionally; defaults come last."""
    finger = kt.FingerSample(0, np.zeros(2), 1.0, 0.5, False)
    assert finger.lift == 0.0
    hand = kt.HandSample(1, "right", 0.1, 0.1, np.zeros(2), 0.0, (finger,) * 4)
    assert hand.score == 1.0
    event = kt.PressEvent(0.2, 0.1, 1, "left", 2, (0.0, 0.0), 0.3, 0.2)
    assert (event.depth, event.conf) == (0.0, 0.0)
    view = kt.FingerView(1, "left", 2, (0.0, 0.0), "open")
    assert (view.fill, view.note) == (0.0, "")
    assert kt.PressQuality(0.0, None).gaps == 0


def test_touch_is_as_sensitive_as_the_text() -> None:
    """SR26: where a tap fell is as private as the letter it typed, so it prints as a fixed word, everywhere."""
    touch = kt.Touch(3.14159, 0.77777, 1, "left", 12.5, 0.9)
    assert repr(touch) == "<Touch>"
    assert str(touch) == "<Touch>"
    assert f"{touch}" == "<Touch>" and f"{touch!r}" == "<Touch>" and "%s %r" % (touch, touch) == "<Touch> <Touch>"  # noqa: UP031
    assert "3.14" not in repr([touch]) and "0.777" not in repr({"t": touch})
    assert not hasattr(touch, "__dict__")  # slots: nothing to dump by accident
    with pytest.raises(dataclasses.FrozenInstanceError):
        touch.u = 0.0  # type: ignore[misc]


def test_touch_defaults_and_identity_equality() -> None:
    a = kt.Touch(1.0, 2.0, 0, "right", 0.5)
    b = kt.Touch(1.0, 2.0, 0, "right", 0.5)
    assert a.conf == 1.0
    assert a == a and a != b  # eq=False: a decoder never compares touches by value


def test_decoder_records_print_without_their_content() -> None:
    touch = kt.Touch(1.0, 2.0, 0, "right", 0.5)
    request = kt.DecodeRequest(7, 3, 0, 5, "zzqxjv", (touch,) * 6, "air", True)
    result = kt.DecodeResult(7, 3, 0, 5, (("zzqxjv", 0.9),), "keep", 0.9, 1.5)
    assert repr(request) == "<DecodeRequest seq=7 n=6>"
    assert repr(result) == "<DecodeResult seq=7>"
    assert "zzqxjv" not in repr(request) + str(request) + repr(result) + str(result)
    assert not hasattr(request, "__dict__") and not hasattr(result, "__dict__")
    assert request != kt.DecodeRequest(7, 3, 0, 5, "zzqxjv", (touch,) * 6, "air", True)  # eq=False


def test_decoder_protocol_names() -> None:
    assert {"name", "submit", "poll", "keep", "close"} <= set(vars(kt.Decoder)) | set(kt.Decoder.__annotations__)
    assert typing.Protocol in kt.Decoder.__mro__


def test_press_method_protocol_names() -> None:
    wanted = {
        "name",
        "requires_review",
        "rejects",
        "update",
        "reset",
        "set_finger",
        "fingers",
        "quality",
        "set_level",
        "set_calibrating",
    }
    assert wanted <= set(vars(kt.PressMethod)) | set(kt.PressMethod.__annotations__)
    assert typing.Protocol in kt.PressMethod.__mro__


# --------------------------------------------------------------------------- overlay/base.py (3.11)

OVERLAY_RECORDS: dict[type, list[tuple[str, bool, Any]]] = {
    ob.TipView: [
        ("u", False, None),
        ("v", False, None),
        ("side", False, None),
        ("finger", False, None),
        ("state", False, None),
        ("done", True, True),
        ("named", True, False),
        ("fill", True, 0.0),
        ("note", True, ""),
    ],
    ob.ComposeView: [
        ("text", False, None),
        ("length", False, None),
        ("state", False, None),
        ("sent", False, None),
        ("guard", False, None),
        ("guard_taps", False, None),
        ("guard_need", False, None),
        ("guard_left", False, None),
        ("can_send", False, None),
        ("full", False, None),
        ("chips", True, ()),
        ("chip_active", True, None),
    ],
    ob.KeyboardView: [
        ("seq", False, None),
        ("mode", False, None),
        ("phase", False, None),
        ("lang", False, None),
        ("shift", False, None),
        ("private", False, None),
        ("pulse", False, None),
        ("hold", False, None),
        ("armed_enter", False, None),
        ("drift", False, None),
        ("tips", False, None),
        ("homes", False, None),
        ("lit", False, None),
        ("strip", False, None),
        ("echo", False, None),
        ("prompt", False, None),
        ("progress", False, None),
        ("work", True, (0, 0, 0, 0)),
        ("size", True, 1.0),
        ("dock", True, "top"),
        ("exclude_capture", True, False),
        ("banner", True, ""),
        ("banner_level", True, ""),
        ("commit", True, "direct"),
        ("compose", True, None),
    ],
    ob.OverlayHealth: [
        ("alive", False, None),
        ("failures", False, None),
        ("ok_age_s", False, None),
        ("keyboard_ok", False, None),
        ("draw_ms", True, None),
    ],
}


@pytest.mark.parametrize("cls", OVERLAY_RECORDS, ids=lambda c: c.__name__)
def test_overlay_record_fields_order_and_defaults(cls: type) -> None:
    assert field_table(cls) == OVERLAY_RECORDS[cls]
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_overlay_state_gains_one_field_at_the_end_and_keeps_the_rest() -> None:
    names = [f.name for f in dataclasses.fields(ob.OverlayState)]
    assert names == ["mode", "cursor", "helper", "pinch", "progress", "keyboard"]
    state = ob.OverlayState()
    assert state.keyboard is None and state.mode == "hidden"
    # the reticle callers build states positionally and by keyword; neither changes
    assert ob.OverlayState("point", None, None, 0.5, 0.0) == ob.OverlayState(mode="point", pinch=0.5)


def make_view(**changes: Any) -> ob.KeyboardView:
    base: dict[str, Any] = {
        "seq": 1,
        "mode": "live",
        "phase": "typing",
        "lang": "en",
        "shift": False,
        "private": False,
        "pulse": False,
        "hold": None,
        "armed_enter": False,
        "drift": False,
        "tips": (ob.TipView(1.0, 1.5, "right", 0, "open"),),
        "homes": ((1.0, 1.5, "right"),),
        "lit": ((3, "ok"),),
        "strip": "",
        "echo": "",
        "prompt": "",
        "progress": 0.0,
    }
    return ob.KeyboardView(**{**base, **changes})


def test_views_are_hashable_and_equal_by_value() -> None:
    """3.11: all are hashable and cheap to compare (the overlay skips a redraw when a view equals the last but seq)."""
    compose = ob.ComposeView("hello", 5, "composing", 0, None, 0, 0, 0.0, False, False)
    one, two = make_view(compose=compose), make_view(compose=compose)
    assert one == two and hash(one) == hash(two)
    assert one != make_view(compose=compose, seq=2)
    assert dataclasses.replace(one, seq=2) == make_view(compose=compose, seq=2)
    assert len({one, two, make_view(strip="x")}) == 2
    assert ob.OverlayState(keyboard=one) == ob.OverlayState(keyboard=two)
    hash(ob.OverlayState(keyboard=one))


def test_a_view_prints_without_the_typed_text() -> None:
    """The box text and the echo are typed content (SR13): a repr that reaches a log must not carry them."""
    compose = ob.ComposeView("zzqxjv", 6, "composing", 0, None, 0, 0, 0.0, False, False)
    view = make_view(compose=compose, echo="qjxzvz")
    shown = repr(view) + str(view) + repr(compose) + repr(ob.OverlayState(keyboard=view))
    assert "zzqxjv" not in shown and "qjxzvz" not in shown


def test_a_view_prints_without_candidate_words_or_key_identity() -> None:
    """SR13 names key identity and SR44 makes a candidate list as sensitive as the box ('repr shows counts only')."""
    compose = ob.ComposeView("", 0, "composing", 0, None, 0, 0, 0.0, False, False, chips=("wordish", "otherish"))
    view = make_view(compose=compose, lit=((37, "ok"), (38, "ok")))
    shown = repr(view) + str(view) + repr(compose) + repr(ob.OverlayState(keyboard=view))
    assert "wordish" not in shown and "otherish" not in shown
    assert "37" not in shown and "38" not in shown  # the indices of the keys just pressed
    # hidden from the print only: a view still compares and hashes by what it shows on the screen
    assert view != make_view(compose=compose, lit=((39, "ok"),))
    assert compose != dataclasses.replace(compose, chips=("wordish",))


# --------------------------------------------------------------------------- overlay_health


class WithHealth:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    def health(self) -> Any:
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def test_overlay_health_reads_the_method_when_there_is_one() -> None:
    health = ob.OverlayHealth(True, 0, 0.01, True)
    overlay = WithHealth(health)
    assert ob.overlay_health(overlay) is health
    assert overlay.calls == 1


@pytest.mark.parametrize("overlay", [ob.NullOverlay(), object(), None, 5], ids=["null", "object", "none", "int"])
def test_overlay_health_is_none_without_the_method(overlay: object) -> None:
    assert ob.overlay_health(overlay) is None


def test_overlay_health_is_not_part_of_the_overlay_protocol() -> None:
    """RecordingOverlay and every other double keep working: `health` is asked for with getattr."""
    assert "health" not in vars(ob.Overlay)
    assert not hasattr(ob.NullOverlay(), "health")


def test_a_health_method_that_fails_means_unknown_health() -> None:
    """The controller reads None as 'not healthy' (fail closed), so a broken probe must not escape as an exception."""
    assert ob.overlay_health(WithHealth(RuntimeError("zzqxjv"))) is None
    assert ob.overlay_health(WithHealth(None)) is None
    assert ob.overlay_health(WithHealth("not a health")) is None  # only an OverlayHealth counts
