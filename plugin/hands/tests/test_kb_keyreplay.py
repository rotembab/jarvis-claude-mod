"""``keyreplay``: the offline replay of a landmark trace (DESIGN-KEYBOARD.md 3.13, 2.12.10, 5.7; X50).

The traces here are made, not recorded: ``AirTypist`` hands tap scripted fingers in the time windows of a drill, the
frames go through ``TraceWriter`` (and, for a ``keytrace`` file, the prompt table is added to the npz the way
``keytrace`` adds it), and the replay is run on the file. Nothing opens a camera or sleeps. What is pinned: the
arguments and every refusal, ``--set`` (inside the clamps, never silently another value), the report numbers on traces
whose truth is known, the press method that is read from the file, and ``--write``: atomic, merged, clamped, and
unable to name anything but a ``Tuning`` field.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import types
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_hands import synthetic as syn
from jarvis_hands.keyboard import keyreplay
from jarvis_hands.keyboard import synth as sy
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.limits import AIR_DRILL_GAP_S
from jarvis_hands.keyboard.plane import Plane
from jarvis_hands.keyboard.practice import HOME_CHARS
from jarvis_hands.keyboard.trace import TraceWriter
from jarvis_hands.keyboard.tuning import (
    AIR_AIM_CHOICES,
    GROUPS,
    LEVEL_PALM_RANGE,
    RANGES,
    Tuning,
    load_tuning,
    tuning_path,
)
from jarvis_hands.landmarks import Frame, HandObservation

from scripted import Script

REVIEW = layout_for("review")
FPS = 30.0
T0 = 1000.0
HOME = {"left": (0.36, 0.55), "right": (0.64, 0.55)}
PLACE_S = 3.0
SIDES = ("left", "right")


# ------------------------------------------------------------------------------------------------ the made traces


def centre(key: Any) -> tuple[float, float]:
    return key.col + key.width / 2, key.row + 0.5


@dataclass
class Made:
    """A written trace and what is true about it."""

    path: Path
    #: (side, finger, start second of the trace) of every scripted tap.
    taps: list[tuple[str, int, float]]
    #: (frame, side 0/1, finger, key, displaced) of every prompt, as ``keytrace`` stores them.
    prompts: list[tuple[int, int, int, int, int]]
    frames: int


def with_prompts(path: Path, rows: list[tuple[int, int, int, int, int]]) -> None:
    """Adds the ``drill_prompts`` array to a trace file the way ``keytrace`` does."""
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    arrays["drill_prompts"] = np.array(rows, dtype=np.int32).reshape(len(rows), 5)
    np.savez_compressed(path, **arrays)


WARM_ORDER = [(side, f) for f in range(4) for side in ("right", "left")]  # AIR_WARMUP_ORDER: right first, then left


def drill_trace(
    path: Path,
    *,
    prompts: int = 24,
    fingers: tuple[int, ...] = (0, 1, 2, 3),
    sides: tuple[str, ...] = SIDES,
    displaced: bool = False,
    rest_s: float = 0.0,
    rest_taps: tuple[tuple[str, int, float], ...] = (),
    wave_s: float = 0.0,
    sigma: float = 0.001,
    amp: float = 40.0,
    fps: float = FPS,
    press: str = "air",
    seed: int = 1,
    written_prompts: bool = True,
    skip: tuple[int, ...] = (),
    extra: tuple[tuple[int, str, int], ...] = (),
    place_s: float = PLACE_S,
    lead_s: float = 0.08,
    warm: bool = False,
    move_s: float = 0.2,
    drill_kind: str = "drill",
    warm_taps: int = len(WARM_ORDER),
) -> Made:
    """place, (a warm-up of one tap per finger,) ``prompts`` drill prompts 1.2 s apart (one tap each, 0.25 s into its
    window), then rest and wave.

    ``skip`` leaves a prompt without its tap, ``extra`` adds ``(prompt, side, finger)`` taps 0.6 s into a window (a
    second tap, or one of the wrong finger), ``rest_taps`` are ``(side, finger, second in the rest)``, ``lead_s`` is
    how long the hand holds over the key before the tap (0 and it is still moving), ``move_s`` how long it takes to
    get there, ``drill_kind`` what the prompted stretch is called in the file ("phrase" in a practice trace) and
    ``warm_taps`` how many of the warm-up's eight taps are made (the segment is as long as ever).
    """
    rng = np.random.default_rng(seed)
    order = [(sides[k % len(sides)], fingers[(k // len(sides)) % len(fingers)]) for k in range(prompts)]
    taps: list[tuple[str, int, float]] = []
    rows: list[tuple[int, int, int, int, int]] = []
    events: dict[str, list[sy.Event]] = {s: [] for s in SIDES}

    def tap(side: str, finger: int, at: float, move: tuple[float, float] = (0.0, 0.0)) -> None:
        events[side].append(sy.Event(side, finger, at, move, amp, 0.2, mt=move_s))
        taps.append((side, finger, at))

    warm_s = 2.0 * len(WARM_ORDER) + 1.0 if warm else 0.0
    for i, (side, finger) in enumerate(WARM_ORDER[:warm_taps] if warm else []):
        tap(side, finger, place_s + 1.5 + 2.0 * i)
    drill_start = place_s + warm_s
    for k, (side, finger) in enumerate(order):
        start = drill_start + k * AIR_DRILL_GAP_S
        home = REVIEW.find(char=HOME_CHARS[side][finger])
        key, move = home, (0.0, 0.0)
        if displaced and k % 2 == 1:
            key = REVIEW.find(char="e" if side == "left" else "i")
            (hu, hv), (ku, kv) = centre(home), centre(key)
            move = (ku - hu, kv - hv)
        rows.append((round(start * fps), 0 if side == "left" else 1, finger, key.index, int(key is not home)))
        if k not in skip:
            tap(side, finger, start + 0.25, move)
    for k, side, finger in extra:
        tap(side, finger, drill_start + k * AIR_DRILL_GAP_S + 0.6)
    drill_end = drill_start + prompts * AIR_DRILL_GAP_S
    for side, finger, at in rest_taps:
        tap(side, finger, drill_end + at)
    total = drill_end + rest_s + wave_s + 1.0
    noise = sy.Noise(sigma=sigma, glitch_p=0.0 if sigma < 0.0015 else 0.003, amp_gain=(1.0, 1.0))
    hands = {s: sy.AirTypist(s, HOME[s], events[s], rng, noise, lead_s=lead_s) for s in SIDES}
    writer = TraceWriter(press=press)  # type: ignore[arg-type]
    for n in range(int(total * fps)):
        t = n / fps
        frame = Frame(T0 + t, tuple(hands[s].observe(t, 1 / fps) for s in SIDES), 1280, 720)
        window = int((t - drill_start) / AIR_DRILL_GAP_S)
        if t < place_s:
            kind, target = "place", -1
        elif t < drill_start:
            kind, target = "warm", -1
        elif t < drill_end:
            kind, target = drill_kind, rows[window][3]
        else:
            kind, target = ("rest" if t < drill_end + rest_s else "wave"), -1
        writer.add(frame, kind, target)
    writer.save(path)
    if written_prompts:
        with_prompts(path, rows)
    return Made(path, taps, rows, len(writer))


def arguments(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    keyreplay.add_arguments(parser)
    return parser.parse_args(list(argv))


def replay(path: Path, *argv: str, data_dir: Path | None = None) -> tuple[int, str]:
    out = io.StringIO()
    extra = ["--data-dir", str(data_dir)] if data_dir is not None else []
    code = keyreplay.run(arguments(str(path), *extra, *argv), out=out)
    return code, out.getvalue()


# ------------------------------------------------------------------------------------------------- the module


def test_the_stub_is_gone_and_the_two_pinned_names_exist():
    assert not hasattr(keyreplay, "STUB_OWNER")
    assert callable(keyreplay.add_arguments) and callable(keyreplay.run)


def test_the_arguments_are_a_file_press_set_csv_write_and_data_dir():
    args = arguments("t.npz")
    assert args.file == Path("t.npz") and args.press is None and args.sets == [] and args.csv is None
    assert args.write is False and args.data_dir is None and args.no_suggest is False
    args = arguments("t.npz", "--press", "pinch", "--set", "air_theta_k=6.5", "--set", "air_aim=onset", "--write")
    assert args.press == "pinch" and args.sets == ["air_theta_k=6.5", "air_aim=onset"] and args.write is True
    with pytest.raises(SystemExit):
        arguments("t.npz", "--press", "windows")
    with pytest.raises(SystemExit):
        arguments()


# ------------------------------------------------------------------------------------------------------------ --set


def sets(*pairs: str, base: Tuning | None = None) -> Tuning:
    return keyreplay.parse_sets(list(pairs), base if base is not None else Tuning())


def test_a_set_changes_the_field_named_and_nothing_else():
    changed = sets("air_theta_k=6.5")
    assert changed.air_theta_k == 6.5
    assert changed == Tuning(air_theta_k=6.5)


def test_a_set_starts_from_the_tuning_it_is_given_and_a_later_set_wins():
    base = Tuning(pitch=0.05, air_aim="commit")
    changed = sets("air_theta_k=6", "air_theta_k=7", base=base)
    assert changed == Tuning(pitch=0.05, air_aim="commit", air_theta_k=7.0)


def test_every_kind_of_field_can_be_set():
    changed = sets("confirm_frames=3", "air_aim=onset", "level_palm=0,0.1,0.05,-0.1", "z_scale=0.5", "air_speed_gate=1")
    assert changed.confirm_frames == 3 and isinstance(changed.confirm_frames, int)
    assert changed.air_aim == "onset"
    assert changed.level_palm == (0.0, 0.1, 0.05, -0.1)
    assert changed.z_scale == 0.5 and changed.air_speed_gate == 1.0


@pytest.mark.parametrize(
    "pair",
    [
        "zzz_nothing=1", "air_theta_k", "=3", "air_theta_k=", "air_theta_k=abc", "air_theta_k=nan", "air_theta_k=inf",
        "air_theta_k=3.9", "air_theta_k=8.1", "air_theta_min=0", "air_theta_min=0.07", "air_veto_ratio=0.2",
        "air_veto_ratio=0.49", "confirm_frames=2.5", "confirm_frames=9", "air_aim=peak", "air_aim=", "level_palm=1,2,3",
        "level_palm=0,0,0,9", "level_palm=a,b,c,d", "AIR_MIN_SCORE=0.1", "min_score=0.1", "air_refractory_s=0",
        "version=2", "close=-1", "close=0.5",
    ],
)  # fmt: skip
def test_a_set_outside_the_clamps_or_of_no_field_is_refused_never_replaced(pair):
    with pytest.raises(keyreplay.ReplayError) as caught:
        sets(pair)
    message = str(caught.value)
    assert message.endswith(".") and "\n" not in message  # one fixed sentence


def test_a_refused_set_names_the_field_and_its_range_and_never_the_value_typed():
    with pytest.raises(keyreplay.ReplayError) as caught:
        sets("air_theta_k=3.123456")
    assert "air_theta_k" in str(caught.value) and "3.123456" not in str(caught.value)
    assert f"{RANGES['air_theta_k'][0]:g}" in str(caught.value) and f"{RANGES['air_theta_k'][1]:g}" in str(caught.value)
    with pytest.raises(keyreplay.ReplayError) as caught:
        sets("zzz_secret_name=1")
    assert "zzz_secret_name" not in str(caught.value)


@pytest.mark.parametrize(
    ("pair", "sentence"),
    [
        ("air_theta_k", "That is not a setting"),  # a name with no value is not a pair
        ("air_theta_k=99", "air_theta_k is a number from 4 to 8"),
        ("air_theta_k=high", "air_theta_k is a number from 4 to 8"),
        ("confirm_frames=99", "confirm_frames is a whole number from"),
        ("confirm_frames=2.5", "confirm_frames is a whole number from"),
        ("level_palm=0,0,0,9", "level_palm is four numbers from -0.3 to 0.3"),
        ("level_palm=0,0,0", "level_palm is four numbers from -0.3 to 0.3"),
        ("air_aim=peak", "air_aim is one of: auto, onset, commit."),
    ],
)
def test_each_kind_of_refusal_says_what_that_field_takes(pair, sentence):
    with pytest.raises(keyreplay.ReplayError) as caught:
        sets(pair)
    assert str(caught.value).startswith(sentence)


def test_spaces_around_a_name_or_a_value_do_not_matter():
    changed = sets(" air_theta_k = 6.5 ", "air_aim= onset ", "level_palm= 0, 0.1 ,0,0 ")
    assert changed.air_theta_k == 6.5 and changed.air_aim == "onset" and changed.level_palm == (0.0, 0.1, 0.0, 0.0)
    assert keyreplay.set_names([" air_theta_k = 6.5 ", "air_aim=onset"]) == {"air_theta_k", "air_aim"}


def test_the_edges_of_every_range_are_accepted():
    paired = {"close", "open", "curled_enter", "curled_leave", "air_theta_min", "air_theta_max"}
    for name, (low, high) in RANGES.items():
        if name in paired:
            continue
        for edge in (low, high):
            text = str(int(edge)) if name in ("confirm_frames", "aim_frames") else repr(float(edge))
            assert getattr(sets(f"{name}={text}"), name) == edge, name


def test_two_fields_that_break_a_relation_are_refused_together():
    for pairs in (
        ("air_theta_min=0.2", "air_theta_max=0.18"),
        ("close=0.36", "open=0.34"),
        ("curled_enter=1.2", "curled_leave=1.15"),
    ):
        with pytest.raises(keyreplay.ReplayError):
            sets(*pairs)
    assert sets("air_theta_min=0.2", "air_theta_max=0.2").air_theta_min == 0.2


def test_every_tuning_field_has_a_group_and_a_set_name():
    names = {name for group in GROUPS.values() for name in group}
    assert names == set(keyreplay.TUNABLE)


# ---------------------------------------------------------------------------------------------------- --write file


def test_a_write_makes_the_file_with_the_group_of_each_field_and_loads_back(tmp_path):
    path = tuning_path(tmp_path)
    names = keyreplay.write_tuning(path, {"air_theta_k": 6.0, "air_aim": "onset", "pitch": 0.05})
    assert sorted(names) == ["air_aim", "air_theta_k", "pitch"]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "plane": {"pitch": 0.05},
        "air": {"air_theta_k": 6.0, "air_aim": "onset"},
    }
    loaded = load_tuning(tmp_path)
    assert (loaded.air_theta_k, loaded.air_aim, loaded.pitch) == (6.0, "onset", 0.05)


def test_a_write_merges_into_what_is_there_and_keeps_the_rest(tmp_path):
    path = tuning_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"version": 1, "pinch": {"close": 0.3}, "air": {"air_theta_min": 0.12, "airThetaK": 5.5}, "mine": 7}
        ),
        encoding="utf-8",
    )
    keyreplay.write_tuning(path, {"air_theta_k": 6.0, "air_depth_frac": 0.6})
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["pinch"] == {"close": 0.3} and doc["mine"] == 7
    assert doc["air"] == {
        "air_theta_min": 0.12,
        "air_theta_k": 6.0,
        "air_depth_frac": 0.6,
    }  # the camelCase twin is gone
    loaded = load_tuning(tmp_path)
    assert (loaded.close, loaded.air_theta_min, loaded.air_theta_k) == (0.3, 0.12, 6.0)


def test_a_write_clamps_every_value_to_its_range_and_so_never_below_a_floor(tmp_path):
    path = tuning_path(tmp_path)
    keyreplay.write_tuning(
        path,
        {
            "air_theta_k": 1.0, "air_theta_min": 0.0, "air_veto_ratio": 0.1, "air_theta_max": 9.0,
            "air_depth_frac": 5, "pitch": 1.0,
        },
    )  # fmt: skip
    air = json.loads(path.read_text(encoding="utf-8"))
    assert air["air"]["air_theta_k"] == RANGES["air_theta_k"][0] >= 4.0
    assert air["air"]["air_theta_min"] == RANGES["air_theta_min"][0] >= 0.08
    assert air["air"]["air_veto_ratio"] == RANGES["air_veto_ratio"][0] >= 0.5
    assert air["air"]["air_theta_max"] == RANGES["air_theta_max"][1]
    assert air["air"]["air_depth_frac"] == RANGES["air_depth_frac"][1]
    assert air["plane"]["pitch"] == RANGES["pitch"][1]
    loaded = load_tuning(tmp_path)  # and the loader takes every one of them as written
    assert loaded.air_theta_k == 4.0 and loaded.air_veto_ratio == 0.5 and loaded.pitch == RANGES["pitch"][1]


def test_a_write_makes_whole_numbers_of_the_count_fields_and_clamps_the_levels(tmp_path):
    path = tuning_path(tmp_path)
    keyreplay.write_tuning(path, {"confirm_frames": 2.0, "level_palm": (9.0, 0.0, 0.1, -9.0)})
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["pinch"]["confirm_frames"] == 2 and isinstance(doc["pinch"]["confirm_frames"], int)
    assert doc["hands"]["level_palm"] == [0.3, 0.0, 0.1, -0.3]


@pytest.mark.parametrize(
    "values",
    [
        {"min_score": 0.1}, {"MIN_SCORE": 0.1}, {"AIR_MIN_SCORE": 0.1}, {"version": 2}, {"air_nothing": 1},
        {"__class__": 1}, {"air": {"air_theta_k": 5}}, {"air_theta_k": 6, "air_refractory_s": 0},
        {"air_theta_k": float("nan")}, {"air_theta_k": float("inf")}, {"air_theta_k": "6"}, {"air_theta_k": True},
        {"air_aim": "peak"}, {"air_aim": 3}, {"level_palm": (0, 0, 0)}, {"air_theta_min": 0.2, "air_theta_max": 0.18},
    ],
)  # fmt: skip
def test_a_write_names_only_tuning_fields_with_usable_values_and_otherwise_writes_nothing(tmp_path, values):
    path = tuning_path(tmp_path)
    with pytest.raises(keyreplay.ReplayError):
        keyreplay.write_tuning(path, values)
    assert not path.exists() and not list(tmp_path.rglob("*.tmp"))


def test_no_safety_constant_of_limits_can_be_named_in_a_write(tmp_path):
    from jarvis_hands.keyboard import limits

    tunable = set(keyreplay.TUNABLE)
    for name in dir(limits):
        if not name.isupper():
            continue
        for spelling in (name, name.lower()):
            if spelling in tunable:
                continue
            with pytest.raises(keyreplay.ReplayError):
                keyreplay.write_tuning(tuning_path(tmp_path), {spelling: 0.0})
    assert not tuning_path(tmp_path).exists()


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'"text"', b'{"version": 2, "air": {}}', b'{"version": "1"}', b'{"version": 1, "air": 5}',
     b'{"version": 1, "air": []}', b"\xff\xfe", b"x" * 70000],
)  # fmt: skip
def test_a_write_over_a_file_it_cannot_read_as_a_tuning_file_refuses_and_leaves_it_as_it_is(tmp_path, content):
    path = tuning_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(keyreplay.ReplayError):
        keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    assert path.read_bytes() == content and not list(tmp_path.rglob("*.tmp"))


def test_a_failed_replace_leaves_the_old_file_whole_and_no_temporary_file(tmp_path, monkeypatch):
    path = tuning_path(tmp_path)
    keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    before = path.read_bytes()

    def broken(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("jarvis_hands.keyboard.keyreplay.os.replace", broken)
    with pytest.raises(keyreplay.ReplayError):
        keyreplay.write_tuning(path, {"air_theta_k": 7.0})
    assert path.read_bytes() == before and not list(tmp_path.rglob("*.tmp"))


def test_the_new_content_is_whole_in_the_temporary_file_before_it_replaces_the_old(tmp_path, monkeypatch):
    path = tuning_path(tmp_path)
    keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    seen: list[Any] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        seen.append((Path(src).parent == path.parent, json.loads(Path(src).read_text(encoding="utf-8"))))
        real(src, dst)

    monkeypatch.setattr("jarvis_hands.keyboard.keyreplay.os.replace", spy)
    keyreplay.write_tuning(path, {"air_theta_k": 7.0})
    assert seen == [(True, {"version": 1, "air": {"air_theta_k": 7.0}})]  # beside the file: one filesystem, one rename


def test_a_folder_in_place_of_the_file_is_refused(tmp_path):
    path = tuning_path(tmp_path)
    path.mkdir(parents=True)
    with pytest.raises(keyreplay.ReplayError):
        keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    assert path.is_dir()


# ------------------------------------------------------------------------------------------- other traces we make


def hover_trace(
    path: Path,
    *,
    fps: float,
    seconds: float,
    press: str = "air",
    kind: str = "rest",
    taps: tuple[tuple[str, int, float], ...] = (),
) -> Path:
    """Two still hands at ``fps``, with a tap by ``(side, finger, second)`` where scripted (none by default)."""
    rng = np.random.default_rng(3)
    noise = sy.Noise(sigma=0.0008, glitch_p=0.0)
    events = {
        s: [sy.Event(s, f, at, (0.0, 0.0), 40.0, 0.2, mt=0.2) for side, f, at in taps if side == s] for s in SIDES
    }
    hands = {s: sy.AirTypist(s, HOME[s], events[s], rng, noise) for s in SIDES}
    writer = TraceWriter(press=press)  # type: ignore[arg-type]
    for n in range(int(seconds * fps)):
        t = n / fps
        writer.add(Frame(T0 + t, tuple(hands[s].observe(t, 1 / fps) for s in SIDES), 1280, 720), kind)
    return writer.save(path)


def noisy_hand_trace(
    path: Path, *, sigma_xy: float, sigma_z: float, seconds: float = 10.0, kind: str = "rest", loud_tip: float = 1.0
) -> Path:
    """One hand, exactly still, with independent Gaussian noise of known size on every landmark (the index fingertip's
    is ``loud_tip`` times that)."""
    rng = np.random.default_rng(5)
    base = syn.hand("palm", (0.5, 0.5), handedness="right")
    aspect = 720 / 1280
    writer = TraceWriter(press="air")
    for n in range(int(seconds * FPS)):
        image = base.image.copy()
        image[:, 0] += rng.normal(0, sigma_xy, 21)
        image[:, 1] += rng.normal(0, sigma_xy / aspect, 21)  # sigma_xy in frame widths on both axes
        image[:, 2] += rng.normal(0, sigma_z, 21)
        if loud_tip != 1.0:
            image[8] = base.image[8] + rng.normal(0, 1, 3) * np.array([sigma_xy, sigma_xy / aspect, sigma_z]) * loud_tip
        obs = HandObservation(handedness="right", score=0.95, image=image, world=base.world)
        writer.add(Frame(T0 + n / FPS, (obs,), 1280, 720), kind)
    return writer.save(path)


def pinch_trace(path: Path, *, press: str = "pinch", text: str = "fdsa jkl hello world", drill: bool = False) -> Path:
    """A pinch recording: place, the warm-up pinches, typing, a rest. ``drill`` relabels the typing as a drill."""
    script = Script(t0=T0, fps=FPS)
    t = Tuning()
    plane = Plane(0.5, 0.45 * script.height / script.width, t.pitch, t.pitch * t.pitch_y_ratio, 5)
    typist = sy.Typist(script, plane, rng=np.random.default_rng(1), layout=REVIEW)
    writer = TraceWriter(press=press)  # type: ignore[arg-type]
    for frames, kind in (
        (typist.place(), "place"),
        (typist.warm(), "tap"),
        (typist.type(text, gap_s=0.5), "drill" if drill else "type"),
        (typist.hover(4.0), "rest"),
    ):
        for frame in frames:
            writer.add(frame, kind)
    return writer.save(path)


def as_version_1(path: Path) -> None:
    """The file as T2's first version wrote it: no ``press``, version 1."""
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files if name != "press"}
    arrays["version"] = np.int32(1)
    np.savez_compressed(path, **arrays)


