"""Draws the reticle: one premultiplied BGRA image per overlay state, in plain numpy.

Every shape (ring, arc, disc, segment) is a signed distance to the shape over
the pixel grid, and coverage is ``clamp(0.5 - d, 0, 1)``: one pixel of
anti-aliasing at any size, no drawing library, and the same pixels on every
OS, so the tests here check exactly what Windows shows. A soft dark halo under
the bright strokes (the same distances with a wider falloff) keeps the cyan
readable on white pages as well as dark ones; the calibration target gets a
crisp dark outline too, since it has to be found on any background.

Pixels are premultiplied (every colour channel at most alpha), which is what
``UpdateLayeredWindow`` with ``AC_SRC_ALPHA`` takes. The reticle's centre is the
centre of pixel ``(size // 2, size // 2)``, so ``top_left`` puts that pixel on
the cursor's pixel.

The runtime asks for a frame at the camera's rate, so renders are cached by
mode, size and pinch / progress quantized to 1/32 (finer steps are invisible
at this size), and only for the modes that draw them. A frame is then usually
a dict lookup, and the Windows backend skips copying an image it already shows
(the same, read-only, object comes back).
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from functools import lru_cache
from typing import NamedTuple, get_args

import numpy as np

from ..geometry import Point
from .base import OverlayMode, OverlayState

MODES: tuple[OverlayMode, ...] = get_args(OverlayMode)

#: The reticle's size in pixels at 96 DPI (100 % scaling); ``reticle_size`` scales it.
BASE_SIZE = 96
#: The second hand's marker during a two-hand resize, at 96 DPI.
HELPER_BASE_SIZE = 48
#: The calibration target is drawn this much larger, so it is found at a glance.
CALIBRATE_SCALE = 1.5
#: Smallest image ``render`` draws.
MIN_SIZE = 8
#: Pinch and progress steps per unit.
QUANTUM = 32
CACHE_SIZE = 128

#: Colours as BGR in 0..1 (the layered window's byte order).
CYAN = (1.0, 0xE5 / 255, 0.0)  # #00E5FF
AMBER = (0.0, 0xB3 / 255, 1.0)  # #FFB300
WHITE = (1.0, 1.0, 1.0)
SHADOW = (0.10, 0.05, 0.0)  # a deep navy, softer than black under cyan

#: Peak opacity of the dark halo under a fully opaque stroke (scaled by the stroke's own alpha).
HALO_ALPHA = 0.55
#: Opacity of the calibration target's crisp outline.
OUTLINE_ALPHA = 0.85

_TAU = 2.0 * math.pi


class _Grid(NamedTuple):
    """Pixel-centre coordinates relative to the reticle's centre (y down), shared by every render of a size."""

    x: np.ndarray
    y: np.ndarray
    #: ``|x|`` and ``|y|``: shapes symmetric about both axes are drawn once in the folded quadrant.
    ax: np.ndarray
    ay: np.ndarray
    r: np.ndarray
    #: Clockwise from 12 o'clock, in [0, 2π).
    theta: np.ndarray


#: (signed distance in px, BGR colour, alpha); later strokes are painted over earlier ones.
_Stroke = tuple[np.ndarray, tuple[float, float, float], float]


