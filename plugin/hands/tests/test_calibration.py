from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from jarvis_hands.calibration import CORNER_TARGETS, CORNERS, CalibrationFlow, fit_homography
from jarvis_hands.geometry import Point, apply_homography
from jarvis_hands.poses import HandPose
from jarvis_hands.settings import HandsSettings, load_calibration, save_calibration

QUAD = [Point(0.30, 0.25), Point(0.75, 0.30), Point(0.70, 0.75), Point(0.25, 0.70)]


def pose(at: Point, name: str = "palm") -> HandPose:
    return HandPose(name, at, 0.0, (True,) * 4, (False,) * 4)  # type: ignore[arg-type]


def hold(
    flow: CalibrationFlow, at: Point, start: float, seconds: float, *, name: str = "palm"
) -> tuple[list[str], float]:
    events: list[str] = []
    t = start
    for _ in range(round(seconds * 30)):
        events += flow.update(pose(at, name), t)
        t += 1 / 30
    return events, t


def test_corner_order_and_targets() -> None:
    assert CORNERS == ("top_left", "top_right", "bottom_right", "bottom_left")
    assert [CORNER_TARGETS[c] for c in CORNERS] == [(0, 0), (1, 0), (1, 1), (0, 1)]


def test_fit_homography_maps_the_corners_onto_the_unit_square() -> None:
    h = fit_homography(QUAD)
    for p, corner in zip(QUAD, CORNERS, strict=True):
        u = apply_homography(h, p)
        assert (u.x, u.y) == pytest.approx(CORNER_TARGETS[corner], abs=1e-5)
    centre = apply_homography(h, Point(0.5, 0.5))
    assert 0.3 < centre.x < 0.7 and 0.3 < centre.y < 0.7


@pytest.mark.parametrize(
    "points",
    [
        [Point(0.2, 0.2), Point(0.5, 0.2), Point(0.8, 0.2), Point(0.2, 0.7)],  # three collinear
        [Point(0.5, 0.5), Point(0.55, 0.5), Point(0.55, 0.55), Point(0.5, 0.55)],  # tiny
        [Point(0.3, 0.3), Point(0.7, 0.7), Point(0.7, 0.3), Point(0.3, 0.7)],  # self-intersecting
        [Point(0.2, 0.2), Point(0.8, 0.2), Point(0.45, 0.35), Point(0.2, 0.8)],  # not convex (a dart)
        [Point(0.3, 0.3)] * 4,
        QUAD[:3],
        [Point(float("nan"), 0.3), *QUAD[1:]],
    ],
)
def test_fit_homography_refuses_degenerate_corners(points: list[Point]) -> None:
    with pytest.raises(ValueError):
        fit_homography(points)


@pytest.mark.parametrize(
    "points",
    [
        [Point(0.75, 0.25), Point(0.25, 0.25), Point(0.25, 0.70), Point(0.75, 0.70)],  # left-right
        [Point(0.25, 0.70), Point(0.70, 0.75), Point(0.75, 0.30), Point(0.30, 0.25)],  # top-bottom
    ],
)
def test_fit_homography_accepts_a_mirror_image_quad(points: list[Point]) -> None:
    # A camera that already mirrors its image: the fit is what puts the cursor the right way round.
    h = fit_homography(points)
    for p, corner in zip(points, CORNERS, strict=True):
        u = apply_homography(h, p)
        assert (u.x, u.y) == pytest.approx(CORNER_TARGETS[corner], abs=1e-5)


def test_a_mirrored_camera_calibrates() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    events: list[str] = []
    t = 0.0
    for p in [Point(0.75, 0.25), Point(0.25, 0.25), Point(0.25, 0.70), Point(0.75, 0.70)]:
        got, t = hold(flow, p, t, 1.2)
        events += got
    assert events == ["top_right", "bottom_right", "bottom_left", "done"]
    assert flow.result is not None
    assert apply_homography(flow.result, Point(0.3, 0.45)).x > 0.8  # the hand's right is the cursor's right