@pytest.fixture(scope="module")
def clean(tmp_path_factory) -> Made:
    """32 home-key prompts of all eight fingers on clean landmarks, then 20 s of rest with three stray taps."""
    path = tmp_path_factory.mktemp("clean") / "clean.npz"
    return drill_trace(
        path,
        prompts=32,
        rest_s=20.0,
        rest_taps=(("left", 0, 5.0), ("left", 0, 12.0), ("right", 2, 16.0)),
        wave_s=4.0,
        extra=((3, "left", 3), (5, "right", 2)),
        skip=(7,),
    )


@pytest.fixture(scope="module")
def clean_report(clean: Made) -> dict[str, Any]:
    return keyreplay.analyse(keyreplay.load(clean.path), Tuning(), suggest=False)


def finger(report: dict[str, Any], label: str) -> dict[str, Any]:
    side, name = label.split(".")
    return next(f for f in report["fingers"] if f["side"] == side and f["name"] == name)


# ------------------------------------------------------------------------------------------------------ the files


def test_a_missing_file_is_refused_with_one_line_that_does_not_repeat_the_path(tmp_path):
    code, text = replay(tmp_path / "zzz_missing.npz")
    assert code == keyreplay.EXIT_REFUSED == 2
    assert len(text.strip().splitlines()) == 1 and "zzz_missing" not in text


@pytest.mark.parametrize("name", ["garbage", "pickle", "empty_npz", "wrong_shapes"])
def test_a_file_that_is_not_a_trace_is_refused_with_one_line(tmp_path, name):
    path = tmp_path / "zzz_bad.npz"
    if name == "garbage":
        path.write_bytes(b"this is not a zip file")
    elif name == "pickle":
        np.savez(path, t=np.array([object()], dtype=object))
    elif name == "empty_npz":
        np.savez(path, other=np.zeros(3))
    else:
        np.savez(path, t=np.zeros(5), present=np.zeros((5, 2), bool), version=np.int32(2), press=np.str_("air"))
    code, text = replay(path)
    assert code == 2 and len(text.strip().splitlines()) == 1 and "zzz_bad" not in text


@pytest.mark.parametrize(
    "rows",
    [
        [(0, 0, 0, 5)],  # four columns
        [(0, 2, 0, 5, 0)],  # a side that is neither hand
        [(0, 0, 4, 5, 0)],  # a fifth finger
        [(0, 0, 0, 999, 0)],  # a key the layout has not
        [(10**6, 0, 0, 5, 0)],  # a frame past the end
        [(0, 0, 0, 5, 3)],  # displaced is 0 or 1
    ],
)
def test_a_prompt_table_that_does_not_fit_the_file_refuses_the_file(tmp_path, rows):
    path = hover_trace(tmp_path / "t.npz", fps=30, seconds=3)
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    arrays["drill_prompts"] = np.array(rows, dtype=np.int64)
    np.savez_compressed(path, **arrays)
    code, text = replay(path)
    assert code == 2 and len(text.strip().splitlines()) == 1


def test_a_prompt_table_of_fractions_is_not_a_prompt_table(tmp_path):
    path = hover_trace(tmp_path / "t.npz", fps=30, seconds=3)
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    arrays["drill_prompts"] = np.array([(0, 0, 0, 5, 0)], dtype=np.float64)
    np.savez_compressed(path, **arrays)
    assert replay(path)[0] == 2


def test_the_prompt_table_name_and_shape_are_the_ones_keytrace_writes(tmp_path):
    assert keyreplay.PROMPTS_KEY == "drill_prompts"
    made = drill_trace(tmp_path / "t.npz", prompts=6)
    rec = keyreplay.load(made.path)
    assert rec.prompts is not None and rec.prompts.shape == (6, 5)
    assert [tuple(r) for r in rec.prompts.tolist()] == made.prompts


