from __future__ import annotations

import threading

import numpy as np
import pytest

from jarvis_hands.geometry import Point, Rect, box_homography
from jarvis_hands.mapping import ScreenMapper, display_in_direction, select_displays
from jarvis_hands.settings import HandsSettings

from scripted import camera_point, display

# A 1920x1080 primary, a 1280x720 projector to its right (top-aligned) and a virtual display further right.
PRIMARY = display(1, 0, 0, 1920, 1080, primary=True)
PROJECTOR = display(2, 1920, 0, 1280, 720)
VIRTUAL = display(3, 3200, 0, 1920, 1080, virtual=True, name="Virtual Display")
DESK = [PRIMARY, PROJECTOR, VIRTUAL]


def test_select_all_excludes_virtual_displays() -> None:
    assert select_displays(DESK, "all") == [PRIMARY, PROJECTOR]


def test_select_all_keeps_virtual_displays_when_there_is_nothing_else() -> None:
    assert select_displays([VIRTUAL], "all") == [VIRTUAL]


def test_select_ids() -> None:
    assert select_displays(DESK, (2,)) == [PROJECTOR]
    assert select_displays(DESK, (3, 1)) == [PRIMARY, VIRTUAL]  # display order, a virtual one when asked for
    assert select_displays(DESK, (9,)) == [PRIMARY, PROJECTOR]  # none exists: all
    assert select_displays(DESK, ()) == [PRIMARY, PROJECTOR]


def test_display_in_direction_side_by_side() -> None:
    assert display_in_direction(DESK, PRIMARY, "right") == PROJECTOR
    assert display_in_direction(DESK, PROJECTOR, "left") == PRIMARY
    assert display_in_direction(DESK, PRIMARY, "left") is None
    assert display_in_direction(DESK, PRIMARY, "up") is None
    assert display_in_direction([PRIMARY, PROJECTOR], PROJECTOR, "right") is None


def test_display_in_direction_prefers_the_nearest_overlapping_display() -> None:
    right_low = display(4, 1920, 1080, 1920, 1080)  # diagonal: down-right, no vertical overlap
    far_right = display(5, 3840, 0, 1920, 1080)
    assert display_in_direction([PRIMARY, right_low, far_right], PRIMARY, "right") == far_right
    near_right = display(6, 1920, 200, 1280, 1024)
    assert display_in_direction([PRIMARY, far_right, near_right], PRIMARY, "right") == near_right


def test_display_in_direction_falls_back_to_the_nearest_centre() -> None:
    diagonal = display(4, 1920, 1080, 1920, 1080)
    farther = display(5, 5000, 3000, 1920, 1080)
    assert display_in_direction([PRIMARY, farther, diagonal], PRIMARY, "right") == diagonal


def test_display_in_direction_vertical_and_negative_coordinates() -> None:
    above = display(2, -200, -1440, 2560, 1440)
    left = display(3, -1920, 0, 1920, 1080)
    displays = [PRIMARY, above, left]
    assert display_in_direction(displays, PRIMARY, "up") == above
    assert display_in_direction(displays, above, "down") == PRIMARY
    assert display_in_direction(displays, PRIMARY, "left") == left
    assert display_in_direction(displays, left, "right") == PRIMARY


def test_default_box_maps_onto_the_region() -> None:
    mapper = ScreenMapper([PRIMARY], HandsSettings())
    assert not mapper.calibrated
    assert mapper.region == Rect(0, 0, 1920, 1080)
    assert mapper.to_target(Point(0.2, 0.2)) == Point(pytest.approx(0.0), pytest.approx(0.0))
    assert mapper.to_target(Point(0.8, 0.7)) == Point(pytest.approx(1.0), pytest.approx(1.0))
    centre = mapper.to_desktop(Point(0.5, 0.45))
    assert centre == Point(pytest.approx(960), pytest.approx(540))