def test_flow_captures_four_corners_and_fits() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    assert flow.active and flow.corner == "top_left" and flow.progress == 0.0
    events: list[str] = []
    t = 0.0
    for p in QUAD:
        got, t = hold(flow, p, t, 1.2)
        events += got
    assert events == ["top_right", "bottom_right", "bottom_left", "done"]
    assert not flow.active and flow.corner is None
    assert flow.result is not None
    for p, corner in zip(QUAD, CORNERS, strict=True):
        u = apply_homography(flow.result, p)
        assert (u.x, u.y) == pytest.approx(CORNER_TARGETS[corner], abs=1e-5)


def test_progress_rises_during_a_hold() -> None:
    flow = CalibrationFlow(HandsSettings(), hold_s=1.0)
    flow.start(0.0)
    hold(flow, QUAD[0], 0.0, 0.5)
    assert 0.4 < flow.progress < 0.6
    assert flow.corner == "top_left"


def test_the_capture_is_the_mean_anchor_over_the_hold() -> None:
    flow = CalibrationFlow(HandsSettings(), still_speed=0.5)
    flow.start(0.0)
    t = 0.0
    for i in range(40):  # a slow wobble around (0.3, 0.25)
        flow.update(pose(Point(0.3 + (0.004 if i % 2 else -0.004), 0.25)), t)
        t += 1 / 30
        if flow.corner != "top_left":
            break
    assert flow.points[0].x == pytest.approx(0.3, abs=0.001)


def test_moving_restarts_the_hold() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    _, t = hold(flow, QUAD[0], 0.0, 0.8)
    flow.update(pose(Point(0.45, 0.25)), t)  # jumps away: far too fast to be still
    assert flow.progress == 0.0
    events, _ = hold(flow, Point(0.45, 0.25), t + 1 / 30, 0.8)
    assert events == [] and flow.corner == "top_left"


def test_only_an_open_palm_counts() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    events, t = hold(flow, QUAD[0], 0.0, 2.0, name="fist")
    assert events == [] and flow.progress == 0.0
    events, _ = hold(flow, QUAD[0], t, 1.2)
    assert events == ["top_right"]


def test_a_dropped_frame_keeps_the_hold_but_a_longer_gap_restarts_it() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    _, t = hold(flow, QUAD[0], 0.0, 0.6)
    flow.update(None, t)
    assert flow.progress > 0.5
    events, t = hold(flow, QUAD[0], t + 1 / 30, 0.5)
    assert events == ["top_right"]
    _, t = hold(flow, QUAD[1], t, 0.6)
    for _ in range(15):
        flow.update(None, t)
        t += 1 / 30
    assert flow.progress == 0.0


def test_a_hand_resting_on_the_last_corner_does_not_capture_the_next_one() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    events, _ = hold(flow, QUAD[0], 0.0, 3.5)
    assert events == ["top_right"]
    assert flow.corner == "top_right"


def test_degenerate_corners_start_over() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    events: list[str] = []
    t = 0.0
    for p in [Point(0.2, 0.3), Point(0.5, 0.3), Point(0.8, 0.3), Point(0.2, 0.7)]:  # first three collinear
        got, t = hold(flow, p, t, 1.2)
        events += got
    assert events == ["top_right", "bottom_right", "bottom_left", "top_left"]
    assert flow.active and flow.corner == "top_left" and flow.result is None


def test_corners_that_keep_failing_to_fit_end_in_cancelled() -> None:
    flow = CalibrationFlow(HandsSettings(), timeout_s=30.0)
    flow.start(0.0)
    events: list[str] = []
    t = 0.0
    while flow.active and t < 120.0:  # the same collinear corners, round after round
        for p in [Point(0.2, 0.3), Point(0.5, 0.3), Point(0.8, 0.3), Point(0.2, 0.7)]:
            got, t = hold(flow, p, t, 1.2)
            events += got
    assert events[-1] == "cancelled" and "done" not in events
    assert t < 40.0  # 30 s after the last corner that was real progress