@lru_cache(maxsize=8)
def _grid(size: int) -> _Grid:
    c = np.arange(size, dtype=np.float32) - np.float32(size // 2)
    x, y = np.meshgrid(c, c)
    r = np.sqrt(x * x + y * y)
    theta = np.mod(np.arctan2(x, -y), np.float32(_TAU)).astype(np.float32)
    grid = _Grid(x, y, np.abs(x), np.abs(y), r, theta)
    for a in grid:
        a.flags.writeable = False
    return grid


# --------------------------------------------------------------------------- shapes (signed distances, px)


def _ring(g: _Grid, radius: float, width: float) -> np.ndarray:
    return np.abs(g.r - radius) - width / 2


def _disc(g: _Grid, radius: float) -> np.ndarray:
    return g.r - radius


def _arc(g: _Grid, radius: float, width: float, start: float, sweep: float) -> np.ndarray:
    """A ring from ``start`` clockwise through ``sweep`` radians (0 is 12 o'clock), with round caps."""
    if sweep >= _TAU - 1e-6:
        return _ring(g, radius, width)
    start %= _TAU
    rel = g.theta - np.float32(start)
    rel += np.float32(_TAU) * (rel < 0)
    end = start + sweep
    to_caps = np.minimum(
        _length(g.x - radius * math.sin(start), g.y + radius * math.cos(start)),
        _length(g.x - radius * math.sin(end), g.y + radius * math.cos(end)),
    )
    return np.where(rel <= sweep, np.abs(g.r - radius), to_caps) - width / 2


def _segment(x: np.ndarray, y: np.ndarray, a: tuple[float, float], b: tuple[float, float], width: float) -> np.ndarray:
    """A straight stroke from ``a`` to ``b`` with round caps. ``x``, ``y`` may be folded (abs) for symmetry."""
    ax, ay = a
    bx, by = b
    px, py = x - ax, y - ay
    dx, dy = bx - ax, by - ay
    t = np.clip((px * dx + py * dy) / (dx * dx + dy * dy), 0.0, 1.0)
    return _length(px - dx * t, py - dy * t) - width / 2


def _length(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    # Not np.hypot: several times slower on float32 and the overflow care it takes is not needed here.
    return np.sqrt(x * x + y * y)


def _union(*distances: np.ndarray) -> np.ndarray:
    out = distances[0]
    for d in distances[1:]:
        out = np.minimum(out, d)
    return out


def _brackets(g: _Grid, half: float, arm: float, width: float) -> np.ndarray:
    """Four corner brackets of a square ``2 * half`` wide, each arm ``arm`` long."""
    corner = (half, half)
    return _union(
        _segment(g.ax, g.ay, (half - arm, half), corner, width),
        _segment(g.ax, g.ay, (half, half - arm), corner, width),
    )


def _cardinal_ticks(g: _Grid, inner: float, outer: float, width: float) -> np.ndarray:
    """Short radial strokes at 12, 3, 6 and 9 o'clock between ``inner`` and ``outer``."""
    return _union(
        _segment(g.x, g.ay, (0.0, inner), (0.0, outer), width),
        _segment(g.ax, g.y, (inner, 0.0), (outer, 0.0), width),
    )


def _diagonal_ticks(g: _Grid, inner: float, outer: float, width: float) -> np.ndarray:
    """Short strokes along the four diagonals, ``inner`` .. ``outer`` along each axis."""
    return _segment(g.ax, g.ay, (inner, inner), (outer, outer), width)


# --------------------------------------------------------------------------- compositing


def _fill(d: np.ndarray) -> np.ndarray:
    """Coverage of the shape whose signed distance is ``d`` (1 px anti-aliased edge)."""
    return np.clip(0.5 - d, 0.0, 1.0)


def _glow(d: np.ndarray, spread: float) -> np.ndarray:
    """1 inside the shape, easing to 0 at ``spread`` px outside it."""
    t = np.clip(1.0 - d / spread, 0.0, 1.0)
    return t * t


def _strongest(covers: list[np.ndarray]) -> np.ndarray:
    out = covers[0]
    for c in covers[1:]:
        out = np.maximum(out, c)
    return out


def _paint(planes: np.ndarray, cover: np.ndarray, colour: tuple[float, float, float], opacity: float) -> None:
    """``colour`` at ``opacity`` times ``cover``, over what ``planes`` (premultiplied B, G, R, A) holds."""
    src = cover * np.float32(opacity)
    planes *= 1.0 - src
    for plane, value in zip(planes[:3], colour, strict=True):
        if value:
            plane += src * np.float32(value)
    planes[3] += src


def _compose(size: int, strokes: list[_Stroke], *, spread: float, outline: float = 0.0) -> np.ndarray:
    """Halo (and outline) under every stroke first, then the strokes in order, all premultiplied."""
    # Planes (B, G, R, A) rather than interleaved pixels: whole-plane numpy ops are several times faster.
    planes = np.zeros((4, size, size), np.float32)
    if strokes:
        # One shadow layer from all strokes (the strongest wins), so no stroke's halo dims another stroke.
        _paint(planes, _strongest([_glow(d, spread) * a for d, _, a in strokes]), SHADOW, HALO_ALPHA)
        if outline > 0:
            _paint(planes, _strongest([_fill(d - outline) * a for d, _, a in strokes]), SHADOW, OUTLINE_ALPHA)
        for d, colour, a in strokes:
            _paint(planes, _fill(d), colour, a)

    np.clip(planes, 0.0, 1.0, out=planes)
    # Over-compositing never lifts a colour above its alpha; the clamp only absorbs float rounding,
    # since the layered window requires premultiplied pixels.
    np.minimum(planes[:3], planes[3], out=planes[:3])
    planes *= 255.0
    np.rint(planes, out=planes)
    out = np.empty((size, size, 4), np.uint8)
    for i in range(4):
        out[..., i] = planes[i]
    return out


# --------------------------------------------------------------------------- modes

_Painter = Callable[[_Grid, int, float, float], list[_Stroke]]


def _main_ring(g: _Grid, u: float) -> _Stroke:
    return (_ring(g, 21 * u, 2 * u), CYAN, 0.95)


def _idle(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    return [(_ring(g, 21 * u, 1.5 * u), CYAN, 0.35)]


def _engaging(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    radius = 21 * u
    strokes: list[_Stroke] = [
        (_ring(g, radius, 1.5 * u), CYAN, 0.35),
        # Where the fill starts.
        (_segment(g.x, g.y, (0.0, -(radius + 3 * u)), (0.0, -(radius + 7 * u)), 2 * u), CYAN, 0.8),
    ]
    if progress > 0:
        strokes.append((_arc(g, radius, 3 * u, 0.0, progress * _TAU), CYAN, 1.0))
    return strokes


def _point(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    strokes = [_main_ring(g, u)]
    if pinch > 0:
        # Grows from 12 o'clock both ways and closes at 6 as the fingers close.
        strokes.append((_arc(g, 13 * u, 2.5 * u, -pinch * math.pi, pinch * _TAU), CYAN, 1.0))
    strokes.append((_disc(g, 2 * u), WHITE, 1.0))
    return strokes


def _press(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    return [_main_ring(g, u), (_disc(g, 10 * u), CYAN, 0.9), (_disc(g, 3 * u), WHITE, 1.0)]


def _drag(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    ticks = (_cardinal_ticks(g, 25 * u, 31 * u, 2.5 * u), CYAN, 0.95)
    return [*_press(g, size, pinch, progress), ticks]


def _scroll(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    # Folded on both axes: one stroke draws both arms of the up chevron (above) and the down one (below).
    chevrons = _segment(np.abs(g.x), np.abs(g.y), (0.0, 34 * u), (6 * u, 28 * u), 2.5 * u)
    return [_main_ring(g, u), (chevrons, CYAN, 1.0), (_disc(g, 2 * u), WHITE, 1.0)]


def _grab(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    return [(_brackets(g, 17 * u, 7 * u, 3 * u), AMBER, 1.0), (_disc(g, 3 * u), AMBER, 1.0)]


def _resize(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    u = size / BASE_SIZE
    return [
        (_brackets(g, 23 * u, 8 * u, 3 * u), AMBER, 1.0),
        (_diagonal_ticks(g, 8 * u, 13 * u, 2 * u), AMBER, 0.9),
        (_disc(g, 3 * u), AMBER, 1.0),
    ]


def _calibrate(g: _Grid, size: int, pinch: float, progress: float) -> list[_Stroke]:
    # Proportional to the whole image (which the backend makes CALIBRATE_SCALE larger), not to the ring.
    s = float(size)
    line = max(2.0, 0.02 * s)
    track_r, track_w = 0.37 * s, max(3.0, 0.04 * s)
    crosshair = _union(
        _segment(np.abs(g.x), g.y, (0.07 * s, 0.0), (0.31 * s, 0.0), line),
        _segment(g.x, np.abs(g.y), (0.0, 0.07 * s), (0.0, 0.31 * s), line),
    )
    strokes: list[_Stroke] = [(_ring(g, track_r, track_w), WHITE, 0.3)]
    if progress > 0:
        strokes.append((_arc(g, track_r, track_w, 0.0, progress * _TAU), CYAN, 1.0))
    strokes += [
        (_ring(g, 0.20 * s, line), WHITE, 1.0),
        (crosshair, WHITE, 1.0),
        (_disc(g, max(1.5, 0.025 * s)), WHITE, 1.0),
    ]
    return strokes


_PAINTERS: dict[str, _Painter] = {
    "idle": _idle,
    "engaging": _engaging,
    "point": _point,
    "press": _press,
    "drag": _drag,
    "scroll": _scroll,
    "grab": _grab,
    "resize": _resize,
    "calibrate": _calibrate,
}
_USES_PINCH = frozenset({"point"})
_USES_PROGRESS = frozenset({"engaging", "calibrate"})


def draw(mode: OverlayMode, size: int, pinch: float = 0.0, progress: float = 0.0) -> np.ndarray:
    """Uncached render of one mode (``render`` is the cached, quantized entry point)."""
    size = _check_size(size)
    if mode == "hidden":
        return np.zeros((size, size, 4), np.uint8)
    painter = _PAINTERS.get(mode)
    if painter is None:
        raise ValueError(f"unknown overlay mode {mode!r}")
    g = _grid(size)
    u = size / BASE_SIZE
    if mode == "calibrate":
        return _compose(size, painter(g, size, pinch, progress), spread=max(2.0, 0.025 * size), outline=0.011 * size)
    return _compose(size, painter(g, size, pinch, progress), spread=max(2.0, 3.0 * u))


def _check_size(size: int) -> int:
    size = operator.index(size)
    if size < MIN_SIZE:
        raise ValueError(f"overlay image size must be at least {MIN_SIZE} px, got {size}")
    return size


def _quantize(value: float) -> int:
    if math.isnan(value):
        return 0
    return round(min(max(value, 0.0), 1.0) * QUANTUM)


@lru_cache(maxsize=CACHE_SIZE)
def _cached(mode: OverlayMode, size: int, pinch_q: int, progress_q: int) -> np.ndarray:
    image = draw(mode, size, pinch_q / QUANTUM, progress_q / QUANTUM)
    image.flags.writeable = False
    return image


def render(state: OverlayState, size: int = BASE_SIZE) -> np.ndarray:
    """The reticle for ``state``: ``(size, size, 4)`` uint8, premultiplied BGRA, read-only and cached."""
    mode = state.mode
    if mode != "hidden" and mode not in _PAINTERS:
        raise ValueError(f"unknown overlay mode {mode!r}")
    pinch_q = _quantize(state.pinch) if mode in _USES_PINCH else 0
    progress_q = _quantize(state.progress) if mode in _USES_PROGRESS else 0
    return _cached(mode, _check_size(size), pinch_q, progress_q)


@lru_cache(maxsize=8)
def _helper_cached(size: int) -> np.ndarray:
    g = _grid(size)
    u = size / HELPER_BASE_SIZE
    reach, width = 0.30 * size, max(2.0, 0.06 * size)
    # Folded on both axes, one segment is all four sides of the diamond.
    diamond = _segment(np.abs(g.x), np.abs(g.y), (0.0, reach), (reach, 0.0), width)
    strokes: list[_Stroke] = [(diamond, AMBER, 1.0), (_disc(g, max(1.5, 0.05 * size)), AMBER, 1.0)]
    image = _compose(size, strokes, spread=max(2.0, 3.0 * u))
    image.flags.writeable = False
    return image


def render_helper(size: int = HELPER_BASE_SIZE) -> np.ndarray:
    """The second hand's marker during a two-hand resize: a small amber diamond (same format as ``render``)."""
    return _helper_cached(_check_size(size))


def clear_cache() -> None:
    _cached.cache_clear()
    _helper_cached.cache_clear()


# --------------------------------------------------------------------------- placement


def _scaled(base: int, dpi: int) -> int:
    if not isinstance(dpi, int) or dpi <= 0:
        dpi = 96
    return max(MIN_SIZE, round(base * dpi / 96))


def reticle_size(mode: OverlayMode, dpi: int) -> int:
    """The reticle's image size on a monitor of ``dpi`` (96 at 100 % scaling): 96 px there, 144 px at 150 %."""
    base = round(BASE_SIZE * CALIBRATE_SCALE) if mode == "calibrate" else BASE_SIZE
    return _scaled(base, dpi)


def helper_size(dpi: int) -> int:
    return _scaled(HELPER_BASE_SIZE, dpi)


def top_left(point: Point, size: int) -> tuple[int, int]:
    """Where a ``size`` image's top-left corner goes so that its centre pixel covers ``point``."""
    x, y = point.rounded()
    return x - size // 2, y - size // 2
