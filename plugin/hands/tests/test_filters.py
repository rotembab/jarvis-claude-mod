from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from jarvis_hands.filters import OneEuroFilter, OneEuroFilter2D, smoothing_factor
from jarvis_hands.geometry import Point


def test_first_sample_passes_through_and_constant_stays_constant() -> None:
    f = OneEuroFilter(1.0, 0.004, 1.0)
    assert f.filter(5.0, 0.0) == 5.0
    for i in range(1, 30):
        assert f.filter(5.0, i / 30) == pytest.approx(5.0)


def test_one_step_matches_casiez_low_pass() -> None:
    f = OneEuroFilter(min_cutoff=2.0, beta=0.0, d_cutoff=1.0)
    f.filter(0.0, 0.0)
    dt = 1 / 30
    alpha = 1.0 / (1.0 + (1.0 / (2 * math.pi * 2.0)) / dt)
    assert smoothing_factor(2.0, dt) == pytest.approx(alpha)
    assert f.filter(10.0, dt) == pytest.approx(10.0 * alpha)


@pytest.mark.parametrize("dt", [0.0, -0.01])
def test_repeated_or_backwards_timestamp_returns_last_output(dt: float) -> None:
    f = OneEuroFilter(1.0, 0.0, 1.0)
    f.filter(0.0, 1.0)
    out = f.filter(10.0, 1.0 + 1 / 30)
    assert f.filter(99.0, 1.0 + 1 / 30 + dt) == out
    f2 = OneEuroFilter2D(1.0, 0.0, 1.0)
    f2.filter(Point(0, 0), 1.0)
    p = f2.filter(Point(10, 10), 1.1)
    assert f2.filter(Point(99, 99), 1.1 + dt) == p
    assert math.isfinite(f2.speed)


def test_step_converges_gradually() -> None:
    f = OneEuroFilter(1.0, 0.0, 1.0)
    f.filter(0.0, 0.0)
    outs = [f.filter(100.0, i / 30) for i in range(1, 120)]
    assert 0 < outs[0] < 30
    assert all(b >= a for a, b in itertools.pairwise(outs))
    assert outs[-1] == pytest.approx(100.0, abs=0.5)


def test_beta_reduces_lag_on_fast_motion() -> None:
    def lag(beta: float) -> float:
        f = OneEuroFilter(1.0, beta, 1.0)
        last = 0.0
        for i in range(31):
            last = f.filter(2000.0 * i / 30, i / 30)  # 2000 px/s sweep
        return 2000.0 - last

    assert lag(0.004) < 0.6 * lag(0.0)


def test_jitter_is_smoothed_at_rest() -> None:
    rng = np.random.default_rng(1)
    f = OneEuroFilter2D(1.0, 0.004, 1.0)
    noisy = [Point(960 + rng.normal(0, 4), 540 + rng.normal(0, 4)) for _ in range(300)]
    outs = [f.filter(p, i / 30) for i, p in enumerate(noisy)]
    raw_std = np.std([p.x for p in noisy[100:]])
    out_std = np.std([p.x for p in outs[100:]])
    assert out_std < raw_std / 2.5


def test_reset_restarts_from_the_next_sample() -> None:
    f = OneEuroFilter(1.0, 0.0, 1.0)
    f.filter(0.0, 0.0)
    f.filter(50.0, 0.1)
    f.reset()
    assert f.value is None
    assert f.filter(80.0, 0.2) == 80.0
    f2 = OneEuroFilter2D()
    f2.filter(Point(0, 0), 0.0)
    f2.filter(Point(50, 0), 0.1)
    f2.reset()
    assert f2.speed == 0.0
    assert f2.filter(Point(7, 8), 0.2) == Point(7, 8)


def test_2d_speed_tracks_a_steady_sweep() -> None:
    f = OneEuroFilter2D(1.0, 0.004, 1.0)
    for i in range(90):
        f.filter(Point(600.0 * i / 30, 800.0 * i / 30), i / 30)  # 1000 px/s diagonally
    assert f.speed == pytest.approx(1000.0, rel=0.05)
    assert f.velocity.x == pytest.approx(600.0, rel=0.05)


def test_2d_with_beta_zero_matches_two_1d_filters() -> None:
    f2 = OneEuroFilter2D(1.5, 0.0, 1.0)
    fx, fy = OneEuroFilter(1.5, 0.0, 1.0), OneEuroFilter(1.5, 0.0, 1.0)
    rng = np.random.default_rng(2)
    for i in range(50):
        p = Point(rng.uniform(0, 100), rng.uniform(0, 100))
        out = f2.filter(p, i / 30)
        assert out.x == pytest.approx(fx.filter(p.x, i / 30))
        assert out.y == pytest.approx(fy.filter(p.y, i / 30))


def test_2d_keeps_a_diagonal_straight() -> None:
    f = OneEuroFilter2D(1.0, 0.004, 1.0)
    for i in range(40):
        out = f.filter(Point(10.0 * i, 3.0 * i), i / 30)
        if i:
            assert out.y / out.x == pytest.approx(0.3, rel=1e-6)


def test_bad_cutoffs_are_refused() -> None:
    with pytest.raises(ValueError):
        OneEuroFilter(0.0)
    with pytest.raises(ValueError):
        OneEuroFilter2D(1.0, 0.0, -1.0)
