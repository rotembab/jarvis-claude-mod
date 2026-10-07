from __future__ import annotations

import itertools
import time

import numpy as np
import pytest

from jarvis_hands.geometry import Point
from jarvis_hands.overlay import render as reticle
from jarvis_hands.overlay.base import OverlayState

DRAWN_MODES = [m for m in reticle.MODES if m != "hidden"]


def state(mode: str, *, pinch: float = 0.0, progress: float = 0.0) -> OverlayState:
    return OverlayState(mode=mode, cursor=Point(500, 400), pinch=pinch, progress=progress)  # type: ignore[arg-type]


def lit(image: np.ndarray, threshold: int = 128) -> int:
    return int((image[..., 3] > threshold).sum())


def alpha_centroid(image: np.ndarray) -> tuple[float, float]:
    a = image[..., 3].astype(float)
    ys, xs = np.indices(a.shape)
    return float((xs * a).sum() / a.sum()), float((ys * a).sum() / a.sum())


@pytest.fixture(autouse=True)
def fresh_cache() -> None:
    reticle.clear_cache()


@pytest.mark.parametrize("mode", reticle.MODES)
@pytest.mark.parametrize("size", [96, 144, 61])
def test_every_mode_is_a_square_uint8_bgra_image(mode: str, size: int) -> None:
    image = reticle.render(state(mode, pinch=0.5, progress=0.5), size)
    assert image.shape == (size, size, 4)
    assert image.dtype == np.uint8


@pytest.mark.parametrize("mode", reticle.MODES)
@pytest.mark.parametrize(("pinch", "progress"), [(0.0, 0.0), (0.37, 0.61), (1.0, 1.0)])
def test_every_mode_is_premultiplied(mode: str, pinch: float, progress: float) -> None:
    for image in (reticle.render(state(mode, pinch=pinch, progress=progress), 96), reticle.render(state(mode), 150)):
        assert (image[..., :3] <= image[..., 3:]).all()


def test_the_helper_marker_is_premultiplied_and_amber() -> None:
    image = reticle.render_helper()
    assert image.shape == (reticle.HELPER_BASE_SIZE, reticle.HELPER_BASE_SIZE, 4)
    assert image.dtype == np.uint8
    assert (image[..., :3] <= image[..., 3:]).all()
    solid = image[image[..., 3] == 255]
    assert len(solid) > 20
    b, g, r = solid[:, 0].astype(int), solid[:, 1].astype(int), solid[:, 2].astype(int)
    assert (r > g).mean() > 0.9 and (g > b).mean() > 0.9  # amber: red over green over blue
    assert reticle.render_helper(72).shape == (72, 72, 4)


def test_hidden_is_fully_transparent() -> None:
    image = reticle.render(OverlayState(), 96)
    assert not image.any()
    assert not reticle.render(state("hidden", pinch=1.0, progress=1.0), 120).any()


@pytest.mark.parametrize("mode", DRAWN_MODES)
def test_drawn_modes_stay_inside_the_image_with_a_clear_border(mode: str) -> None:
    image = reticle.render(state(mode, pinch=1.0, progress=1.0), 96)
    assert lit(image) > 0 or mode == "idle"
    assert image[..., 3].max() > 60
    border = np.concatenate([image[0], image[-1], image[:, 0], image[:, -1]])
    assert not border.any(), "a shape or its halo runs into the window's edge and would be cut off"


