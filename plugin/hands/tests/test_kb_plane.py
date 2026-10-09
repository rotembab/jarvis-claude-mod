"""The plane and its placement: U6, U48, A12 (the formulas of 2.4), H7 (DESIGN-KEYBOARD.md 2.3, 2.4, 3.3).

A13 and A15, which run the whole pipeline (synthetic hands, tracker, placement, press), are in ``test_kb_scenarios``.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from jarvis_hands.keyboard import plane as pl
from jarvis_hands.keyboard.layout import WIDTH_U, Layout, layout_for
from jarvis_hands.keyboard.plane import Placement, Plane, place_plane
from jarvis_hands.keyboard.tuning import Tuning
from jarvis_hands.keyboard.types import FingerSample, HandSample, Side

DIRECT = layout_for("direct")
REVIEW = layout_for("review")
TUNING = Tuning()
PX0 = TUNING.pitch


def hand(
    hid: int,
    side: Side,
    aims: list[tuple[float, float]],
    t: float = 0.0,
    *,
    anchor: tuple[float, float] | None = None,
) -> HandSample:
    """A hand sample with the four given aims (index to pinky); everything else is quiet and open."""
    fingers = tuple(
        FingerSample(f, np.array(aim, dtype=float), reach=1.8, ratio=1.0, curled=False) for f, aim in enumerate(aims)
    )
    centre = (
        anchor if anchor is not None else (float(np.mean([a[0] for a in aims])), float(np.mean([a[1] for a in aims])))
    )
    return HandSample(hid, side, t, 0.1, np.array(centre, dtype=float), 0.0, fingers)


def cluster(x: float, y: float, spread: float = 0.04) -> list[tuple[float, float]]:
    """Four fingertips around ``x``: a symmetric spread, a slight arc."""
    offsets = (-1.5, -0.5, 0.5, 1.5)
    return [(x + o * spread, y + (0.004 if i in (1, 2) else 0.0)) for i, o in enumerate(offsets)]


def window(*hands: list[HandSample], frames: int = 4) -> list[list[HandSample]]:
    """``frames`` identical frames of the given hands."""
    return [[h for h in hands] for _ in range(frames)]


# --- U6, H7: units and pose ---


@pytest.mark.parametrize("rows", [4, 5])
def test_u6_units_and_pose_are_exact_inverses(rows: int) -> None:
    plane = Plane(0.4, 0.55, 0.05, 0.0625, rows)
    rng = np.random.default_rng(6)
    for _ in range(200):
        u, v = rng.uniform(-1.0, 13.0), rng.uniform(-1.0, rows + 1.0)
        x, y = plane.pose(u, v)
        back = plane.units((x, y))
        assert math.isclose(back[0], u, abs_tol=1e-9)
        assert math.isclose(back[1], v, abs_tol=1e-9)
        again = plane.pose(*plane.units((x, y)))
        assert math.isclose(again[0], x, abs_tol=1e-12)
        assert math.isclose(again[1], y, abs_tol=1e-12)


@pytest.mark.parametrize("rows", [4, 5])
def test_u6_the_centre_of_the_plane_is_the_middle_of_the_keyboard(rows: int) -> None:
    plane = Plane(0.4, 0.55, 0.05, 0.0625, rows)
    assert plane.units((0.4, 0.55)) == (WIDTH_U / 2, rows / 2)
    assert plane.pose(WIDTH_U / 2, rows / 2) == (0.4, 0.55)


def test_u6_the_formula_of_2_3() -> None:
    plane = Plane(cx=0.5, cy=0.5, px=0.05, py=0.0625)
    u, v = plane.units((0.5 + 0.05 * 3.0, 0.5 + 0.0625 * 1.0))
    assert math.isclose(u, WIDTH_U / 2 + 3.0)
    assert math.isclose(v, 4 / 2 + 1.0)  # the default is the four-row plane
    assert plane.rows == 4


def test_u6_units_take_numpy_points_and_return_floats() -> None:
    plane = Plane(0.5, 0.5, 0.05, 0.0625, 5)
    u, v = plane.units(np.array([0.55, 0.5625]))
    assert type(u) is float
    assert type(v) is float
    assert math.isclose(u, WIDTH_U / 2 + 1.0)


def test_u6_a_plane_is_data() -> None:
    plane = Plane(0.5, 0.5, 0.05, 0.0625)
    with pytest.raises(AttributeError):
        plane.px = 1.0  # type: ignore[misc]
    assert plane == Plane(0.5, 0.5, 0.05, 0.0625, 4)


# --- A12: the formulas of 2.4 ---


def test_a12_one_left_hand_puts_a_s_d_f_under_its_fingers() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    placement = place_plane(window(left), layout=DIRECT, tuning=TUNING)
    plane = placement.plane
    assert math.isclose(plane.px, PX0)
    assert math.isclose(plane.py, 1.25 * PX0)
    assert plane.rows == 4
    # the fingertips' mean sits at u = 2.0 on the home row v = 1.5, whatever the cluster's own spread
    u, v = plane.units((0.30, float(np.mean([a[1] for a in cluster(0.30, 0.55)]))))
    assert math.isclose(u, 2.0, abs_tol=1e-9)
    assert math.isclose(v, 1.5, abs_tol=1e-9)
    assert set(placement.home) == {"left"}
    assert math.isclose(placement.home["left"][0], 2.0, abs_tol=1e-9)
    assert math.isclose(placement.home["left"][1], 1.5, abs_tol=1e-9)


def test_a12_one_right_hand_puts_j_k_l_quote_under_its_fingers() -> None:
    right = hand(2, "right", cluster(0.70, 0.55))
    placement = place_plane(window(right), layout=DIRECT, tuning=TUNING)
    u, v = placement.plane.units((0.70, float(np.mean([a[1] for a in cluster(0.70, 0.55)]))))
    assert math.isclose(u, 8.0, abs_tol=1e-9)
    assert math.isclose(v, 1.5, abs_tol=1e-9)
    assert set(placement.home) == {"right"}


def test_a12_two_hands_set_the_pitch_from_their_distance_and_centre_between_them() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    right = hand(2, "right", cluster(0.30 + 6.0 * PX0 * 1.05, 0.55))
    placement = place_plane(window(left, right), layout=DIRECT, tuning=TUNING)
    plane = placement.plane
    assert math.isclose(plane.px, PX0 * 1.05, rel_tol=1e-9)
    assert math.isclose(plane.py, 1.25 * plane.px)
    assert math.isclose(placement.home["left"][0], 2.0, abs_tol=1e-9)
    assert math.isclose(placement.home["right"][0], 8.0, abs_tol=1e-9)
    assert math.isclose(placement.home["left"][1], 1.5, abs_tol=1e-9)
    assert math.isclose(placement.home["right"][1], 1.5, abs_tol=1e-9)


@pytest.mark.parametrize(("gap", "expected"), [(0.02, 0.90), (6.0 * PX0 * 0.95, 0.95), (1.0, 1.15)])
def test_a12_the_pitch_is_clamped_to_ninety_and_one_fifteen_percent(gap: float, expected: float) -> None:
    left = hand(1, "left", cluster(0.20, 0.5))
    right = hand(2, "right", cluster(0.20 + gap, 0.5))
    plane = place_plane(window(left, right), layout=DIRECT, tuning=TUNING).plane
    assert math.isclose(plane.px, PX0 * expected, rel_tol=1e-9)


def test_a12_two_hands_are_ordered_by_position_never_by_label() -> None:
    """The labels are the engine's newest guess and flip; the hand on the left of the picture is the left cluster."""
    a = hand(1, "right", cluster(0.30, 0.55))  # labelled right, stands on the left
    b = hand(2, "left", cluster(0.30 + 6.0 * PX0, 0.55))
    placement = place_plane(window(a, b), layout=DIRECT, tuning=TUNING)
    assert math.isclose(placement.home["left"][0], 2.0, abs_tol=1e-9)
    assert math.isclose(placement.home["right"][0], 8.0, abs_tol=1e-9)
    assert placement.home_f[("left", 0)][0] < placement.home_f[("right", 0)][0]