def test_a_trace_without_the_table_has_none(tmp_path):
    made = drill_trace(tmp_path / "t.npz", prompts=6, written_prompts=False)
    assert keyreplay.load(made.path).prompts is None


# ------------------------------------------------------------------------------------- the press method (F16, X50)


def test_an_air_file_runs_the_air_tap_and_a_pinch_file_the_pinch(tmp_path):
    air = keyreplay.analyse(
        keyreplay.load(hover_trace(tmp_path / "a.npz", fps=30, seconds=4, press="air")), Tuning(), suggest=False
    )
    pinch = keyreplay.analyse(keyreplay.load(pinch_trace(tmp_path / "p.npz")), Tuning(), suggest=False)
    assert air["press"] == "air" and air["level"] is not None
    assert pinch["press"] == "pinch" and pinch["level"] is None
    assert pinch["events"] > 0


def test_the_press_argument_overrides_the_file_both_ways(tmp_path):
    air_file = hover_trace(tmp_path / "a.npz", fps=30, seconds=4, press="air")
    pinch_file = pinch_trace(tmp_path / "p.npz")
    assert keyreplay.analyse(keyreplay.load(air_file), Tuning(), "pinch", suggest=False)["press"] == "pinch"
    assert keyreplay.analyse(keyreplay.load(pinch_file), Tuning(), "air", suggest=False)["press"] == "air"
    code, text = replay(pinch_file, "--press", "air", "--no-suggest")
    assert code == 0 and "air tap" in text.lower()


def test_a_version_1_file_has_no_press_and_runs_the_pinch(tmp_path):
    path = pinch_trace(tmp_path / "v1.npz")
    as_version_1(path)
    rec = keyreplay.load(path)
    assert rec.data.version == 1 and rec.data.press == "pinch"
    assert keyreplay.analyse(rec, Tuning(), suggest=False)["press"] == "pinch"
    code, text = replay(path, "--no-suggest")
    assert code == 0 and "pinch" in text.lower()


def test_a_pinch_file_with_a_drill_is_refused_with_one_line_and_so_is_the_pinch_override(tmp_path):
    pinch_file = pinch_trace(tmp_path / "p.npz", drill=True)
    code, text = replay(pinch_file)
    assert code == 2 and len(text.strip().splitlines()) == 1 and "drill" in text and "air" in text
    air_file = drill_trace(tmp_path / "a.npz", prompts=6)
    assert replay(air_file.path, "--press", "pinch")[0] == 2
    assert replay(air_file.path, "--no-suggest")[0] == 0  # the same file with its own method is fine
    # a pinch file whose drill label is overridden to air is the air tap's to replay
    assert replay(pinch_file, "--press", "air", "--no-suggest")[0] == 0


# ------------------------------------------------------------------------------------------------- the report numbers


def test_the_frames_the_clock_and_the_segments_are_those_of_the_file(clean, clean_report):
    assert clean_report["frames"] == clean.frames
    assert clean_report["seconds"] == pytest.approx((clean.frames - 1) / FPS, abs=0.01)
    assert clean_report["fps"] == {"mean": pytest.approx(FPS, abs=0.1), "median": pytest.approx(FPS, abs=0.1)}
    seconds = clean_report["segments"]
    assert list(seconds) == ["place", "drill", "rest", "wave"]
    assert seconds["place"] == pytest.approx(3.0, abs=0.05) and seconds["drill"] == pytest.approx(32 * 1.2, abs=0.05)
    assert seconds["rest"] == pytest.approx(20.0, abs=0.05)


def test_the_plane_is_placed_when_the_hands_have_been_still_and_taps_count_after_that(clean, clean_report):
    assert clean_report["placed"]["at"] == pytest.approx(0.6, abs=0.1)  # still_s
    assert clean_report["placed"]["pitch"] == pytest.approx(0.0555, abs=0.002)


@pytest.mark.parametrize(("onset", "counted"), [(0.9, 0), (1.3, 0), (1.317, 0), (1.35, 1), (2.5, 1)])
def test_a_tap_is_counted_from_the_frame_after_the_one_that_placed_the_plane(tmp_path, onset, counted):
    """The plane is placed at 1.5 s (frame 45). A tap committed on that frame or before it is not a tap of the
    typing: the person has not seen the keyboard yet."""
    path = hover_trace(tmp_path / "t.npz", fps=30, seconds=5, taps=(("left", 0, onset),))
    report = keyreplay.analyse(keyreplay.load(path), Tuning(still_s=1.5), suggest=False)
    assert report["placed"]["at"] == pytest.approx(1.5, abs=0.001) and report["events"] == counted


def test_the_prompts_are_scored_hit_wrong_finger_extra_and_missed(clean_report):
    prompts = clean_report["prompts"]
    assert prompts["total"] == prompts["named"] == 32 and prompts["unnamed"] == 0
    assert prompts["hit"] == 31 and prompts["missed"] == 1
    assert prompts["wrong_finger"] == 1  # left.pinky tapped in a window that names right.middle
    assert prompts["extra"] == 1  # right.ring tapped twice in its window
    assert prompts["recall"] == pytest.approx(31 / 32, abs=0.001)


def test_a_missed_prompt_says_why_and_a_prompt_nobody_tapped_is_no_peak(clean_report):
    assert clean_report["prompts"]["missed_by"] == {"no_peak": 1}


def test_each_finger_has_its_own_prompts_hits_wrong_taps_and_recall(clean_report):
    assert len(clean_report["fingers"]) == 8
    assert [(f["side"], f["name"]) for f in clean_report["fingers"]][:5] == [
        ("left", "index"), ("left", "middle"), ("left", "ring"), ("left", "pinky"), ("right", "index"),
    ]  # fmt: skip
    for f in clean_report["fingers"]:
        assert f["prompts"] == 4 and f["hits"] + f["missed"] == 4
    assert finger(clean_report, "right.pinky")["missed"] == 1 and finger(clean_report, "right.pinky")["recall"] == 0.75
    assert finger(clean_report, "left.pinky")["wrong"] == 1 and finger(clean_report, "left.ring")["wrong"] == 0
    assert sum(f["taps"] for f in clean_report["fingers"]) == clean_report["events"]


def test_the_depth_distribution_and_the_calibrated_numbers_of_each_finger(clean_report):
    assert clean_report["depth_source"] == "prompted taps"
    for f in clean_report["fingers"]:
        d = f["depth"]
        assert d["p10"] <= d["p50"] <= d["p90"] and 0.2 < d["p50"] < 0.8
        assert f["D"] == pytest.approx(d["p50"], abs=0.1) and 0.10 <= f["D"] <= 0.80
        assert 0.008 <= f["sigma"] < 0.05 and 0.10 <= f["theta"] <= 0.25
        assert 50 <= f["latency_ms"] <= 400
    assert 50 <= clean_report["latency_ms"] <= 400


def test_the_reject_histogram_is_the_methods_own_counters_most_common_first(clean_report):
    rejects = clean_report["rejects"]
    assert rejects and all(isinstance(v, int) and v > 0 for v in rejects.values())
    counts = list(rejects.values())
    assert counts == sorted(counts, reverse=True)
    assert set(rejects) <= {"plateau", "narrow", "wide", "motion", "pinched", "veto", "excl", "coherence_raw", "tremor"}


def test_phantom_taps_in_rest_are_counted_per_minute_and_split_by_finger(clean_report):
    rest = clean_report["phantoms"]["rest"]
    assert rest["taps"] == 3 and rest["by_finger"] == {"left.index": 2, "right.ring": 1}
    assert rest["seconds"] == pytest.approx(20.0, abs=0.1) and rest["per_min"] == pytest.approx(9.0, abs=0.1)


def test_a_wave_with_no_tap_is_a_phantom_free_stretch_and_a_trace_without_one_has_no_line(clean_report, tmp_path):
    wave = clean_report["phantoms"]["wave"]
    assert wave["taps"] == 0 and wave["per_min"] == 0.0 and wave["by_finger"] == {}
    plain = keyreplay.analyse(keyreplay.load(drill_trace(tmp_path / "t.npz", prompts=4).path), Tuning(), suggest=False)
    assert set(plain["phantoms"]) == {"wave"}  # the made trace ends in a second of wave and has no rest


def test_taps_that_fall_outside_every_prompt_window_of_a_drill_are_not_hits(clean):
    """A tap in the second after the last window is stray, not a hit and not a wrong finger."""
    rec = keyreplay.load(clean.path)
    analysis = keyreplay.Analysis(rec, Tuning())
    assert all(t.kind in ("drill", "rest", "wave") for t in analysis.taps)


def test_the_made_trace_is_read_back_whole_one_tap_after_another_by_the_scripted_fingers(clean):
    analysis = keyreplay.Analysis(keyreplay.load(clean.path), Tuning())
    detected = [(t.side, t.finger, t.t - T0) for t in analysis.taps]
    for side, f, at in clean.taps:
        near = [d for d in detected if d[:2] == (side, f) and at - 0.1 <= d[2] <= at + 0.6]
        assert near, (side, f, at)


def test_the_taps_are_what_the_detector_gives_on_the_recorded_frames(clean):
    """The replay adds nothing to the press method: the same samples through a bare ``AirTapPress``, the same taps."""
    from jarvis_hands.keyboard.hands import HandTracker
    from jarvis_hands.keyboard.press_air import AirTapPress

    rec = keyreplay.load(clean.path)
    analysis = keyreplay.Analysis(rec, Tuning())
    tracker, press = HandTracker(Tuning()), AirTapPress(Tuning())
    for (side, f), depth in analysis.depths.items():
        press.set_finger(side, f, depth)
    direct = [(e.t, e.side, e.finger) for frame in rec.data.frames() for e in press.update(tracker.update(frame))]
    counted = [(t.t, t.side, t.finger) for t in analysis.taps]
    assert counted == [d for d in direct if d[0] > analysis.placed_t]
    assert len(counted) > 30


def test_the_replay_is_deterministic(clean):
    one = keyreplay.analyse(keyreplay.load(clean.path), Tuning(), suggest=False)
    two = keyreplay.analyse(keyreplay.load(clean.path), Tuning(), suggest=False)
    assert one == two


# ------------------------------------------------------------------------------------------- the prompts, two ways


def test_a_file_without_the_table_gets_its_prompts_from_the_drill_targets_and_scores_the_same(tmp_path):
    with_table = drill_trace(tmp_path / "a.npz", prompts=16, skip=(5,))
    without = drill_trace(tmp_path / "b.npz", prompts=16, skip=(5,), written_prompts=False)
    a = keyreplay.analyse(keyreplay.load(with_table.path), Tuning(), suggest=False)
    b = keyreplay.analyse(keyreplay.load(without.path), Tuning(), suggest=False)
    assert a["prompts"] == b["prompts"] and a["fingers"] == b["fingers"]
    assert b["prompts"]["total"] == 16 and b["prompts"]["missed"] == 1


def test_a_displaced_key_names_no_finger_in_a_practice_trace_and_is_left_out_of_the_recall(tmp_path):
    made = drill_trace(tmp_path / "d.npz", prompts=16, displaced=True, written_prompts=False)
    report = keyreplay.analyse(keyreplay.load(made.path), Tuning(), suggest=False)
    prompts = report["prompts"]
    assert prompts["total"] == 16 and prompts["unnamed"] == 8 and prompts["named"] == 8
    assert prompts["hit"] + prompts["missed"] == 8
    assert sum(f["prompts"] for f in report["fingers"]) == 8


def test_a_displaced_key_with_the_table_names_its_finger(tmp_path):
    made = drill_trace(tmp_path / "d.npz", prompts=16, displaced=True)
    report = keyreplay.analyse(keyreplay.load(made.path), Tuning(), suggest=False)
    assert report["prompts"]["unnamed"] == 0 and report["prompts"]["named"] == 16
    assert sum(f["prompts"] for f in report["fingers"]) == 16


# --------------------------------------------------------------------------------------------------------- the aim


@pytest.fixture(scope="module")
def moving(tmp_path_factory) -> Made:
    """Index and middle fingers over displaced keys, the hand still moving when the tap begins."""
    return drill_trace(
        tmp_path_factory.mktemp("moving") / "m.npz", prompts=24, fingers=(0, 1), displaced=True, lead_s=0.0
    )


def test_the_aim_table_has_the_four_rules_each_split_by_the_hand_speed_at_the_left_base(moving):
    report = keyreplay.analyse(keyreplay.load(moving.path), Tuning(), suggest=False)
    aim = report["aim"]
    assert list(aim["rules"]) == ["onset", "commit", "auto", "peak"] and aim["n"] == 24
    assert aim["aim_speed"] == Tuning().air_aim_speed
    sizes = {tuple(r["still"][1:]) + tuple(r["moving"][1:]) for r in aim["rules"].values()}
    assert len(sizes) == 1  # every rule is scored on the same taps
    still, moving_n = aim["rules"]["onset"]["still"][1], aim["rules"]["onset"]["moving"][1]
    assert still > 0 and moving_n > 0 and still + moving_n == 24
    for row in aim["rules"].values():
        assert row["all"] == [row["still"][0] + row["moving"][0], 24]
        assert row["accuracy"] == pytest.approx(row["all"][0] / 24, abs=0.001)


def test_the_split_follows_the_aim_speed_that_was_set(moving):
    slow = keyreplay.analyse(keyreplay.load(moving.path), Tuning(air_aim_speed=0.02), suggest=False)["aim"]
    fast = keyreplay.analyse(keyreplay.load(moving.path), Tuning(air_aim_speed=0.15), suggest=False)["aim"]
    assert slow["rules"]["onset"]["moving"][1] >= fast["rules"]["onset"]["moving"][1]
    assert (
        slow["rules"]["onset"]["moving"][1] > fast["rules"]["onset"]["moving"][1]
        or slow["rules"]["onset"]["moving"][1] == 0
    )