def test_overshoot_clamps_to_edges_and_corners() -> None:
    mapper = ScreenMapper([PRIMARY], HandsSettings())
    assert mapper.to_target(Point(0.95, 0.1)).x > 1.0  # to_target does not clamp
    assert mapper.to_desktop(Point(0.95, 0.1)) == Point(1919, 0)
    assert mapper.to_desktop(Point(0.0, 0.99)) == Point(0, 1079)
    assert mapper.to_region(Point(0.95, 0.1)).x > 1920  # the unclamped motion point


def test_two_displays_of_different_heights_plus_an_excluded_virtual_one() -> None:
    mapper = ScreenMapper(DESK, HandsSettings())
    assert mapper.used == [PRIMARY, PROJECTOR]
    assert mapper.region == Rect(0, 0, 3200, 1080)
    # Left part of the box: the primary; right part: the projector.
    on_primary = mapper.to_desktop(Point(*camera_point(0.25, 0.5)))
    assert on_primary == Point(pytest.approx(800), pytest.approx(540))
    on_projector = mapper.to_desktop(Point(*camera_point(0.8, 0.3)))
    assert on_projector == Point(pytest.approx(2560), pytest.approx(324))
    assert mapper.display_at(on_projector) == PROJECTOR
    # Below the projector there is no screen: the point clamps into the nearest display.
    gap = mapper.to_desktop(Point(*camera_point(0.9, 0.9)))
    assert PROJECTOR.rect.contains(gap) and gap.y == 719
    gap_near_primary = mapper.to_desktop(Point(*camera_point(0.61, 0.95)))
    assert PRIMARY.rect.contains(gap_near_primary) and gap_near_primary.x == 1919
    # Nothing ever maps onto the virtual display.
    assert not VIRTUAL.rect.contains(mapper.to_desktop(Point(0.99, 0.5)))


def test_choosing_displays() -> None:
    settings = HandsSettings(displays=(2,))
    mapper = ScreenMapper(DESK, settings)
    assert mapper.used == [PROJECTOR]
    assert mapper.region == PROJECTOR.rect
    assert mapper.to_desktop(Point(0.5, 0.45)) == Point(pytest.approx(2560), pytest.approx(360))
    mapper.set_selection((3,))
    assert mapper.used == [VIRTUAL]
    mapper.set_selection("all")
    assert mapper.region == Rect(0, 0, 3200, 1080)


def test_negative_coordinates_left_of_the_primary() -> None:
    left = display(2, -1920, 0, 1920, 1080)
    mapper = ScreenMapper([PRIMARY, left], HandsSettings())
    assert mapper.region == Rect(-1920, 0, 3840, 1080)
    p = mapper.to_desktop(Point(*camera_point(0.1, 0.5)))
    assert p.x == pytest.approx(-1920 + 384) and mapper.display_at(p) == left


def test_set_displays_recomputes_the_region() -> None:
    mapper = ScreenMapper([PRIMARY], HandsSettings())
    mapper.set_displays(DESK)
    assert mapper.region == Rect(0, 0, 3200, 1080)
    assert mapper.displays == DESK


def test_calibrated_homography() -> None:
    mapper = ScreenMapper([PRIMARY], HandsSettings())
    h = box_homography(0.1, 0.1, 0.9, 0.9)
    mapper.set_homography(h)
    assert mapper.calibrated
    assert np.allclose(mapper.homography, h)
    assert mapper.to_desktop(Point(0.5, 0.5)) == Point(pytest.approx(960), pytest.approx(540))
    assert mapper.to_target(Point(0.1, 0.9)) == Point(pytest.approx(0.0), pytest.approx(1.0))
    mapper.set_homography(None)
    assert not mapper.calibrated
    assert mapper.to_target(Point(0.2, 0.2)) == Point(pytest.approx(0.0), pytest.approx(0.0))
    with pytest.raises(ValueError):
        mapper.set_homography(np.eye(2))


def test_initial_homography_and_custom_box() -> None:
    settings = HandsSettings(box=(0.0, 0.0, 1.0, 1.0))
    assert ScreenMapper([PRIMARY], settings).to_desktop(Point(0.25, 0.5)) == Point(480, 540)
    calibrated = ScreenMapper([PRIMARY], settings, homography=box_homography(0.5, 0.5, 1.0, 1.0))
    assert calibrated.calibrated
    assert calibrated.to_desktop(Point(0.75, 0.75)) == Point(960, 540)