def test_a12_the_plane_follows_the_mean_of_every_frame_of_the_window() -> None:
    frames = [[hand(1, "left", cluster(0.30 + 0.002 * k, 0.55 + 0.001 * k))] for k in range(-2, 3)]
    placement = place_plane(frames, layout=DIRECT, tuning=TUNING)
    u, v = placement.home["left"]
    assert math.isclose(u, 2.0, abs_tol=1e-9)
    assert math.isclose(v, 1.5, abs_tol=1e-9)


def test_a12_home_f_is_each_fingers_own_mean_aim() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    placement = place_plane(window(left), layout=DIRECT, tuning=TUNING)
    assert set(placement.home_f) == {("left", f) for f in range(4)}
    for finger, aim in enumerate(cluster(0.30, 0.55)):
        u, v = placement.plane.units(aim)
        assert math.isclose(placement.home_f[("left", finger)][0], u, abs_tol=1e-9)
        assert math.isclose(placement.home_f[("left", finger)][1], v, abs_tol=1e-9)
    # and home is their mean
    assert math.isclose(placement.home["left"][0], np.mean([placement.home_f[("left", f)][0] for f in range(4)]))


def test_a12_nobody_in_the_window_is_refused_with_a_fixed_text() -> None:
    with pytest.raises(ValueError, match="no hand"):
        place_plane([], layout=DIRECT, tuning=TUNING)
    with pytest.raises(ValueError, match="no hand"):
        place_plane([[], []], layout=DIRECT, tuning=TUNING)