def test_the_peak_names_the_wrong_key_far_more_often_than_the_aim_before_the_dip(moving):
    rules = keyreplay.analyse(keyreplay.load(moving.path), Tuning(), suggest=False)["aim"]["rules"]
    assert rules["commit"]["accuracy"] >= 0.9 and rules["onset"]["accuracy"] >= 0.85
    assert rules["peak"]["accuracy"] <= rules["onset"]["accuracy"] - 0.2


@pytest.mark.parametrize("rule", ["onset", "commit"])
def test_the_aim_a_rule_gives_is_the_aim_the_press_method_gave_when_that_rule_was_set(moving, rule):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning(air_aim=rule))
    assert len(analysis.taps) >= 20
    for tap in analysis.taps:
        assert tap.aim == pytest.approx(tap.rules[rule], abs=1e-9), rule


def test_the_aim_of_the_auto_rule_is_the_commit_aim_when_the_hand_moved_and_the_onset_aim_when_it_did_not(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning())
    speed = Tuning().air_aim_speed
    checked = {"commit": 0, "onset": 0}
    for tap in analysis.taps:
        if abs(tap.vl - speed) < 0.001:
            continue  # the tap log rounds the speed to three places
        rule = "commit" if tap.vl > speed else "onset"
        checked[rule] += 1
        assert tap.aim == pytest.approx(tap.rules[rule], abs=1e-9)
    assert checked["commit"] > 0 and checked["onset"] > 0


# ------------------------------------------------------------------------------------------- noise, camera, warm-up


@pytest.mark.parametrize(("sigma_xy", "sigma_z"), [(0.002, 0.005), (0.001, 0.02), (0.004, 0.012)])
def test_the_landmark_noise_of_a_still_hand_is_estimated_to_within_a_sixth(tmp_path, sigma_xy, sigma_z):
    report = keyreplay.analyse(
        keyreplay.load(noisy_hand_trace(tmp_path / "n.npz", sigma_xy=sigma_xy, sigma_z=sigma_z)),
        Tuning(),
        suggest=False,
    )
    jitter = report["jitter"]
    assert jitter["xy"] == pytest.approx(sigma_xy, rel=0.17) and jitter["z"] == pytest.approx(sigma_z, rel=0.17)
    assert jitter["from"] == "rest" and jitter["frames"] > 200


def test_the_noise_warning_is_on_above_a_z_of_0_015_and_not_below(tmp_path):
    loud = keyreplay.analyse(
        keyreplay.load(noisy_hand_trace(tmp_path / "a.npz", sigma_xy=0.001, sigma_z=0.02)), Tuning(), suggest=False
    )
    quiet = keyreplay.analyse(
        keyreplay.load(noisy_hand_trace(tmp_path / "b.npz", sigma_xy=0.001, sigma_z=0.01)), Tuning(), suggest=False
    )
    assert loud["jitter"]["warn"] is True and quiet["jitter"]["warn"] is False
    text = "\n".join(keyreplay.render(loud))
    assert "WARNING" in text and "0.015" in text
    assert "WARNING" not in "\n".join(keyreplay.render(quiet))


def test_a_slow_drift_does_not_count_as_noise(tmp_path):
    """A hand sliding steadily across the picture has no jitter: the second difference removes the slide."""
    base = syn.hand("palm", (0.3, 0.5), handedness="right")
    writer = TraceWriter(press="air")
    for n in range(300):
        image = base.image.copy()
        image[:, 0] += 0.0005 * n
        writer.add(Frame(T0 + n / FPS, (HandObservation("right", 0.95, image, base.world),), 1280, 720), "rest")
    report = keyreplay.analyse(keyreplay.load(writer.save(tmp_path / "d.npz")), Tuning(), suggest=False)
    assert report["jitter"]["xy"] < 0.0005 and report["jitter"]["z"] < 0.0005


def test_the_noise_comes_from_the_place_when_there_is_no_rest_and_is_absent_with_neither(tmp_path):
    from_place = keyreplay.analyse(
        keyreplay.load(noisy_hand_trace(tmp_path / "p.npz", sigma_xy=0.002, sigma_z=0.005, kind="place")),
        Tuning(),
        suggest=False,
    )
    assert from_place["jitter"]["from"] == "place" and from_place["jitter"]["xy"] > 0
    none = keyreplay.analyse(
        keyreplay.load(noisy_hand_trace(tmp_path / "w.npz", sigma_xy=0.002, sigma_z=0.005, kind="wave")),
        Tuning(),
        suggest=False,
    )
    assert none["jitter"] == {"xy": None, "z": None, "frames": 0, "from": "", "warn": False}
    assert "no still stretch" in "\n".join(keyreplay.render(none))


@pytest.mark.parametrize(("fps", "level"), [(30, "ok"), (20, "degraded"), (10, "off")])
def test_the_ladder_level_the_camera_would_have_is_the_one_the_frame_rate_implies(tmp_path, fps, level):
    report = keyreplay.analyse(
        keyreplay.load(hover_trace(tmp_path / "h.npz", fps=fps, seconds=20)), Tuning(), suggest=False
    )
    assert report["level"]["name"] == level and report["level"]["fps"] == pytest.approx(fps, abs=1.0)
    assert (report["level"]["off_at"] is not None) is (level == "off")
    if level != "ok":
        assert report["level"]["reason"] == "fps"


def test_a_degraded_camera_makes_the_press_stricter_as_it_does_in_a_session(tmp_path):
    """Noise past the ladder's limit sets the strict level: every threshold times 1.3, fewer taps than without it."""
    path = drill_trace(tmp_path / "n.npz", prompts=24, sigma=0.002).path
    strict = keyreplay.Analysis(keyreplay.load(path), Tuning())
    assert strict.report(suggest=False)["level"]["name"] in ("degraded", "off")
    assert strict.run.ladder is not None and strict.run.ladder.strict


def test_a_practice_trace_warm_up_sets_the_finger_depths_and_nothing_is_guessed(tmp_path):
    made = drill_trace(tmp_path / "w.npz", prompts=16, warm=True, written_prompts=False)
    report = keyreplay.analyse(keyreplay.load(made.path), Tuning(), suggest=False)
    assert report["depth_source"] == "warm-up"
    assert all(f["D"] is not None for f in report["fingers"])
    assert report["segments"]["warm"] == pytest.approx(17.0, abs=0.1)
    assert report["prompts"]["total"] == 16
    # the warm-up taps are not typing: they are not among the counted taps
    assert report["events"] == sum(f["taps"] for f in report["fingers"]) and report["events"] <= 16 + 2


# ----------------------------------------------------------------------------------------------------- the suggestions

SEARCHED = {"air_theta_k", "air_theta_min", "air_depth_frac", "air_vmax_gate", "air_aim", "air_aim_speed"}


@pytest.fixture(scope="module")
def weak(tmp_path_factory) -> Made:
    """Taps with a third of the usual strength: the shipped numbers find a third of the prompts."""
    return drill_trace(tmp_path_factory.mktemp("weak") / "w.npz", prompts=24, amp=12.0)


@pytest.fixture(scope="module")
def weak_analysis(weak: Made) -> keyreplay.Analysis:
    return keyreplay.Analysis(keyreplay.load(weak.path), Tuning())


@pytest.fixture(scope="module")
def weak_report(weak_analysis: keyreplay.Analysis) -> dict[str, Any]:
    return weak_analysis.report(suggest=True)


@pytest.fixture(scope="module")
def clean_suggested(clean: Made) -> dict[str, Any]:
    return keyreplay.analyse(keyreplay.load(clean.path), Tuning(), suggest=True)


def false_share(report: dict[str, Any]) -> float:
    """Wrong-finger, extra and phantom taps over all counted taps, from the numbers a report shows."""
    bad = report["prompts"]["wrong_finger"] + report["prompts"]["extra"]
    bad += sum(block["taps"] for block in report["phantoms"].values())
    return bad / report["events"] if report["events"] else 0.0


def test_weak_taps_get_values_that_find_more_prompts_and_no_false_taps_past_the_limit(weak_report):
    suggest = weak_report["suggest"]
    assert suggest["values"] and suggest["notes"] == []
    assert suggest["base_recall"] == pytest.approx(weak_report["prompts"]["recall"], abs=0.001)
    assert suggest["recall"] > suggest["base_recall"] + 0.25
    assert suggest["false_rate"] <= keyreplay.FALSE_RATE_LIMIT == 0.05
    assert set(suggest["values"]) <= SEARCHED


def test_the_numbers_said_for_the_suggested_values_are_what_a_replay_with_them_gives(weak, weak_report):
    """The search holds the finger depths fixed; what is reported is the whole replay, as ``--set`` would run it."""
    suggest = weak_report["suggest"]
    again = keyreplay.analyse(keyreplay.load(weak.path), replace(Tuning(), **suggest["values"]), suggest=False)
    assert again["prompts"]["recall"] == pytest.approx(suggest["recall"], abs=0.001)
    assert false_share(again) == pytest.approx(suggest["false_rate"], abs=0.001)


def test_the_false_tap_share_counts_wrong_extra_and_phantom_taps_over_all_taps(clean_suggested):
    assert clean_suggested["suggest"]["base_false_rate"] == pytest.approx(false_share(clean_suggested), abs=0.001)
    assert clean_suggested["suggest"]["base_false_rate"] > keyreplay.FALSE_RATE_LIMIT


def test_a_recording_the_shipped_numbers_already_handle_gets_no_values(clean_suggested):
    suggest = clean_suggested["suggest"]
    assert suggest["values"] == {} and suggest["recall"] == suggest["base_recall"]


def test_every_value_that_can_be_suggested_is_a_tuning_field_inside_its_clamps(weak_report, clean_suggested):
    for name, candidates in keyreplay._CANDIDATES.items():  # the grid the search walks, whatever it finds
        low, high = RANGES[name]
        assert all(low <= value <= high for value in candidates), name
    assert "air_veto_ratio" not in keyreplay._CANDIDATES and "air_veto_ratio" not in SEARCHED
    for report in (weak_report, clean_suggested):
        for name, value in report["suggest"]["values"].items():
            assert name in keyreplay.TUNABLE
            if name == "air_aim":
                assert value in AIR_AIM_CHOICES
            else:
                assert RANGES[name][0] <= value <= RANGES[name][1], name
    values = weak_report["suggest"]["values"]
    assert values.get("air_theta_k", 4.0) >= 4.0 and values.get("air_theta_min", 0.08) >= 0.08


def test_no_candidate_is_below_the_floors_the_safety_review_set():
    assert min(keyreplay._CANDIDATES["air_theta_k"]) >= 4.0 and min(keyreplay._CANDIDATES["air_theta_min"]) >= 0.08


def test_a_value_that_makes_the_press_fire_on_nothing_is_never_better(tmp_path):
    """Noise on the landmarks and weak taps: a very high threshold has no false taps because it has no taps."""
    path = drill_trace(tmp_path / "n.npz", prompts=24, amp=12.0, sigma=0.002).path
    suggest = keyreplay.analyse(keyreplay.load(path), Tuning(), suggest=True)["suggest"]
    assert suggest["base_recall"] > 0
    assert suggest["recall"] >= suggest["base_recall"] and suggest["values"].get("air_theta_k", 4.0) < 6.0
    assert keyreplay.Analysis._better((0.0, 0.0), (0.125, 0.333)) is False
    assert keyreplay.Analysis._better((0.2, 0.2), (0.125, 0.333)) is True


@pytest.mark.parametrize(
    ("new", "old", "better"),
    [
        ((0.7, 0.0), (0.4, 0.0), True),  # more found, none false
        ((0.4, 0.0), (0.7, 0.0), False),
        ((0.7, 0.04), (0.4, 0.0), True),  # a few false taps within the limit do not outweigh finding more
        ((0.9, 0.2), (0.4, 0.0), False),  # past the limit nothing is worth the false taps
        ((0.4, 0.0), (0.9, 0.2), True),  # back inside the limit beats a better recall past it
        ((0.5, 0.2), (0.9, 0.3), True),  # both past the limit: fewer false taps win
        ((0.9, 0.3), (0.5, 0.2), False),
        ((0.5, 0.01), (0.5, 0.03), True),  # equal recall: fewer false taps
        ((0.5, 0.03), (0.5, 0.03), False),  # a tie changes nothing
        ((0.5, 0.2), (0.5, 0.2), False),  # also past the limit
    ],
)
def test_the_objective_is_recall_within_a_false_tap_limit_and_fewer_false_taps_past_it(new, old, better):
    assert keyreplay.Analysis._better(new, old) is better


def test_pinned_names_are_not_searched_and_the_others_still_are(weak_analysis, weak_report):
    pinned = weak_analysis.report(suggest=True, pinned=("air_theta_min",))["suggest"]
    assert "air_theta_min" not in pinned["values"]
    assert "air_theta_min" in weak_report["suggest"]["values"]  # the control: it is what the unpinned search found
    everything = weak_analysis.report(suggest=True, pinned=tuple(SEARCHED))["suggest"]
    assert everything["values"] == {} and everything["recall"] == everything["base_recall"]


def test_fewer_than_twenty_prompts_give_no_values_and_say_why(tmp_path):
    path = drill_trace(tmp_path / "f.npz", prompts=12).path
    suggest = keyreplay.analyse(keyreplay.load(path), Tuning(), suggest=True)["suggest"]
    assert suggest["values"] == {} and "recall" not in suggest
    assert len(suggest["notes"]) == 1 and "20 prompts" in suggest["notes"][0]
    assert keyreplay.MIN_PROMPTS_TO_SUGGEST == 20


def test_no_suggest_leaves_the_block_out(clean):
    assert keyreplay.analyse(keyreplay.load(clean.path), Tuning(), suggest=False)["suggest"] is None