@pytest.mark.parametrize("mode", [m for m in DRAWN_MODES if m != "engaging"])  # engaging marks 12 o'clock
def test_the_reticle_is_centred_on_the_centre_pixel(mode: str) -> None:
    size = 96
    cx, cy = alpha_centroid(reticle.render(state(mode, pinch=1.0, progress=1.0), size))
    assert cx == pytest.approx(size // 2, abs=0.6)
    assert cy == pytest.approx(size // 2, abs=0.6)


def test_each_mode_looks_different_from_every_other() -> None:
    for params in ({}, {"pinch": 0.5, "progress": 0.5}):
        images = {mode: reticle.render(state(mode, **params), 96) for mode in reticle.MODES}
        for a, b in itertools.combinations(reticle.MODES, 2):
            assert not np.array_equal(images[a], images[b]), f"{a} and {b} look the same with {params}"


def test_idle_is_a_faint_ring() -> None:
    image = reticle.render(state("idle"), 96)
    assert 0 < image[..., 3].max() < 140
    assert lit(reticle.render(state("point"), 96)) > 0 and lit(image) == 0


def test_the_pinch_arc_grows_with_the_pinch() -> None:
    counts = [lit(reticle.render(state("point", pinch=p), 96)) for p in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)]
    assert counts == sorted(counts) and len(set(counts)) == len(counts), counts


def test_the_engaging_arc_fills_with_progress() -> None:
    counts = [lit(reticle.render(state("engaging", progress=p), 96)) for p in (0.0, 0.1, 0.3, 0.5, 0.8, 1.0)]
    assert counts == sorted(counts) and len(set(counts)) == len(counts), counts


def test_the_engaging_arc_starts_at_twelve_and_runs_clockwise() -> None:
    quarter = reticle.render(state("engaging", progress=0.25), 96).astype(int)
    c = 96 // 2
    top_right = quarter[c - 30 : c - 5, c + 5 : c + 30, 3].sum()
    top_left = quarter[c - 30 : c - 5, c - 30 : c - 5, 3].sum()
    bottom_right = quarter[c + 5 : c + 30, c + 5 : c + 30, 3].sum()
    assert top_right > 2 * top_left and top_right > 2 * bottom_right


def test_calibration_progress_fills_too() -> None:
    counts = [lit(reticle.render(state("calibrate", progress=p), 144)) for p in (0.0, 0.3, 0.6, 1.0)]
    assert counts == sorted(counts) and len(set(counts)) == len(counts), counts


def test_grab_and_resize_are_amber_and_point_is_cyan() -> None:
    def mean_bgr(mode: str) -> np.ndarray:
        image = reticle.render(state(mode), 96)
        return image[image[..., 3] == 255][:, :3].astype(float).mean(axis=0)

    b, g, r = mean_bgr("grab")
    assert r > g > b
    b, g, r = mean_bgr("resize")
    assert r > g > b
    b, g, r = mean_bgr("scroll")
    assert b > r and g > r


def test_strokes_have_a_dark_halo_for_light_backgrounds() -> None:
    image = reticle.render(state("point"), 96).astype(int)
    # Just outside the ring (radius 21, 2 px wide): dark, partly opaque pixels.
    halo = image[96 // 2, 96 // 2 + 23]
    assert halo[3] > 40 and max(halo[:3]) < halo[3] // 2


def test_the_cache_returns_the_same_object_for_the_same_quantized_state() -> None:
    a = reticle.render(state("point", pinch=0.5), 96)
    assert reticle.render(state("point", pinch=0.5 + 0.2 / reticle.QUANTUM), 96) is a
    assert reticle.render(state("point", pinch=0.5 + 1 / reticle.QUANTUM), 96) is not a
    assert reticle.render(state("point", pinch=0.5), 97) is not a
    # Modes that draw no pinch or progress ignore them.
    press = reticle.render(state("press", pinch=0.1, progress=0.2), 96)
    assert reticle.render(state("press", pinch=0.9, progress=0.7), 96) is press
    assert reticle.render(state("calibrate", progress=0.4), 144) is reticle.render(
        state("calibrate", progress=0.4), 144
    )
    assert reticle.render_helper(48) is reticle.render_helper(48)


def test_cached_images_are_read_only() -> None:
    image = reticle.render(state("point"), 96)
    with pytest.raises(ValueError):
        image[0, 0, 0] = 1
    with pytest.raises(ValueError):
        reticle.render_helper()[0, 0, 0] = 1


def test_out_of_range_and_nan_values_are_clamped() -> None:
    assert reticle.render(state("point", pinch=-3.0), 96) is reticle.render(state("point", pinch=0.0), 96)
    assert reticle.render(state("point", pinch=7.0), 96) is reticle.render(state("point", pinch=1.0), 96)
    assert reticle.render(state("engaging", progress=float("nan")), 96) is reticle.render(state("engaging"), 96)


def test_output_is_deterministic() -> None:
    # 0.5 and 0.75 are exact quanta, so the cached render must equal a fresh draw pixel for pixel.
    for mode in reticle.MODES:
        first = reticle.draw(mode, 96, 0.5, 0.75)  # type: ignore[arg-type]
        assert np.array_equal(first, reticle.draw(mode, 96, 0.5, 0.75))  # type: ignore[arg-type]
        assert np.array_equal(first, reticle.render(state(mode, pinch=0.5, progress=0.75), 96))


def test_bad_sizes_and_modes_are_refused() -> None:
    with pytest.raises(ValueError):
        reticle.render(state("point"), 4)
    with pytest.raises(TypeError):
        reticle.render(state("point"), 96.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        reticle.render(OverlayState(mode="sparkle"), 96)  # type: ignore[arg-type]


def test_an_uncached_render_is_fast() -> None:
    timings = []
    for mode in DRAWN_MODES:
        for _ in range(3):
            start = time.perf_counter()
            reticle.draw(mode, 96, 0.5, 0.5)  # type: ignore[arg-type]
            timings.append(time.perf_counter() - start)
    # About 0.5 ms on a laptop core; the bound leaves room for slow CI machines.
    assert float(np.median(timings)) < 0.010, f"median {np.median(timings) * 1000:.2f} ms"


def test_sizes_follow_the_monitor_dpi() -> None:
    assert reticle.reticle_size("point", 96) == 96
    assert reticle.reticle_size("point", 144) == 144
    assert reticle.reticle_size("grab", 120) == 120
    assert reticle.reticle_size("calibrate", 96) == 144
    assert reticle.reticle_size("calibrate", 192) == 288
    assert reticle.helper_size(96) == 48 and reticle.helper_size(144) == 72
    assert reticle.reticle_size("point", 0) == 96  # an unknown DPI counts as 100 %


@pytest.mark.parametrize(
    ("point", "size", "expected"),
    [
        (Point(500, 400), 96, (452, 352)),
        (Point(500.4, 399.6), 97, (452, 352)),
        (Point(-1500, -20), 144, (-1572, -92)),
        (Point(0, 0), 96, (-48, -48)),
    ],
)
def test_top_left_puts_the_centre_pixel_on_the_point(point: Point, size: int, expected: tuple[int, int]) -> None:
    assert reticle.top_left(point, size) == expected
    x, y = reticle.top_left(point, size)
    assert (x + size // 2, y + size // 2) == point.rounded()