def test_target_point_clamps_like_to_desktop() -> None:
    mapper = ScreenMapper(DESK, HandsSettings())
    assert mapper.target_point(0, 0) == Point(0, 0)
    assert mapper.target_point(1, 0) == Point(3199, 0)
    assert mapper.target_point(1, 1) == Point(3199, 719)  # the projector's corner, not the empty region corner
    assert mapper.target_point(0, 1) == Point(0, 1079)


def test_display_at_falls_back_to_the_nearest() -> None:
    mapper = ScreenMapper(DESK, HandsSettings())
    assert mapper.display_at(Point(100, 100)) == PRIMARY
    assert mapper.display_at(Point(3000, 1000)) == PROJECTOR
    assert mapper.display_at(Point(4000, 100)) == PROJECTOR  # the virtual display is not used


def test_concurrent_reconfiguration_while_mapping() -> None:
    mapper = ScreenMapper(DESK, HandsSettings())
    errors: list[BaseException] = []
    stop = threading.Event()

    def reconfigure() -> None:
        while not stop.is_set():
            mapper.set_selection((2,))
            mapper.set_selection("all")
            mapper.set_displays(list(DESK))

    thread = threading.Thread(target=reconfigure)
    thread.start()
    try:
        for i in range(3000):
            p = mapper.to_desktop(Point((i % 100) / 100, 0.5))
            if not (PRIMARY.rect.contains(p) or PROJECTOR.rect.contains(p)):
                errors.append(AssertionError(p))
    finally:
        stop.set()
        thread.join()
    assert not errors


# -- live settings (the protocol's config command) -------------------------------------------


def test_apply_config_takes_the_protocol_body() -> None:
    settings = HandsSettings()
    settings.apply_config({"engage": "always", "displays": [2, 1, 2], "hand": "left", "anchor": "index"})
    assert (settings.engage, settings.displays, settings.hand, settings.anchor) == ("always", (2, 1), "left", "index")
    settings.apply_config({"overlay": False, "scrollSpeed": 2.5})
    assert settings.overlay is False and settings.scroll_speed == 2.5
    assert settings.engage == "always"  # absent keys keep their value
    settings.apply_config({"displays": "all"})
    assert settings.displays == "all"
    settings.apply_config({})
    assert settings.hand == "left"


@pytest.mark.parametrize(
    "body",
    [
        {"engage": "wave"},
        {"hand": "both"},
        {"anchor": "wrist"},
        {"overlay": "yes"},
        {"scrollSpeed": 0},
        {"scrollSpeed": True},
        {"displays": "some"},
        {"displays": [0]},
        {"displays": [1, "2"]},
        {"hand": "left", "scrollSpeed": 99},
    ],
)
def test_apply_config_refuses_bad_values_and_changes_nothing(body: dict) -> None:
    settings = HandsSettings()
    with pytest.raises(ValueError):
        settings.apply_config(body)
    assert settings == HandsSettings()


def test_status_matches_the_protocol_schema() -> None:
    import json
    from pathlib import Path

    import jsonschema

    schema_path = Path(__file__).resolve().parents[2] / "protocol" / "hands.schema.json"
    document = json.loads(schema_path.read_text(encoding="utf-8"))
    schema = document["$defs"]["StatusResponse"]["properties"]["settings"]
    settings = HandsSettings()
    settings.apply_config({"engage": "always", "scrollSpeed": 3})
    status = settings.status()
    jsonschema.Draft202012Validator({**schema, "$defs": document["$defs"]}).validate(status)
    assert status == {"engage": "always", "hand": "any", "anchor": "knuckles", "overlay": True, "scrollSpeed": 3.0}


def test_config_selection_reaches_the_mapper() -> None:
    settings = HandsSettings()
    mapper = ScreenMapper(DESK, settings)
    settings.apply_config({"displays": [2]})
    mapper.set_selection(settings.displays)
    assert mapper.used == [PROJECTOR]