def test_the_tuning_a_file_asks_for_is_the_base_of_the_search(weak):
    """Starting from the better numbers, there is nothing left for the search to add."""
    better = replace(Tuning(), air_theta_min=0.08)
    suggest = keyreplay.analyse(keyreplay.load(weak.path), better, suggest=True)["suggest"]
    assert suggest["base_recall"] > 0.6 and "air_theta_min" not in suggest["values"]


def corrupt_onset_of_moving_taps(analysis: keyreplay.Analysis) -> None:
    """The aim the onset rule gave is made a key and a half off wherever the hand was moving (the table is the data)."""
    speed = analysis.tuning.air_aim_speed
    for tap in analysis.taps:
        if tap.vl > speed:
            u, v = tap.rules["onset"]
            tap.rules["onset"] = (u + 1.5, v)


def test_the_aim_rule_is_changed_when_another_names_more_keys_and_left_alone_on_a_tie(moving):
    rec = keyreplay.load(moving.path)
    always = keyreplay.Analysis(rec, Tuning(air_aim="onset"))
    assert always._aim_choice(frozenset()) == {}  # onset names every key right, and so do the others: a tie
    corrupt_onset_of_moving_taps(always)
    assert always._aim_choice(frozenset()) == {"air_aim": "auto"}  # still taps by onset, moving taps by commit
    assert always._aim_choice(frozenset({"air_aim"})) == {}  # not what was asked to be left alone
    already = keyreplay.Analysis(rec, Tuning(air_aim="commit"))
    corrupt_onset_of_moving_taps(already)
    assert already._aim_choice(frozenset()) == {}  # commit is as good as auto here: no change


def test_the_aim_speed_moves_when_the_hand_that_the_onset_got_wrong_was_slower_than_the_limit(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning(air_aim="auto", air_aim_speed=0.15))
    speeds = sorted({t.vl for t in analysis.taps})
    assert speeds[0] < 0.15 < speeds[-1]
    for tap in analysis.taps:
        if 0.03 < tap.vl <= 0.15:  # such a tap is aimed by onset at 0.15; make that wrong
            u, v = tap.rules["onset"]
            tap.rules["onset"] = (u + 1.5, v)
    choice = analysis._aim_choice(frozenset())
    assert choice["air_aim_speed"] < 0.15
    assert "air_aim_speed" not in analysis._aim_choice(frozenset({"air_aim_speed"}))


@pytest.fixture(scope="module")
def pinch_report(tmp_path_factory) -> dict[str, Any]:
    path = pinch_trace(tmp_path_factory.mktemp("pinch") / "p.npz")
    return keyreplay.analyse(keyreplay.load(path), Tuning(), suggest=True)


def test_a_pinch_recording_gets_the_levels_and_the_close_threshold_and_never_an_air_value(pinch_report):
    suggest = pinch_report["suggest"]
    assert set(suggest["values"]) <= {"pitch", "level_palm", "close"} and suggest["values"]
    assert not set(suggest["values"]) & SEARCHED and "recall" not in suggest
    low, high = LEVEL_PALM_RANGE
    assert all(low <= x <= high for x in suggest["values"]["level_palm"])
    assert RANGES["close"][0] <= suggest["values"]["close"] <= RANGES["close"][1]


def test_a_pinned_pinch_value_is_not_suggested(tmp_path):
    path = pinch_trace(tmp_path / "p.npz")
    analysis = keyreplay.Analysis(keyreplay.load(path), Tuning())
    suggest = analysis.report(suggest=True, pinned=("close", "level_palm", "pitch"))["suggest"]
    assert suggest["values"] == {}


# ------------------------------------------------------------------------------------------------------------ the run


def lines_of(text: str) -> list[str]:
    return text.strip().splitlines()


def taps_counted(text: str) -> int:
    return int(next(line for line in lines_of(text) if line.startswith("taps counted:")).split()[2].rstrip(","))


def test_a_run_prints_the_report_and_exits_zero(clean):
    code, text = replay(clean.path, "--no-suggest")
    assert code == keyreplay.EXIT_OK == 0
    assert lines_of(text)[0].startswith("keyreplay: the air tap, ")
    assert "drill: 32 prompts" in text and "finger " in text and "suggested" not in text
    assert "settings tried" not in text


def test_the_run_says_what_it_is_doing_while_the_suggestions_are_searched(weak):
    code, text = replay(weak.path)
    assert code == 0 and "Looking for better values" in text and "suggested: " in text
    assert text.index("Looking for better values") < text.index("keyreplay: the air tap")
    assert "with the suggested values: recall " in text


def test_a_set_is_the_tuning_of_the_replay_and_is_said_in_the_report(tmp_path):
    path = drill_trace(tmp_path / "n.npz", prompts=24, sigma=0.002).path
    _, plain = replay(path, "--no-suggest")
    code, text = replay(path, "--no-suggest", "--set", "air_theta_k=6.5", "--set", "air_theta_min=0.2")
    assert code == 0 and "settings tried: air_theta_k=6.5, air_theta_min=0.2" in text
    assert taps_counted(text) < taps_counted(plain)


def test_the_tuning_file_of_the_data_dir_is_the_base_and_a_set_overrides_it(weak, tmp_path):
    keyreplay.write_tuning(tuning_path(tmp_path), {"air_theta_min": 0.08})
    _, shipped = replay(weak.path, "--no-suggest")
    _, from_file = replay(weak.path, "--no-suggest", data_dir=tmp_path)
    _, overridden = replay(weak.path, "--no-suggest", "--set", "air_theta_min=0.2", data_dir=tmp_path)
    assert taps_counted(from_file) > taps_counted(shipped) and taps_counted(overridden) < taps_counted(from_file)
    _, again = replay(weak.path, "--no-suggest", "--set", "air_theta_min=0.08")
    assert taps_counted(again) == taps_counted(from_file)


@pytest.mark.parametrize(
    "bad",
    [
        ["--set", "bogus=1"],
        ["--set", "air_theta_k=99"],
        ["--set", "air_theta_k"],
        ["--set", "close=0.9", "--set", "open_=0.5"],
    ],
)
def test_a_refused_set_exits_two_with_one_line_and_runs_nothing(clean, tmp_path, bad):
    code, text = replay(clean.path, *bad, "--write", data_dir=tmp_path)
    assert code == keyreplay.EXIT_REFUSED == 2 and len(lines_of(text)) == 1
    assert "99" not in text and not tuning_path(tmp_path).exists()


def test_a_file_that_is_refused_writes_nothing_even_with_write(tmp_path):
    (tmp_path / "bad.npz").write_bytes(b"not a trace")
    code, text = replay(tmp_path / "bad.npz", "--write", "--set", "air_theta_k=6.0", data_dir=tmp_path)
    assert code == 2 and len(lines_of(text)) == 1 and not tuning_path(tmp_path).exists()


# --csv


def test_the_csv_has_one_row_for_each_hand_in_each_frame_and_the_numbers_of_the_hand(clean, tmp_path):
    target = tmp_path / "out.csv"
    code, text = replay(clean.path, "--no-suggest", "--csv", str(target))
    assert code == 0 and "Wrote the per-frame numbers" in text
    rows = target.read_text(encoding="utf-8").splitlines()
    header = (
        "t,segment,side," + ",".join(f"ratio_{i}" for i in range(4)) + "," + ",".join(f"lift_{i}" for i in range(4))
    )
    assert rows[0] == header + ",speed"
    analysis = keyreplay.Analysis(keyreplay.load(clean.path), Tuning())
    assert len(rows) - 1 == sum(len(frame) for frame in analysis.hands.samples)
    first = rows[1].split(",")
    assert float(first[0]) == 0.0 and first[1] == "place" and first[2] in ("left", "right") and len(first) == 12
    assert {row.split(",")[1] for row in rows[1:]} == {"place", "drill", "rest", "wave"}


def test_the_csv_holds_times_kinds_sides_and_numbers_and_nothing_else(clean, tmp_path):
    target = tmp_path / "out.csv"
    replay(clean.path, "--no-suggest", "--csv", str(target))
    for row in target.read_text(encoding="utf-8").splitlines()[1:]:
        cells = row.split(",")
        float(cells[0])
        assert cells[1] in {"place", "drill", "rest", "wave"} and cells[2] in ("left", "right")
        for cell in cells[3:]:
            float(cell)


@pytest.mark.parametrize("name", ["out.txt", "out", "out.CSV"])
def test_a_csv_name_must_end_in_csv(clean, tmp_path, name):
    code, text = replay(clean.path, "--csv", str(tmp_path / name))
    assert code == 2 and len(lines_of(text)) == 1 and not (tmp_path / name).exists()


def test_an_existing_csv_is_never_overwritten(clean, tmp_path):
    target = tmp_path / "out.csv"
    target.write_text("keep me", encoding="utf-8")
    code, text = replay(clean.path, "--csv", str(target))
    assert code == 2 and len(lines_of(text)) == 1 and target.read_text(encoding="utf-8") == "keep me"