def test_timeout_cancels() -> None:
    flow = CalibrationFlow(HandsSettings(), timeout_s=30.0)
    flow.start(0.0)
    assert flow.update(None, 29.0) == []
    assert flow.update(None, 30.5) == ["cancelled"]
    assert not flow.active and flow.result is None
    assert flow.update(pose(QUAD[0]), 31.0) == []


def test_progress_resets_the_timeout() -> None:
    flow = CalibrationFlow(HandsSettings(), timeout_s=5.0)
    flow.start(0.0)
    _, t = hold(flow, QUAD[0], 3.0, 1.2)
    assert flow.corner == "top_right"
    assert flow.update(None, t + 4.0) == []
    assert flow.update(None, t + 5.5) == ["cancelled"]


def test_cancel_and_restart() -> None:
    flow = CalibrationFlow(HandsSettings())
    flow.start(0.0)
    hold(flow, QUAD[0], 0.0, 1.2)
    flow.cancel()
    assert not flow.active and flow.corner is None and flow.progress == 0.0
    flow.start(10.0)
    assert flow.corner == "top_left" and flow.points == [] and flow.result is None


# -- the calibration file ------------------------------------------------------------------


def test_calibration_file_round_trip(tmp_path: Path) -> None:
    h = fit_homography(QUAD)
    path = save_calibration(tmp_path, h)
    assert path == tmp_path / "hands" / "calibration.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["homography"]) == 3
    loaded = load_calibration(tmp_path)
    assert loaded is not None and np.allclose(loaded, h)
    assert [p.name for p in path.parent.iterdir()] == ["calibration.json"]  # no temp file left behind
    save_calibration(tmp_path, np.eye(3))
    assert np.allclose(load_calibration(tmp_path), np.eye(3))  # type: ignore[arg-type]


def test_missing_calibration_loads_as_none(tmp_path: Path) -> None:
    assert load_calibration(tmp_path) is None


IDENTITY = '{"version": 1, "homography": [[1,0,0],[0,1,0],[0,0,1]]}'


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b'{"version": 2, "homography": [[1,0,0],[0,1,0],[0,0,1]]}',
        b'{"version": 1, "homography": [[1,0],[0,1]]}',
        b'{"version": 1, "homography": [[0,0,0],[0,0,0],[0,0,0]]}',
        b'{"version": 1, "homography": [[1,0,0],[0,1,0],[0,0,"x"]]}',
        b'{"version": 1, "homography": [[1,0,0],[0,1,0],[0,0,NaN]]}',
        b"[]",
        b"\xff\xfe\x00garbage\x80\x81",  # not UTF-8 at all
        IDENTITY.encode("utf-16"),  # old Notepad's "Unicode"
        ('{"version": 1, "homography": [[1' + "0" * 400 + ",0,0],[0,1,0],[0,0,1]]}").encode(),  # too big for a float
        b'{"version": 1, "homography": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",  # nested past the recursion limit
    ],
    ids=[
        "not-json",
        "version-2",
        "2x2",
        "singular",
        "string-entry",
        "nan",
        "list",
        "not-utf8",
        "utf16",
        "huge-int",
        "deep-nesting",
    ],
)
def test_corrupt_calibration_loads_as_none_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    path = tmp_path / "hands" / "calibration.json"
    path.parent.mkdir()
    path.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert load_calibration(tmp_path) is None
    assert "calibration" in caplog.text


def test_a_calibration_saved_with_a_utf8_bom_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "hands" / "calibration.json"
    path.parent.mkdir()
    path.write_bytes(IDENTITY.encode("utf-8-sig"))  # Notepad's "UTF-8 with BOM"
    loaded = load_calibration(tmp_path)
    assert loaded is not None and np.allclose(loaded, np.eye(3))


def test_saving_a_bad_matrix_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        save_calibration(tmp_path, np.zeros((3, 3)))
    with pytest.raises(ValueError):
        save_calibration(tmp_path, np.eye(2))
    assert not (tmp_path / "hands" / "calibration.json").exists()