def test_a12_three_tracks_use_the_two_that_stayed() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    right = hand(2, "right", cluster(0.30 + 6.0 * PX0, 0.55))
    ghost = hand(3, "right", cluster(0.9, 0.2))
    frames = [[left, right, ghost], [left, right], [left, right], [left, right]]
    placement = place_plane(frames, layout=DIRECT, tuning=TUNING)
    assert math.isclose(placement.plane.px, PX0, rel_tol=1e-9)
    assert set(placement.home) == {"left", "right"}


def test_a12_the_placement_is_a_record_of_the_plane_and_its_homes() -> None:
    placement = place_plane(window(hand(1, "left", cluster(0.3, 0.5))), layout=DIRECT, tuning=TUNING)
    assert isinstance(placement, Placement)
    assert isinstance(placement.plane, Plane)
    assert all(type(x) is float for pair in placement.home.values() for x in pair)


# --- U48: five rows ---


@pytest.mark.parametrize("layout", [DIRECT, REVIEW], ids=lambda layout: layout.name)
def test_u48_the_resting_fingertips_land_on_the_home_row_in_both_layouts(layout: Layout) -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    right = hand(2, "right", cluster(0.30 + 6.0 * PX0, 0.55))
    placement = place_plane(window(left, right), layout=layout, tuning=TUNING)
    assert placement.plane.rows == layout.rows
    for side in ("left", "right"):
        assert math.isclose(placement.home[side][1], layout.home_v, abs_tol=1e-9)
        assert math.isclose(placement.home[side][1], 1.5, abs_tol=1e-9)


def test_u48_the_plane_centre_lies_half_a_pitch_below_the_fingertips_in_direct_and_a_whole_one_in_review() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    mean_y = float(np.mean([a[1] for a in cluster(0.30, 0.55)]))
    direct = place_plane(window(left), layout=DIRECT, tuning=TUNING).plane
    review = place_plane(window(left), layout=REVIEW, tuning=TUNING).plane
    assert math.isclose(direct.cy - mean_y, 0.5 * direct.py, abs_tol=1e-12)
    assert math.isclose(review.cy - mean_y, 1.0 * review.py, abs_tol=1e-12)
    assert direct.cx == review.cx
    assert direct.px == review.px
    assert direct.py == review.py


def test_u48_the_letter_rows_stand_where_they_stood_in_the_direct_layout() -> None:
    """The review plane is the direct plane with one more row under it: every letter key keeps its place."""
    left = hand(1, "left", cluster(0.30, 0.55))
    direct = place_plane(window(left), layout=DIRECT, tuning=TUNING).plane
    review = place_plane(window(left), layout=REVIEW, tuning=TUNING).plane
    for key in DIRECT.keys:
        x_d, y_d = direct.pose(key.col + key.width / 2, key.row + 0.5)
        x_r, y_r = review.pose(key.col + key.width / 2, key.row + 0.5)
        if key.kind in ("backspace", "enter"):
            continue
        assert math.isclose(x_d, x_r, abs_tol=1e-12)
        assert math.isclose(y_d, y_r, abs_tol=1e-12)


def test_u48_a_fingertip_over_a_key_resolves_to_it_through_the_whole_chain() -> None:
    left = hand(1, "left", cluster(0.30, 0.55))
    for layout in (DIRECT, REVIEW):
        plane = place_plane(window(left), layout=layout, tuning=TUNING).plane
        for key in layout.keys:
            if key.kind == "chip":
                continue
            u, v = plane.units(plane.pose(key.col + key.width / 2, key.row + 0.5))
            assert layout.key_at(u, v) is key


def test_the_stub_marker_is_gone() -> None:
    assert not hasattr(pl, "STUB_OWNER")