def test_a_csv_that_cannot_be_written_exits_one_and_leaves_no_file(clean, tmp_path, monkeypatch):
    target = tmp_path / "out.csv"

    def refuse(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(keyreplay.os, "replace", refuse)
    code, text = replay(clean.path, "--no-suggest", "--csv", str(target))
    assert code == keyreplay.EXIT_FAILED == 1 and "could not be written" in text
    assert list(tmp_path.glob("out*")) == []
    assert "keyreplay: the air tap" in text  # the report was already printed


# --write


@pytest.fixture(scope="module")
def weak_values(weak_report) -> dict[str, Any]:
    return dict(weak_report["suggest"]["values"])


def test_write_puts_the_suggested_values_in_the_tuning_file_where_the_loader_finds_them(weak, weak_values, tmp_path):
    code, text = replay(weak.path, "--write", data_dir=tmp_path)
    assert code == 0 and "Wrote to the tuning file: " in text
    loaded = load_tuning(tmp_path)
    for name, value in weak_values.items():
        assert getattr(loaded, name) == value, name
    assert replace(Tuning(), **weak_values) == loaded
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [tuning_path(tmp_path).name]  # no temporary left


@pytest.fixture
def known_suggestion(monkeypatch, weak_report):
    """What the search found on the weak recording, given without searching again (the tests of ``--write`` do not
    test the search)."""
    found = weak_report["suggest"]

    def suggest(self, pinned=()):
        return {**found, "values": {k: v for k, v in found["values"].items() if k not in pinned}}

    monkeypatch.setattr(keyreplay.Analysis, "suggest", suggest)
    return found


def test_write_keeps_what_the_file_has_for_other_groups_and_settings(weak, tmp_path, known_suggestion):
    path = tuning_path(tmp_path)
    keyreplay.write_tuning(path, {"pitch": 0.06, "air_vmax_gate": 1.0})
    before = json.loads(path.read_text(encoding="utf-8"))
    code, _ = replay(weak.path, "--write", "--set", "air_vmax_gate=0.5", data_dir=tmp_path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert code == 0 and load_tuning(tmp_path).pitch == 0.06 and load_tuning(tmp_path).air_vmax_gate == 0.5
    assert after["version"] == before["version"] == 1 and after != before


def test_write_also_writes_what_was_set_by_hand_and_a_set_value_is_not_searched(weak, tmp_path):
    code, text = replay(weak.path, "--write", "--set", "air_theta_min=0.12", data_dir=tmp_path)
    assert code == 0 and "air_theta_min=0.12" in text.splitlines()[-1]
    assert load_tuning(tmp_path).air_theta_min == 0.12


def test_write_with_nothing_to_say_changes_nothing_and_exits_zero(moving, tmp_path):
    code, text = replay(moving.path, "--write", data_dir=tmp_path)
    assert code == 0 and "Nothing to write" in text and not tuning_path(tmp_path).exists()
    code, text = replay(moving.path, "--write", "--no-suggest", data_dir=tmp_path)
    assert code == 0 and "Nothing to write" in text and not tuning_path(tmp_path).exists()


def test_write_over_a_file_it_cannot_read_exits_one_and_leaves_the_file_as_it_was(weak, tmp_path, known_suggestion):
    path = tuning_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    code, text = replay(weak.path, "--write", data_dir=tmp_path)
    assert code == keyreplay.EXIT_FAILED == 1 and path.read_text(encoding="utf-8") == "{ not json"
    assert "keyreplay: the air tap" in text and [p.name for p in tmp_path.rglob("*") if p.is_file()] == [path.name]


def test_write_clamps_a_suggestion_that_is_out_of_range_instead_of_writing_it(clean, tmp_path, monkeypatch):
    """Whatever the search might one day return, the file never holds a value below a floor."""

    def reckless(self, pinned=()):
        values = {"air_theta_k": 0.5, "air_theta_min": 0.0, "air_veto_ratio": 0.01, "air_depth_frac": 9.0}
        return {
            "values": values,
            "notes": [],
            "base_recall": 0.0,
            "base_false_rate": 0.0,
            "recall": 0.0,
            "false_rate": 0.0,
        }

    monkeypatch.setattr(keyreplay.Analysis, "suggest", reckless)
    code, _ = replay(clean.path, "--write", data_dir=tmp_path)
    document = json.loads(tuning_path(tmp_path).read_text(encoding="utf-8"))
    flat = {name: value for group in document.values() if isinstance(group, dict) for name, value in group.items()}
    assert code == 0
    assert (
        flat["air_theta_k"] == RANGES["air_theta_k"][0] == 4.0 and flat["air_theta_min"] == RANGES["air_theta_min"][0]
    )
    assert (
        flat["air_veto_ratio"] == RANGES["air_veto_ratio"][0] and flat["air_depth_frac"] == RANGES["air_depth_frac"][1]
    )
    loaded = load_tuning(tmp_path)
    assert loaded.air_theta_k >= 4.0 and loaded.air_theta_min >= 0.08 and loaded.air_veto_ratio >= 0.5


def test_write_of_a_pinch_recording_writes_the_levels_and_the_close_threshold(tmp_path):
    path = pinch_trace(tmp_path / "p.npz")
    data = tmp_path / "data"
    data.mkdir()
    code, _ = replay(path, "--write", data_dir=data)
    loaded = load_tuning(data)
    assert code == 0 and loaded.close == 0.22 and loaded != Tuning()
    assert all(-0.30 <= x <= 0.30 for x in loaded.level_palm)


# ------------------------------------------------------------------------------------- phrases of a practice trace


def phrase_report(path: Path) -> dict[str, Any]:
    return keyreplay.analyse(keyreplay.load(path), Tuning(), suggest=False)


def ask_for_another_key(path: Path, *, from_second: float, to_second: float | None = None, key: int) -> None:
    """The file as if the script had asked for ``key`` (-1: nothing) during the seconds given."""
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    seconds = arrays["t"] - arrays["t"][0]
    window = (seconds >= from_second) & (seconds < (to_second if to_second is not None else 1e9))
    arrays["targets"] = np.where(window & (arrays["targets"] >= 0), key, arrays["targets"]).astype(np.int16)
    np.savez_compressed(path, **arrays)


def test_taps_in_a_phrase_are_scored_by_the_key_the_script_asked_for_as_a_drill_tap_is_by_its_prompt(tmp_path):
    """The same taps, once called a phrase and once a drill: the two paths name the same number of right keys."""
    phrase = phrase_report(drill_trace(tmp_path / "p.npz", prompts=16, drill_kind="phrase", written_prompts=False).path)
    drill = phrase_report(drill_trace(tmp_path / "d.npz", prompts=16, written_prompts=False).path)
    assert phrase["prompts"]["total"] == 0 and phrase["prompts"]["recall"] is None  # a phrase is no prompt
    assert phrase["phrase"]["taps"] == phrase["phrase"]["scored"] == 16
    assert 0 < phrase["phrase"]["right"] < 16  # the made hands are not perfect on the ring and little fingers
    assert phrase["phrase"]["right"] == drill["aim"]["rules"]["auto"]["all"][0]
    assert phrase["phrase"]["accuracy"] == round(phrase["phrase"]["right"] / 16, 3)
    assert "phrases: 16 taps, " in "\n".join(keyreplay.render(phrase)) and drill["phrase"] is None


def test_a_phrase_tap_is_wrong_when_the_script_asked_for_another_key_and_unscored_when_it_asked_for_none(tmp_path):
    path = drill_trace(tmp_path / "p.npz", prompts=16, fingers=(0, 1), drill_kind="phrase", written_prompts=False).path
    before = phrase_report(path)["phrase"]
    assert before["right"] == 15 and before["scored"] == 16
    ask_for_another_key(path, from_second=PLACE_S + 8 * AIR_DRILL_GAP_S, key=REVIEW.find(char="q").index)
    elsewhere = phrase_report(path)
    assert elsewhere["phrase"]["scored"] == 16 and 7 <= elsewhere["phrase"]["right"] <= 8
    assert f"{elsewhere['phrase']['right']} of 16 on the asked key" in "\n".join(keyreplay.render(elsewhere))
    ask_for_another_key(path, from_second=PLACE_S + 12 * AIR_DRILL_GAP_S, key=-1)
    none = phrase_report(path)["phrase"]
    assert none["taps"] == 16 and none["scored"] == 12 and none["right"] <= 8


def test_a_trace_with_no_phrase_has_no_phrase_line(clean_report):
    assert clean_report["phrase"] is None and "phrases:" not in "\n".join(keyreplay.render(clean_report))


# --------------------------------------------------------------------------------- the pinch and the render of a report


def test_a_pinch_report_shows_taps_and_latency_per_finger_and_the_warm_up_thresholds(pinch_report):
    text = "\n".join(keyreplay.render(pinch_report))
    assert text.startswith("keyreplay: the pinch, ")
    assert "finger         taps  ms" in text and "depth" not in text and "recall" not in text
    assert len(pinch_report["pinch"]["warm"]) == 8 and "warm-up left.index: r_min " in text
    for line in (x for x in text.splitlines() if x.startswith("warm-up ")):
        assert line.count("close ") == 1 and line.count("open ") == 1
    assert all(
        f["depth"] == {"p10": None, "p50": None, "p90": None} and f["D"] is None for f in pinch_report["fingers"]
    )


def test_the_rendered_report_is_lines_of_plain_text_with_no_unfilled_numbers(clean_suggested):
    lines = keyreplay.render(clean_suggested)
    assert lines and all(isinstance(x, str) and "\n" not in x for x in lines)
    assert not any(token in "\n".join(lines) for token in ("nan", "None", "{", "}", "inf"))


# ---------------------------------------------------------------------------------------------------------- privacy

HOME_CHARS_ALL = {c for chars in HOME_CHARS.values() for c in chars} | set("eiqwrtyuopghcvbnm")
FORBIDDEN_FIELDS = {"key", "keys", "char", "chars", "character", "target", "targets", "legend", "text", "typed", "word"}


def all_fields(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {name for v in value.values() for name in all_fields(v)}
    if isinstance(value, (list, tuple)):
        return {name for v in value for name in all_fields(v)}
    return set()


def all_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in all_strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in all_strings(v)]
    return [value] if isinstance(value, str) else []


def test_no_report_holds_a_key_a_character_or_a_target_only_counts_times_and_finger_labels(
    weak_report, clean_suggested, pinch_report, moving, tmp_path
):
    phrase = phrase_report(drill_trace(tmp_path / "p.npz", prompts=16, drill_kind="phrase", written_prompts=False).path)
    reports = [weak_report, clean_suggested, pinch_report, phrase, phrase_report(moving.path)]
    for report in reports:
        json.dumps(report)  # whole, plain data: nothing to hide in an object
        assert not all_fields(report) & FORBIDDEN_FIELDS
        assert all(len(s) != 1 for s in all_strings(report)), "a one-character string could be a typed character"
        assert not any(key in HOME_CHARS_ALL for key in all_strings(report))


def test_a_run_prints_only_to_the_stream_it_is_given(clean, capsys):
    replay(clean.path, "--no-suggest")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_every_print_of_the_module_names_the_stream():
    import ast

    tree = ast.parse(Path(keyreplay.__file__).read_text(encoding="utf-8"))
    prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"]
    assert prints and all(any(kw.arg == "file" for kw in call.keywords) for call in prints)


def test_importing_the_replay_loads_no_camera_no_model_and_not_the_recorder():
    import subprocess
    import sys

    code = (
        "import sys, jarvis_hands.keyboard.keyreplay;"
        "print(sorted(m for m in ('cv2', 'mediapipe', 'jarvis_hands.keyboard.keytrace') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_the_tuning_names_that_can_be_set_are_exactly_the_fields_of_the_tuning():
    import dataclasses

    assert set(keyreplay.TUNABLE) == {f.name for f in dataclasses.fields(Tuning)}
    assert set(keyreplay.TUNABLE.values()) == set(GROUPS)


# ---------------------------------------------------------------------------------- from the recorder to the replay


def test_a_recording_from_keytrace_replays_with_every_prompt_it_wrote(tmp_path):
    from test_kb_keytrace import Session

    session = Session(tmp_path, "--segments", "drill:30").run()
    assert session.code == 0
    from jarvis_hands.keyboard import keytrace

    assert keyreplay.PROMPTS_KEY == keytrace.PROMPTS_KEY == "drill_prompts"
    rec = keyreplay.load(session.path)
    assert rec.prompts is not None and len(rec.prompts) == len(session.prompts()) == 25
    report = keyreplay.analyse(rec, Tuning(), suggest=True)
    assert report["prompts"]["total"] == 25 and report["prompts"]["named"] == 25 and report["prompts"]["unnamed"] == 0
    assert report["prompts"]["missed"] == 25 and report["prompts"]["missed_by"] == {"no_peak": 25}
    assert sum(f["prompts"] for f in report["fingers"]) == 25
    assert report["suggest"]["values"] == {}  # still hands: nothing to learn, and nothing is made up
    assert report["segments"].keys() == {"place", "drill"}


# ------------------------------------------------------------------------------------- the corners of the replay


def shift_times(path: Path, *, from_frame: int, seconds: float) -> None:
    """The file as if the camera had stopped delivering for ``seconds`` at ``from_frame``."""
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    arrays["t"] = arrays["t"].copy()
    arrays["t"][from_frame:] += seconds
    np.savez_compressed(path, **arrays)


def test_a_gap_in_the_frames_is_not_counted_as_time_spent_in_the_segment(tmp_path):
    path = hover_trace(tmp_path / "g.npz", fps=30, seconds=4)
    shift_times(path, from_frame=60, seconds=3.0)  # two seconds, nothing for three, two more
    report = phrase_report(path)
    assert report["segments"]["rest"] == pytest.approx(4.25, abs=0.1)
    assert report["seconds"] == pytest.approx(7.0, abs=0.1)
    assert report["phantoms"]["rest"]["seconds"] == pytest.approx(4.25, abs=0.1)


def test_the_data_dir_is_the_argument_then_the_environment_then_the_home_folder(clean, tmp_path, monkeypatch):
    elsewhere, home = tmp_path / "env", tmp_path / "home"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(elsewhere))
    assert replay(clean.path, "--no-suggest", "--write", "--set", "air_theta_k=6.0")[0] == 0
    assert load_tuning(elsewhere).air_theta_k == 6.0
    explicit = tmp_path / "explicit"
    assert replay(clean.path, "--no-suggest", "--write", "--set", "air_theta_k=5.0", data_dir=explicit)[0] == 0
    assert load_tuning(explicit).air_theta_k == 5.0 and load_tuning(elsewhere).air_theta_k == 6.0
    monkeypatch.delenv("JARVIS_DATA_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert replay(clean.path, "--no-suggest", "--write", "--set", "air_theta_k=7.0")[0] == 0
    assert load_tuning(home / ".jarvis").air_theta_k == 7.0


def test_a_reach_key_in_a_drill_without_a_table_names_the_finger_that_reaches_it(tmp_path):
    """Practice traces drill backspace, insert, clear and enter with the one finger that owns each."""
    path = drill_trace(
        tmp_path / "r.npz", prompts=8, fingers=(0,), sides=("right",), written_prompts=False
    ).path  # eight right-index taps over the one key 'j' ...
    ask_for_another_key(path, from_second=0.0, key=REVIEW.find(kind="backspace").index)  # ... asked for as backspace
    report = phrase_report(path)
    assert report["prompts"]["total"] == 1 and report["prompts"]["unnamed"] == 0 and report["prompts"]["hit"] == 1
    assert report["prompts"]["extra"] == 7  # the other seven taps are second taps in the same prompt


def test_a_candidate_that_breaks_a_relation_between_two_fields_is_never_tried(weak, monkeypatch):
    """With every candidate taken as better, the lower limit still stops short of the upper one."""
    monkeypatch.setattr(keyreplay.Analysis, "_better", staticmethod(lambda new, old: True))
    tuning = Tuning(air_theta_max=0.18)  # the lowest the range allows: 0.20 would be above it
    values = keyreplay.Analysis(keyreplay.load(weak.path), tuning).suggest()["values"]
    assert values["air_theta_min"] == 0.16
    assert keyreplay._fits(replace(tuning, **values))


def test_values_that_are_better_in_the_search_but_not_in_the_whole_replay_are_not_suggested(weak, monkeypatch):
    real = keyreplay.Analysis._measure

    def measure(self, run):
        recall, false_rate, named = real(self, run)
        return (0.0, 0.0, named) if self.tuning != Tuning() else (recall, false_rate, named)

    monkeypatch.setattr(keyreplay.Analysis, "_measure", measure)
    suggest = keyreplay.Analysis(keyreplay.load(weak.path), Tuning()).suggest()
    assert suggest["values"] == {} and suggest["recall"] == suggest["base_recall"]
    assert suggest["notes"] == ["No value did better when the recording was replayed with it."]


def test_too_few_prompted_taps_give_no_aim_suggestion_even_when_a_rule_is_clearly_better(weak):
    analysis = keyreplay.Analysis(keyreplay.load(weak.path), Tuning(air_aim="onset"))
    for tap in analysis.taps:
        tap.rules["onset"] = (tap.rules["onset"][0] + 1.5, tap.rules["onset"][1])
    scored = analysis._aim_taps(keyreplay._classify(analysis.taps, analysis.prompts))
    assert 0 < len(scored) < keyreplay.MIN_PROMPTS_TO_SUGGEST // 2
    assert analysis._aim_choice(frozenset()) == {}


# ---------------------------------------------------------------------------------------- placing, talk, the aim grid


def swaying_trace(path: Path, sway: dict[str, float], *, seconds: float = 8.0, kind: str = "rest") -> Path:
    """Palms in view: each hand in ``sway`` sways side to side until the second given (0: still all along)."""
    base = {
        "left": syn.hand("palm", (0.36, 0.5), handedness="left"),
        "right": syn.hand("palm", (0.64, 0.5), handedness="right"),
    }
    writer = TraceWriter(press="air")
    for n in range(int(seconds * FPS)):
        t = n / FPS
        seen = []
        for side, until in sway.items():
            image = base[side].image.copy()
            if t < until:
                image[:, 0] += 0.06 * np.sin(2 * np.pi * t)
            seen.append(HandObservation(side, 0.95, image, base[side].world))
        writer.add(Frame(T0 + t, tuple(seen), 1280, 720), kind)
    return writer.save(path)


def placed_at(path: Path) -> float:
    return phrase_report(path)["placed"]["at"]


def test_the_plane_is_placed_once_a_hand_has_been_still_for_the_still_time(tmp_path):
    still = swaying_trace(tmp_path / "a.npz", {"left": 0, "right": 0})
    assert placed_at(still) == pytest.approx(Tuning().still_s, abs=0.1)
    longer = keyreplay.analyse(keyreplay.load(still), Tuning(still_s=1.2), suggest=False)
    assert longer["placed"]["at"] == pytest.approx(1.2, abs=0.1)


def test_the_still_time_is_counted_from_when_the_hand_stopped_not_from_when_it_was_seen(tmp_path):
    """A hand that swayed for two seconds is still after 2 s, and its speed is read over half a second."""
    assert placed_at(swaying_trace(tmp_path / "a.npz", {"right": 2.0})) == pytest.approx(2.87, abs=0.1)
    assert placed_at(swaying_trace(tmp_path / "b.npz", {"right": 4.0})) == pytest.approx(4.87, abs=0.1)


def test_the_hand_that_is_still_waits_one_second_for_the_other_and_then_the_plane_is_placed_for_it_alone(tmp_path):
    path = swaying_trace(tmp_path / "a.npz", {"left": 99.0, "right": 0})  # the left hand never stops
    assert placed_at(path) == pytest.approx(Tuning().still_s + 1.0, abs=0.1)


def test_taps_in_a_talk_segment_are_phantom_taps_like_those_in_a_rest(tmp_path):
    path = hover_trace(tmp_path / "t.npz", fps=30, seconds=20, kind="talk", taps=(("left", 0, 5.0), ("right", 2, 12.0)))
    report = phrase_report(path)
    assert report["phantoms"]["talk"]["taps"] == 2 and report["phantoms"]["talk"]["per_min"] == pytest.approx(
        6.0, abs=0.3
    )
    assert report["phantoms"]["talk"]["by_finger"] == {"left.index": 1, "right.ring": 1}
    assert "phantoms in talk: 2 taps" in "\n".join(keyreplay.render(report))


def test_the_aim_speed_is_chosen_from_a_fixed_grid_and_the_one_nearest_the_current_wins_a_tie(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning(air_aim="auto", air_aim_speed=0.15))
    for tap in analysis.taps:
        tap.vl = 0.025  # every tap moved a little, so only a limit under 0.025 aims them by where the hand ended up
        tap.rules["onset"] = (tap.rules["onset"][0] + 1.5, tap.rules["onset"][1])
    assert analysis._aim_choice(frozenset()) == {"air_aim_speed": 0.02}


# ----------------------------------------------------------------------------- the sentences, the files, edges


@pytest.mark.parametrize(
    ("content", "sentence"),
    [
        (b"{not json", "The tuning file cannot be read."),
        (b"\xff\xfe", "The tuning file cannot be read."),
        (b'{"version":1,"pad":"' + b"x" * 70000 + b'"}', "The tuning file cannot be read."),  # valid, too large
        (b"[]", "The tuning file is not a version 1 object."),
        (b'{"version": 2, "air": {}}', "The tuning file is not a version 1 object."),
        (b'{"version": true}', "The tuning file is not a version 1 object."),
        (b'{"version": "1"}', "The tuning file is not a version 1 object."),
        (b'{"version": 1, "air": 5}', "The tuning file has a group that is not an object."),
    ],
)
def test_each_way_a_tuning_file_can_be_unfit_to_merge_into_says_so(tmp_path, content, sentence):
    path = tuning_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(keyreplay.ReplayError) as caught:
        keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    assert str(caught.value).startswith(sentence) and path.read_bytes() == content


def test_a_file_with_no_version_gets_one_and_keeps_what_it_has(tmp_path):
    path = tuning_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"air": {"air_theta_min": 0.12}}', encoding="utf-8")
    keyreplay.write_tuning(path, {"air_theta_k": 6.0})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "air": {"air_theta_min": 0.12, "air_theta_k": 6.0},
    }


def test_the_names_written_come_back_in_the_order_of_the_files_groups(tmp_path):
    names = keyreplay.write_tuning(
        tuning_path(tmp_path), {"air_aim": "onset", "pitch": 0.05, "air_theta_k": 6.0, "close": 0.3}
    )
    ordered = [n for g in GROUPS for n in GROUPS[g] if n in {"air_aim", "pitch", "air_theta_k", "close"}]
    assert names == ordered and names[0] == "close" and names[1] == "pitch" and names[-1] == "air_aim"


@pytest.mark.parametrize("value", [(0.1, 0.2), "abcd", 5, None, (0, 0, 0, 0, 0)])
def test_a_level_that_is_not_four_numbers_is_not_written(tmp_path, value):
    with pytest.raises(keyreplay.ReplayError):
        keyreplay.write_tuning(tuning_path(tmp_path), {"level_palm": value})
    assert not tuning_path(tmp_path).exists()


def test_a_missing_file_and_a_folder_are_told_apart_from_a_file_that_is_not_a_trace(tmp_path):
    _, missing = replay(tmp_path / "zzz_missing.npz")
    _, folder = replay(tmp_path)
    (tmp_path / "bad.npz").write_bytes(b"x")
    _, bad = replay(tmp_path / "bad.npz")
    assert "does not exist" in missing and "does not exist" in folder and "not a landmark trace" in bad


def test_a_warm_up_that_ends_before_all_eight_fingers_are_done_still_ends_with_its_segment(tmp_path):
    """The session would wait for the rest; a recording cannot, so what was learned is used and typing is counted."""
    made = drill_trace(tmp_path / "w.npz", prompts=16, warm=True, warm_taps=4, written_prompts=False)
    report = phrase_report(made.path)
    assert report["depth_source"] == "warm-up" and report["events"] >= 12
    assert sum(f["D"] is not None for f in report["fingers"]) == 4


def test_a_pause_inside_a_drill_is_not_a_prompt_without_a_table(tmp_path):
    path = drill_trace(tmp_path / "p.npz", prompts=16, written_prompts=False).path
    assert phrase_report(path)["prompts"]["total"] == 16
    half = 0.5 / FPS  # the clock of a frame is a float: cut between frames, not on one
    ask_for_another_key(
        path, from_second=PLACE_S + 4 * AIR_DRILL_GAP_S - half, to_second=PLACE_S + 8 * AIR_DRILL_GAP_S - half, key=-1
    )
    assert phrase_report(path)["prompts"]["total"] == 12


def test_still_fists_do_not_place_the_plane(tmp_path):
    fist = syn.hand("fist", (0.5, 0.5), handedness="right")
    writer = TraceWriter(press="air")
    for n in range(int(6 * FPS)):
        writer.add(Frame(T0 + n / FPS, (HandObservation("right", 0.95, fist.image, fist.world),), 1280, 720), "rest")
    assert phrase_report(writer.save(tmp_path / "f.npz"))["placed"] is None


def test_the_noise_is_read_from_the_rest_when_there_is_one_even_if_the_place_was_quieter(tmp_path):
    rest = noisy_hand_trace(tmp_path / "r.npz", sigma_xy=0.004, sigma_z=0.012, seconds=6.0, kind="rest")
    place = noisy_hand_trace(tmp_path / "p.npz", sigma_xy=0.001, sigma_z=0.003, seconds=6.0, kind="place")
    with np.load(rest, allow_pickle=False) as a, np.load(place, allow_pickle=False) as b:
        joined = {
            name: np.concatenate([b[name], a[name]]) for name in ("t", "present", "side", "score", "lm", "targets")
        }
        joined["t"][len(b["t"]) :] += b["t"][-1] + 0.1 - a["t"][0]
        joined.update({name: a[name] for name in ("aspect", "width", "height", "press", "version")})
        joined["segments"] = np.array(
            [(0, len(b["t"]), "place"), (len(b["t"]), len(joined["t"]), "rest")],
            dtype=[("start", "<i4"), ("end", "<i4"), ("kind", "<U12")],
        )
    np.savez_compressed(tmp_path / "j.npz", **joined)
    jitter = phrase_report(tmp_path / "j.npz")["jitter"]
    assert jitter["from"] == "rest" and jitter["xy"] == pytest.approx(0.004, rel=0.2)


def cut_drill_at(path: Path, frame: int) -> None:
    """The file with its drill segment ending at ``frame`` and the rest starting there."""
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    rows = [(int(s), int(e), str(k)) for s, e, k in arrays["segments"].tolist()]
    rows = [(s, frame, k) if k == "drill" else (frame, e, k) if k == "rest" else (s, e, k) for s, e, k in rows]
    arrays["segments"] = np.array(rows, dtype=arrays["segments"].dtype)
    np.savez_compressed(path, **arrays)


def test_a_tap_committed_on_the_first_frame_after_the_drill_belongs_to_no_prompt(tmp_path):
    path = drill_trace(tmp_path / "c.npz", prompts=8, fingers=(0, 1), rest_s=6.0).path
    before = phrase_report(path)
    assert before["prompts"]["hit"] == 8 and before["phantoms"]["rest"]["taps"] == 0
    last = keyreplay.Analysis(keyreplay.load(path), Tuning()).taps[-1].frame
    cut_drill_at(path, last)
    after = phrase_report(path)
    assert after["prompts"]["hit"] == 7 and after["phantoms"]["rest"]["taps"] == 1


def test_after_the_warm_up_the_press_is_out_of_its_calibrating_mode(tmp_path):
    """While calibrating a threshold is 0.07 or four sigmas; after it, the usual one (here the lower limit, 0.10)."""
    made = drill_trace(tmp_path / "w.npz", prompts=16, warm=True, written_prompts=False)
    report = phrase_report(made.path)
    assert all(f["theta"] == pytest.approx(Tuning().air_theta_min, abs=0.002) for f in report["fingers"])


# ---------------------------------------------------------------------------------- how a tap is read against prompts


def a_tap(t: float, side: str = "left", finger: int = 0, kind: str = "drill", depth: float = 0.3) -> keyreplay.Tap:
    return keyreplay.Tap(
        frame=0, t=t, onset_t=t - 0.1, side=side, finger=finger, hand=0, depth=depth, aim=(0.0, 0.0), kind=kind
    )  # type: ignore[arg-type]


def a_prompt(t0: float, t1: float, side: str | None = "left", finger: int | None = 0) -> keyreplay._Prompt:
    return keyreplay._Prompt(t0, t1, side, finger, 5)  # type: ignore[arg-type]


PROMPTS = [a_prompt(10.0, 11.0), a_prompt(11.0, 12.0, "right", 1), a_prompt(13.0, 14.0, None, None)]


@pytest.mark.parametrize(
    ("tap", "outcome", "window"),
    [
        (a_tap(10.0), "hit", 0),  # a prompt's first instant is its own
        (a_tap(10.999), "hit", 0),
        (a_tap(11.0, "right", 1), "hit", 1),  # and the instant it ends is the next one's
        (a_tap(11.0), "wrong", 1),
        (a_tap(10.5, "right", 0), "wrong", 0),  # the finger is of the other hand
        (a_tap(10.5, "left", 1), "wrong", 0),  # the hand is right and the finger another
        (a_tap(13.5), "unnamed", 2),  # a prompt that names no finger scores no tap
        (a_tap(9.9, kind="rest"), "phantom", None),
        (a_tap(9.9, kind="wave"), "phantom", None),
        (a_tap(9.9, kind="talk"), "phantom", None),
        (a_tap(9.9, kind="type"), "free", None),
        (a_tap(12.0, kind="drill"), "free", None),  # the end of the last prompt before a pause is outside it
        (a_tap(12.5, kind="rest"), "phantom", None),
        (a_tap(14.0, kind="phrase"), "free", None),
    ],
)
def test_a_tap_is_read_against_the_prompt_whose_window_it_falls_in(tap, outcome, window):
    scores = keyreplay._classify([tap], PROMPTS)
    assert (scores.outcome, scores.window) == ([outcome], [window])


def test_the_first_tap_in_a_window_is_the_hit_and_a_second_is_extra():
    scores = keyreplay._classify([a_tap(10.2), a_tap(10.4), a_tap(10.6, "right", 3)], PROMPTS)
    assert scores.outcome == ["hit", "extra", "wrong"] and scores.hit == [0, None, None]


def test_the_window_of_the_last_prompt_of_a_drill_ends_with_the_drill_not_at_the_next_prompt(tmp_path):
    """Two drills with a rest between: taps in the rest are phantoms, not extra taps of the prompt before them."""
    made = drill_trace(tmp_path / "s.npz", prompts=16, fingers=(0, 1), written_prompts=False)
    rows = made.prompts[:8] + made.prompts[12:]
    path = made.path
    with np.load(path, allow_pickle=False) as npz:
        arrays = {name: npz[name] for name in npz.files}
    first, resume, later = made.prompts[8][0], made.prompts[12][0], arrays["segments"].tolist()[-1][1]
    drill_start = made.prompts[0][0]
    arrays["segments"] = np.array(
        [(0, drill_start, "place"), (drill_start, first, "drill"), (first, resume, "rest"), (resume, later, "drill")],
        dtype=arrays["segments"].dtype,
    )
    arrays["targets"] = arrays["targets"].copy()
    arrays["targets"][first:resume] = -1
    np.savez_compressed(path, **arrays)
    with_prompts(path, rows)
    report = phrase_report(path)
    assert report["prompts"]["total"] == 12 and report["prompts"]["hit"] == 12
    assert report["prompts"]["extra"] == 0 and report["phantoms"]["rest"]["taps"] == 4


# ------------------------------------------------------------------------------- why a prompt was missed, and the rest


def test_a_missed_prompt_is_blamed_on_a_refused_peak_then_on_a_hand_gate_shut_a_third_of_it_else_on_no_peak(clean):
    analysis = keyreplay.Analysis(keyreplay.load(clean.path), Tuning())
    times = analysis.rec.data.t
    prompt = analysis.prompts[2]
    first, last = int(np.searchsorted(times, prompt.t0)), int(np.searchsorted(times, prompt.t1))
    frames = last - first
    assert frames == 36
    analysis.run.refused = {}
    analysis.run.gates = [{} for _ in times]
    assert analysis._why_missed(2) == "no_peak"
    for frame in range(first, first + 12):  # exactly a third of the window
        analysis.run.gates[frame] = {prompt.side: "speed"}
    assert analysis._why_missed(2) == "g_speed"
    analysis.run.gates[first] = {}  # one frame short of a third
    assert analysis._why_missed(2) == "no_peak"
    analysis.run.gates[first] = {"left" if prompt.side == "right" else "right": "speed"}  # the other hand's gate
    assert analysis._why_missed(2) == "no_peak"
    analysis.run.refused = {(prompt.side, prompt.finger): [(prompt.t0 - 0.5, "narrow"), (prompt.t1 + 0.1, "wide")]}
    assert analysis._why_missed(2) == "no_peak"  # peaks refused outside the window are not its fault
    analysis.run.refused = {(prompt.side, prompt.finger): [(prompt.t0 + 0.1, "plateau")]}
    assert analysis._why_missed(2) == "plateau"


def test_the_level_says_when_it_first_went_off_not_when_it_last_did(tmp_path):
    report = phrase_report(hover_trace(tmp_path / "h.npz", fps=10, seconds=20))
    assert report["level"]["name"] == "off" and 0 < report["level"]["off_at"] < report["seconds"] - 5


def test_a_finger_with_one_prompted_tap_has_no_depth_of_its_own(tmp_path):
    report = phrase_report(drill_trace(tmp_path / "o.npz", prompts=8).path)
    assert report["depth_source"] == "none" and all(f["hits"] == 1 and f["D"] is None for f in report["fingers"])
    report = phrase_report(drill_trace(tmp_path / "t.npz", prompts=16).path)
    assert report["depth_source"] == "prompted taps" and all(f["D"] is not None for f in report["fingers"])


def test_a_value_equal_to_the_one_in_use_is_not_a_suggestion(pinch_report):
    assert pinch_report["placed"]["pitch"] == Tuning().pitch  # the plane the hands set is the one in use already
    assert "pitch" not in pinch_report["suggest"]["values"]


def test_the_noise_needs_thirty_residuals_to_be_said(tmp_path):
    short = phrase_report(noisy_hand_trace(tmp_path / "s.npz", sigma_xy=0.002, sigma_z=0.005, seconds=0.9))
    assert short["jitter"]["xy"] is None  # 27 frames: 25 second differences
    enough = phrase_report(noisy_hand_trace(tmp_path / "e.npz", sigma_xy=0.002, sigma_z=0.005, seconds=1.2))
    assert enough["jitter"]["frames"] == 34 and enough["jitter"]["xy"] is not None


def test_seconds_with_no_hand_in_view_are_not_seconds_of_a_rest(tmp_path):
    base = syn.hand("palm", (0.5, 0.5), handedness="right")
    writer = TraceWriter(press="air")
    for n in range(int(4 * FPS)):
        seen = (HandObservation("right", 0.95, base.image, base.world),) if n < int(2 * FPS) else ()
        writer.add(Frame(T0 + n / FPS, seen, 1280, 720), "rest")
    report = phrase_report(writer.save(tmp_path / "a.npz"))
    assert report["segments"]["rest"] == pytest.approx(4.0, abs=0.1)
    assert report["phantoms"]["rest"]["seconds"] == pytest.approx(2.0, abs=0.1)


def test_the_pinch_close_threshold_is_suggested_from_the_median_of_four_fingers_or_more(tmp_path):
    analysis = keyreplay.Analysis(keyreplay.load(pinch_trace(tmp_path / "p.npz")), Tuning())

    def recorded(*closes):
        return {"warm": [{"recorded": True, "close": c} for c in closes] + [{"recorded": False, "close": 0.99}]}

    analysis._pinch_block = lambda: recorded(0.20, 0.30, 0.22)  # type: ignore[method-assign]
    assert analysis._pinch_values(frozenset()) == {}
    analysis._pinch_block = lambda: recorded(0.20, 0.30, 0.22, 0.24)  # type: ignore[method-assign]
    assert analysis._pinch_values(frozenset()) == {"close": 0.23}
    assert analysis._pinch_values(frozenset({"close"})) == {}
    analysis._pinch_block = lambda: recorded(0.01, 0.01, 0.01, 0.01)  # type: ignore[method-assign]
    assert analysis._pinch_values(frozenset()) == {"close": RANGES["close"][0]}


def test_a_tap_with_no_aims_of_its_own_is_left_out_of_the_aim_table(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning())
    analysis.taps[0].rules = {}
    scores = keyreplay._classify(analysis.taps, analysis.prompts)
    assert len(analysis._aim_taps(scores)) == len(analysis.taps) - 1
    assert analysis.report(suggest=False)["aim"]["n"] == len(analysis.taps) - 1


def test_a_key_is_looked_up_with_the_edge_tolerance_in_use(moving):
    seen = []
    for tolerance in (0.2, 0.5):
        analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning(edge_tolerance=tolerance))
        analysis.layout = types.SimpleNamespace(key_at=lambda u, v, tol: seen.append(tol))  # type: ignore[assignment]
        analysis._key_of((0.0, 0.0))
    assert seen == [0.2, 0.5]


def test_what_was_set_by_hand_is_not_among_the_suggestions_of_the_run(weak):
    _, free = replay(weak.path)
    _, held = replay(weak.path, "--set", "air_theta_min=0.12")

    def suggested(text: str) -> str:
        return next((x for x in text.splitlines() if x.startswith("suggested: ")), "")

    assert "air_theta_min" in suggested(free) and "air_theta_min" not in suggested(held)


def test_the_aim_speed_nearest_the_one_in_use_wins_among_equally_good_ones(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning(air_aim="auto", air_aim_speed=0.15))
    for tap in analysis.taps:
        tap.vl = 0.1  # any limit under 0.1 aims every tap by where the hand ended up: 0.02 to 0.08 are all right
        tap.rules["onset"] = (tap.rules["onset"][0] + 1.5, tap.rules["onset"][1])
    assert analysis._aim_choice(frozenset()) == {"air_aim_speed": 0.08}


def test_the_pitch_the_hands_set_is_suggested_for_a_pinch_unless_it_was_set_by_hand(tmp_path):
    rec = keyreplay.load(pinch_trace(tmp_path / "p.npz"))
    free = keyreplay.Analysis(rec, Tuning(pitch=0.06)).report(suggest=True)
    assert free["suggest"]["values"]["pitch"] == free["placed"]["pitch"] != 0.06
    held = keyreplay.Analysis(rec, Tuning(pitch=0.06)).report(suggest=True, pinned=("pitch",))
    assert "pitch" not in held["suggest"]["values"]


def test_a_csv_that_fails_keeps_the_exit_code_even_when_the_write_after_it_works(clean, tmp_path):
    code, text = replay(
        clean.path,
        "--no-suggest",
        "--csv",
        str(tmp_path / "nowhere" / "out.csv"),
        "--write",
        "--set",
        "air_theta_k=6.0",
    )
    assert "could not be written" in text and "Wrote to the tuning file: air_theta_k=6" in text
    assert code == keyreplay.EXIT_FAILED


def test_one_loud_landmark_does_not_make_the_hand_noisy(tmp_path):
    """The estimate is the median over the landmarks: a fingertip the model wavers on is a fingertip, not the hand."""
    path = noisy_hand_trace(tmp_path / "l.npz", sigma_xy=0.002, sigma_z=0.005, loud_tip=12.0)
    jitter = phrase_report(path)["jitter"]
    assert jitter["xy"] == pytest.approx(0.002, rel=0.17) and jitter["z"] == pytest.approx(0.005, rel=0.17)


def test_the_rejects_are_listed_most_common_first_and_by_name_among_equals(clean):
    analysis = keyreplay.Analysis(keyreplay.load(clean.path), Tuning())
    analysis.run.rejects = collections.Counter({"motion": 1, "plateau": 3, "narrow": 3, "wide": 2})
    assert list(analysis.report(suggest=False)["rejects"].items()) == [
        ("narrow", 3), ("plateau", 3), ("wide", 2), ("motion", 1),
    ]  # fmt: skip


def test_the_text_of_a_report_gives_each_part_of_it_in_words(clean_suggested, tmp_path):
    text = "\n".join(keyreplay.render(clean_suggested))
    rejects = ", ".join(f"{k} {v}" for k, v in clean_suggested["rejects"].items())
    assert f"rejects: {rejects}" in text
    assert "D is the median depth of each finger's own prompted taps: the recording has no warm-up" in text
    assert "phantoms in rest: 3 taps in 20 s, 9.0 a minute (left.index 2, right.ring 1)" in text
    assert text.splitlines()[-1] == "suggested: nothing to change"
    assert "camera: level ok, fps 30.0," in text and "placement: at 0.6 s, pitch " in text
    warmed = phrase_report(drill_trace(tmp_path / "w.npz", prompts=16, warm=True, written_prompts=False).path)
    assert "D is the median depth" not in "\n".join(keyreplay.render(warmed))
    fist = syn.hand("fist", (0.5, 0.5), handedness="right")
    writer = TraceWriter(press="air")
    for n in range(int(2 * FPS)):
        writer.add(Frame(T0 + n / FPS, (HandObservation("right", 0.95, fist.image, fist.world),), 1280, 720), "rest")
    nothing = "\n".join(keyreplay.render(phrase_report(writer.save(tmp_path / "f.npz"))))
    assert "placement: no still hands were found" in nothing
    off = "\n".join(keyreplay.render(phrase_report(hover_trace(tmp_path / "o.npz", fps=10, seconds=20))))
    assert "camera: level off, off at 6.8 s, fps 10.0" in off


def test_a_kind_that_was_recorded_with_no_hand_in_view_is_there_with_zero_seconds(tmp_path):
    writer = TraceWriter(press="air")
    for n in range(int(3 * FPS)):
        writer.add(Frame(T0 + n / FPS, (), 1280, 720), "wave")
    report = phrase_report(writer.save(tmp_path / "w.npz"))
    assert report["phantoms"] == {"wave": {"seconds": 0.0, "taps": 0, "per_min": None, "by_finger": {}}}
    assert "phantoms in wave: 0 taps in 0 s" in "\n".join(keyreplay.render(report))
    typing = hover_trace(tmp_path / "t.npz", fps=30, seconds=3, kind="type")
    assert keyreplay.analyse(keyreplay.load(typing), Tuning())["phantoms"] == {}  # no rest, wave or talk: no line


def test_a_plane_or_levels_suggestion_stays_inside_the_ranges(tmp_path):
    analysis = keyreplay.Analysis(keyreplay.load(pinch_trace(tmp_path / "p.npz")), Tuning())
    for px, expected in ((0.5, RANGES["pitch"][1]), (0.001, RANGES["pitch"][0])):
        analysis.hands.placement = types.SimpleNamespace(plane=types.SimpleNamespace(px=px))  # type: ignore[assignment]
        assert analysis._plane_values(frozenset())["pitch"] == expected
    analysis.hands.levels = {"left": (0.9, -0.9, 0.1, 0.0), "right": (0.9, -0.9, 0.3, 0.0)}
    assert analysis._plane_values(frozenset())["level_palm"] == (0.3, -0.3, 0.2, 0.0)


def test_a_finger_s_stand_in_depth_is_the_median_of_its_prompted_taps_not_the_mean(moving):
    analysis = keyreplay.Analysis(keyreplay.load(moving.path), Tuning())
    taps = [a_tap(1.0 + k, depth=depth) for k, depth in enumerate((0.3, 0.3, 0.9, 0.5))]
    taps[3].finger = 1  # one tap of another finger: one prompted tap is not enough for a depth
    analysis.run.taps = taps
    scores = keyreplay._Scores(["hit"] * 4, [0, 1, 2, 3], [0, 1, 2, 3])
    assert analysis._stand_in_depths(scores) == {("left", 0): 0.3}


def test_the_search_leaves_the_depth_fraction_alone_when_there_is_no_depth_to_scale(weak, monkeypatch):
    asked = []
    real = keyreplay.Analysis._press

    def spy(self, tuning, depths, *, aims=True):
        if not aims:
            asked.append((tuning, depths))
        return real(self, tuning, depths, aims=aims)

    monkeypatch.setattr(keyreplay.Analysis, "_press", spy)
    with_depths = keyreplay.Analysis(keyreplay.load(weak.path), Tuning())
    assert with_depths.depths
    with_depths.suggest()
    assert len({t.air_depth_frac for t, _ in asked}) > 1  # tried, because the depths are known
    assert all(depths == with_depths.depths for _, depths in asked)  # and every trial holds the fingers' depths fixed
    asked.clear()
    without = keyreplay.Analysis(
        keyreplay.load(drill_trace(weak.path.with_name("n.npz"), prompts=24, skip=tuple(range(24))).path), Tuning()
    )
    assert not without.depths and not without.run.depth
    without.suggest()
    assert asked and {t.air_depth_frac for t, _ in asked} == {Tuning().air_depth_frac}
    assert all(not depths for _, depths in asked)


def test_a_rule_within_a_hundredth_of_the_best_is_as_good_and_the_setting_stays(tmp_path):
    """102 prompts: one wrong key is 0.0098 of them, under the tie of 0.01; two are over it."""
    path = drill_trace(tmp_path / "long.npz", prompts=102, fingers=(0, 1), displaced=True, lead_s=0.0).path
    analysis = keyreplay.Analysis(keyreplay.load(path), Tuning(air_aim="onset"))
    scored = [tap for tap, _ in analysis._aim_taps(keyreplay._classify(analysis.taps, analysis.prompts))]
    assert len(scored) == 102
    for tap in scored:  # the made hands are not perfect: make the three rules name the same keys
        tap.rules["commit"], tap.vl = tap.rules["onset"], 0.0
    assert analysis._aim_choice(frozenset()) == {}
    for count in (1, 2):
        u, v = scored[count - 1].rules["onset"]
        scored[count - 1].rules["onset"] = (u + 1.5, v)
        expected = {} if count == 1 else {"air_aim": "commit"}
        assert analysis._aim_choice(frozenset()) == expected, count
