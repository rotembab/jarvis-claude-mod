"""Pure drawing of the keyboard layer: geometry, the baked key sprites and the per-frame composite.

DESIGN-KEYBOARD.md 1.3 and 3.11. Numpy plus Pillow glyph masks, no window code, so everything here is testable
without a screen. Pixels are premultiplied BGRA, the byte order ``UpdateLayeredWindow`` takes.

Three layers of work, each cached where it changes. ``bake_base`` draws what depends only on the language, Shift, the
key size and the layout: the panels, the key faces and their legends. ``compose`` copies that and adds what changes from
frame to frame: lit keys (a whole replacement sprite per key, never a repaint of pixels), the status strip, the review
box or the echo row, the progress bar, the dimming of a hold and the fingertip rings. Key faces and rings are
signed-distance shapes like the reticle's (``render.py``, whose helpers they reuse): the same pixels on every OS, so the
tests check what Windows shows. Text is Pillow's basic layout, left to right, from a system font: Hebrew goes through
``text.py``'s visual order first, because the basic layout does not reorder anything.

Typed text never leaves this module except as pixels. Nothing here logs, prints or formats an exception: any failure
leaves as ``KeyboardDrawError(code)`` with one of four fixed codes (F4, S22b), raised outside the ``except`` so that no
exception that carried the text is chained to it. Sprites made of typed text (the box, the echo row, the chips) are kept
in one-entry memos, not in the ``lru_cache`` that holds the fixed vocabulary (legends, strip sentences, rings), and
``clear_cache`` drops all of it when the keyboard closes. There is no clock either: a flash or a pulse is whatever the
view says, so the same view is always the same picture.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypeVar

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..keyboard.layout import Layout, layout_for
from ..keyboard.layout import legend as key_legend
from ..keyboard.limits import COMPOSE_MAX
from ..keyboard.types import Commit, Dock, Lang
from . import render as reticle
from .base import ComposeView, KeyboardView, TipView
from .text import base_direction, bidi_directions, bidi_order, wrap_spans

#: The only things a draw error says (DESIGN 3.11 item 6): no font with Latin and Hebrew glyphs, no drawing surface,
#: a size that cannot be drawn, anything else.
DrawErrorCode = Literal["font", "surface", "size", "internal"]
DRAW_ERROR_CODES: tuple[DrawErrorCode, ...] = ("font", "surface", "size", "internal")


class KeyboardDrawError(RuntimeError):
    """The layer could not be drawn. Exception text is data too (F4, S22b): ``code`` is the whole message.

    Anything but one of ``DRAW_ERROR_CODES`` (a formatted exception, a piece of the box) is replaced by ``internal``
    instead of being carried.
    """

    code: DrawErrorCode

    def __init__(self, code: DrawErrorCode = "internal") -> None:
        safe: DrawErrorCode = code if type(code) is str and code in DRAW_ERROR_CODES else "internal"
        super().__init__(safe)
        self.code = safe

    def __str__(self) -> str:
        return self.code


_T = TypeVar("_T")


def _guarded(fn: Callable[..., _T], *args: Any) -> _T:
    """Runs ``fn``; whatever escapes it but a ``KeyboardDrawError`` leaves as one with a fixed code.

    The new error is raised after the ``except`` block, so it has no ``__context__``: the exception that was caught may
    carry the text being drawn (Pillow quotes it in some messages), and a traceback printed from the new one must not
    reach it (F4).
    """
    code: DrawErrorCode = "internal"
    try:
        return fn(*args)
    except KeyboardDrawError:
        raise
    except MemoryError:
        code = "surface"
    except Exception:  # noqa: BLE001 - nothing is allowed to carry what was being drawn
        pass
    raise KeyboardDrawError(code)


# --------------------------------------------------------------------------- the look (DESIGN 1.3)

#: Key pitch in pixels at 96 DPI and size 1.0; every other length is a multiple of ``pitch / BASE_PITCH``.
BASE_PITCH = 56
#: The smallest pitch that can still be read; a work area that cannot hold even that is a "size" error.
MIN_PITCH = 16
#: Window padding, status strip, echo row, review box and the gap under it, in pixels at BASE_PITCH.
PAD = 8
STRIP_H = 40  # not a key count: pixels
ECHO_H = 32
BOX_H = 78
BOX_GAP = 6
#: Distance of the window from the work area's edge, in pixels (the design says 12 whatever the scale).
DOCK_MARGIN = 12
#: One line of the strip (the banner and the status line are two of them) and of the review box.
STRIP_LINE_H = 18
BOX_LINE_H = 22
BOX_LINES = 3
#: Font sizes at BASE_PITCH: a one-character legend, a word legend, the strip and banner, the box, two echo lines.
LEGEND_CHAR_PX = 22
LEGEND_WORD_PX = 15
STRIP_PX = 15
BOX_PX = 18
ROW_PX = 13
#: The counter turns amber from here (DESIGN 1.3: ``n/200`` right-aligned in the strip, amber from 180).
COUNT_AMBER_FROM = 180
#: The keyboard below the strip at this brightness while a hold is set; the typed prefix of a run likewise.
HOLD_DIM = 0.45
SENT_DIM = 0.45
#: Fingertip ring radius (DESIGN 1.3: 7 px), the placement homes', and how many rings at most are drawn.
RING_RADIUS = 7
HOME_RADIUS = 10
MAX_RINGS = 8
#: The arming ring and the guard ring are drawn in this many steps; finer ones are invisible and fill the cache.
RING_FILL_STEPS = 16
GUARD_RING_STEPS = 16
#: A chip drawn masked is at most this many bullets (O81); the layout has this many chip cells.
CHIP_MAX_BULLETS = 14
CHIP_CELLS = 3
BULLET = "•"

Rect = tuple[int, int, int, int]


def _rgb(value: int) -> tuple[int, int, int]:
    """0xRRGGBB to (B, G, R) bytes, the order the layered window takes."""
    return value & 0xFF, (value >> 8) & 0xFF, value >> 16


def _bgr(value: int) -> tuple[float, float, float]:
    b, g, r = _rgb(value)
    return b / 255, g / 255, r / 255


def _mix(a: int, b: int, t: float) -> int:
    """``a`` moved ``t`` of the way to ``b`` (both 0xRRGGBB)."""
    return sum(round(((a >> s) & 0xFF) * (1 - t) + ((b >> s) & 0xFF) * t) << s for s in (0, 8, 16))


FACE = 0x1E2430
BORDER = 0x3A4658
LEGEND = 0xEAF0F7
DARK_LEGEND = 0x14181F
AMBER = 0xF5B544
GREEN = 0x4CC38A
RED = 0xE5534B
CYAN = 0x00E5FF
WHITE = 0xFFFFFF
MUTED = 0x9AA7B8
GREY = 0xA0A8B4
PANEL = 0x121720
SHADOW = 0x0D0A1A
FACE_ALPHA = 0.82
PANEL_ALPHA = 0.88


@dataclass(frozen=True)
class _Look:
    """How one cell is painted: fill, border (width in pixels at BASE_PITCH) and legend."""

    fill: int
    fill_a: float
    border: int
    border_a: float
    border_w: float
    legend: int
    legend_a: float


#: Looks by name: the six ``LitKind``, the key as baked and the cells that are never lit.
_LOOKS: dict[str, _Look] = {
    "normal": _Look(FACE, FACE_ALPHA, BORDER, 1.0, 1.0, LEGEND, 1.0),
    # a fingertip is over the key: 25% white
    "ghost": _Look(_mix(FACE, WHITE, 0.25), FACE_ALPHA, BORDER, 1.0, 1.0, LEGEND, 1.0),
    # a tap is in progress and its aim is frozen: amber
    "target": _Look(AMBER, 0.92, AMBER, 1.0, 1.0, DARK_LEGEND, 1.0),
    "ok": _Look(GREEN, 0.92, GREEN, 1.0, 1.0, DARK_LEGEND, 1.0),
    "drop": _Look(RED, 0.92, RED, 1.0, 1.0, WHITE, 1.0),
    # an armed guard: an amber outline
    "armed": _Look(FACE, FACE_ALPHA, AMBER, 1.0, 2.0, LEGEND, 1.0),
    # an available or running action, a toggle that is on: the reticle's cyan
    "on": _Look(_mix(FACE, CYAN, 0.22), FACE_ALPHA, CYAN, 1.0, 2.0, LEGEND, 1.0),
    # Send while nothing can be sent
    "dim": _Look(FACE, FACE_ALPHA, BORDER, 1.0, 1.0, LEGEND, 0.35),
    # a dead cell, and an inert chip cell
    "dead": _Look(FACE, 0.30, BORDER, 0.0, 0.0, LEGEND, 0.0),
    "chip": _Look(FACE, 0.42, BORDER, 0.45, 1.0, LEGEND, 0.0),
    "panel": _Look(PANEL, PANEL_ALPHA, BORDER, 1.0, 1.0, LEGEND, 1.0),
    "strip": _Look(FACE, FACE_ALPHA, BORDER, 1.0, 1.0, LEGEND, 1.0),
}
#: Which look wins when a key has several (the session's own order): the strongest.
_LIT_RANK = {"ghost": 0, "on": 1, "armed": 2, "target": 3, "ok": 4, "drop": 5}
#: What the session lights and a private view drops: it is where a finger is, or what it did.
_TOUCH_KINDS = frozenset({"ghost", "target", "ok", "drop"})
#: The key a guard is on, by guard kind (the Send guard belongs to the ``enter`` key, which reads ``Send`` in review).
_GUARD_KEY = {"insert": "insert", "clear": "clear", "send": "enter", "close": "close"}


# --------------------------------------------------------------------------- geometry


@dataclass(frozen=True)
class KeyboardGeometry:
    #: Pixels per key unit.
    pitch: int
    #: Window size.
    width: int
    height: int
    #: Per key index (the review layout's cells too): x, y, w, h in window pixels.
    keys: tuple[Rect, ...]
    strip: Rect
    #: The row under the strip where the direct layout shows the typed tail; zero-high in the review layout, whose box
    #: takes its place.
    echo: Rect
    #: The box rect in review mode, else None.
    compose: Rect | None


class _Metrics(NamedTuple):
    """Every length of one layout at one pitch, in window pixels."""

    pad: int
    strip_h: int
    echo_h: int
    box_h: int
    gap: int
    #: Top of the first key row.
    y_keys: int
    width: int
    height: int
    #: Pixels per BASE_PITCH pixel.
    unit: float


def _round(value: float) -> int:
    return math.floor(value + 0.5)


def _scaled(unit: float, base: float) -> int:
    """``base`` pixels at BASE_PITCH, at this unit; never below one pixel."""
    return max(1, _round(base * unit))


@lru_cache(maxsize=64)
def _metrics(pitch: int, commit: Commit) -> _Metrics:
    unit = pitch / BASE_PITCH
    layout = layout_for(commit)
    pad, strip_h = _scaled(unit, PAD), _scaled(unit, STRIP_H)
    review = commit == "review"
    echo_h = 0 if review else _scaled(unit, ECHO_H)
    box_h = _scaled(unit, BOX_H) if review else 0
    gap = _scaled(unit, BOX_GAP) if review else 0
    y_keys = pad + strip_h + echo_h + box_h + gap
    width = 2 * pad + _round(11.5 * pitch)
    return _Metrics(pad, strip_h, echo_h, box_h, gap, y_keys, width, y_keys + layout.rows * pitch + pad, unit)


@lru_cache(maxsize=32)
def _geometry_for(pitch: int, commit: Commit) -> KeyboardGeometry:
    m = _metrics(pitch, commit)
    keys: list[Rect] = []
    for key in layout_for(commit).keys:
        x0 = m.pad + _round(key.col * pitch)
        x1 = m.pad + _round((key.col + key.width) * pitch)
        keys.append((x0, m.y_keys + key.row * pitch, x1 - x0, pitch))
    inner = m.width - 2 * m.pad
    strip = (m.pad, m.pad, inner, m.strip_h)
    echo = (m.pad, m.pad + m.strip_h, inner, m.echo_h)
    compose = (m.pad, m.pad + m.strip_h, inner, m.box_h) if commit == "review" else None
    return KeyboardGeometry(pitch, m.width, m.height, tuple(keys), strip, echo, compose)


def keyboard_geometry(
    work: tuple[int, int, int, int], size: float, dpi: int, dock: Dock, commit: Commit = "direct"
) -> tuple[KeyboardGeometry, tuple[int, int]]:
    """The geometry and the window's top-left corner."""
    return _guarded(_keyboard_geometry, work, size, dpi, dock, commit)


def _keyboard_geometry(
    work: tuple[int, int, int, int], size: float, dpi: int, dock: Dock, commit: Commit
) -> tuple[KeyboardGeometry, tuple[int, int]]:
    bad = commit not in ("direct", "review") or dock not in ("top", "bottom")
    x = y = w = h = 0
    scale = 0.0
    if not bad:
        try:
            x, y, w, h = (int(v) for v in work)
            scale = float(size) * float(dpi) / 96.0
        except (TypeError, ValueError, OverflowError):
            bad = True
    if bad or w <= 0 or h <= 0 or not math.isfinite(scale) or scale <= 0:
        raise KeyboardDrawError("size")
    # The formula first; a keyboard bigger than the display's work area is no use (the right-hand keys would be off the
    # screen), so it shrinks until it fits, and "size" is what is left when even the smallest does not. The first
    # bound only keeps the loop short for an absurd size.
    pitch = min(_round(BASE_PITCH * scale), max(w // 11, 1), max(h // 4, 1))
    while pitch >= MIN_PITCH:
        m = _metrics(pitch, commit)
        if m.width <= w and m.height + 2 * DOCK_MARGIN <= h:
            break
        pitch -= 1
    if pitch < MIN_PITCH:
        raise KeyboardDrawError("size")
    geometry = _geometry_for(pitch, commit)
    left = x + (w - geometry.width) // 2
    top = y + DOCK_MARGIN if dock == "top" else y + h - geometry.height - DOCK_MARGIN
    return geometry, (left, top)


def _gap_rects(pitch: int, commit: Commit) -> list[Rect]:
    """The dead cells, which have no key and so no rect in the geometry."""
    m = _metrics(pitch, commit)
    rects = []
    for row, col_from, col_to in layout_for(commit).gaps:
        x0, x1 = m.pad + _round(col_from * pitch), m.pad + _round(col_to * pitch)
        rects.append((x0, m.y_keys + row * pitch, x1 - x0, pitch))
    return rects


# --------------------------------------------------------------------------- fonts


def _font_candidates() -> list[str]:
    """Where to look for a font with Latin and Hebrew glyphs, in order (DESIGN 3.11).

    ``%WINDIR%\\Fonts``: Segoe UI, Arial, Tahoma (all have Hebrew); then DejaVu Sans and the other common Latin and
    Hebrew system fonts by name, which Pillow looks up in the system font folders; last, the DejaVu Sans that
    matplotlib (a dependency of MediaPipe) ships, found without importing it. No font is bundled with Jarvis.
    """
    found: list[str] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT")
    if windir:
        found += [os.path.join(windir, "Fonts", name) for name in ("segoeui.ttf", "arial.ttf", "tahoma.ttf")]
    found += ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"]
    try:
        spec = importlib_util.find_spec("matplotlib")
        if spec is not None and spec.origin:
            found.append(str(Path(spec.origin).parent / "mpl-data" / "fonts" / "ttf" / "DejaVuSans.ttf"))
    except Exception:  # noqa: BLE001 - an unusable fallback is no fallback
        pass
    return found


def _open_font(path: str, px: int) -> ImageFont.FreeTypeFont:
    # BASIC: Raqm, where Pillow has it, would reorder Hebrew a second time after ``bidi_order`` did.
    return ImageFont.truetype(path, px, layout_engine=ImageFont.Layout.BASIC)


def _ink(font: ImageFont.FreeTypeFont, ch: str) -> np.ndarray:
    image = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(image).text((8, 48), ch, fill=255, font=font, anchor="ls")
    return np.array(image)


def _covers(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    """The font has a glyph for ``ch``: what it draws differs from what it draws for U+FFFF, which is never assigned."""
    return not np.array_equal(_ink(font, ch), _ink(font, "￿"))


@lru_cache(maxsize=1)
def _font_path() -> str | None:
    """The first candidate that opens and has Latin and Hebrew glyphs, or None. Looked up once per process."""
    try:
        candidates = _font_candidates()
    except Exception:  # noqa: BLE001 - no candidates is no font
        return None
    for candidate in candidates:
        try:
            font = _open_font(candidate, BOX_PX)
            if _covers(font, "a") and _covers(font, "א"):
                return str(font.path)
        except Exception:  # noqa: BLE001 - this one is not usable; the next may be
            continue
    return None


def font_available() -> bool:
    """A font with Latin and Hebrew glyphs loads; False means the keyboard must not be offered (never type blind)."""
    try:
        return _font_path() is not None
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=32)
def _font_at(path: str, px: int) -> ImageFont.FreeTypeFont:
    return _open_font(path, px)


def _font(px: int) -> ImageFont.FreeTypeFont:
    path = _font_path()
    if path is not None:
        try:
            return _font_at(path, px)
        except Exception:  # noqa: BLE001
            pass
    raise KeyboardDrawError("font")


def _cap_height(font: ImageFont.FreeTypeFont) -> float:
    return float(-font.getbbox("H", anchor="ls")[1])


def _clean(text: str) -> str:
    """``text`` with the characters Pillow cannot encode or draw replaced by U+FFFD, one for one (indexes survive)."""
    if text.isprintable():
        return text
    return "".join(c if c.isprintable() else "�" for c in text)


# --------------------------------------------------------------------------- pixels


def _mask(
    width: int, height: int, font: ImageFont.FreeTypeFont, items: list[tuple[str, float, float, int]]
) -> np.ndarray:
    """An (height, width) coverage mask; each ``(text, x, baseline, fill)`` is drawn left-aligned at its baseline."""
    image = Image.new("L", (max(width, 1), max(height, 1)), 0)
    draw = ImageDraw.Draw(image)
    for piece, x, baseline, fill in items:
        if piece:
            draw.text((x, baseline), piece, fill=fill, font=font, anchor="ls")
    return np.array(image, dtype=np.uint8)


def _tint(mask: np.ndarray, colour: int, alpha: float = 1.0) -> np.ndarray:
    """A coverage mask in one colour as premultiplied BGRA."""
    a = np.rint(mask.astype(np.float32) * np.float32(alpha)).astype(np.uint16)
    out = np.empty((*mask.shape, 4), np.uint8)
    for i, value in enumerate(_rgb(colour)):
        out[..., i] = (a * value + 127) // 255
    out[..., 3] = a
    return out


def _over(dst: np.ndarray, src: np.ndarray) -> None:
    """``src`` over ``dst`` in place; both premultiplied BGRA of one shape."""
    alpha = src[..., 3:4]
    if not alpha.any():
        return
    keep = np.uint16(255) - alpha.astype(np.uint16)
    total = src.astype(np.uint16) + (dst.astype(np.uint16) * keep + 127) // 255
    dst[...] = np.minimum(total, 255).astype(np.uint8)


def _blit(dst: np.ndarray, src: np.ndarray, x: int, y: int, *, replace_pixels: bool = False) -> None:
    """``src`` over (or, with ``replace_pixels``, instead of) the part of ``dst`` it covers from its top-left (x, y)."""
    h, w = src.shape[:2]
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, dst.shape[1]), min(y + h, dst.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    part = src[y0 - y : y1 - y, x0 - x : x1 - x]
    if replace_pixels:
        dst[y0:y1, x0:x1] = part
    else:
        _over(dst[y0:y1, x0:x1], part)


def _flatten(height: int, width: int, strokes: list[reticle._Stroke]) -> np.ndarray:
    """Strokes ``(signed distance, BGR in 0..1, opacity)`` painted in order, as premultiplied BGRA."""
    planes = np.zeros((4, height, width), np.float32)
    for distance, colour, opacity in strokes:
        reticle._paint(planes, reticle._fill(distance), colour, opacity)
    np.clip(planes, 0.0, 1.0, out=planes)
    np.minimum(planes[:3], planes[3], out=planes[:3])
    planes *= 255.0
    np.rint(planes, out=planes)
    out = np.empty((height, width, 4), np.uint8)
    for i in range(4):
        out[..., i] = planes[i]
    return out


def _rrect_distance(width: int, height: int, radius: float) -> np.ndarray:
    """Signed distance (px, negative inside) to the rounded rectangle that fills ``width`` x ``height``."""
    radius = min(radius, width / 2, height / 2)
    xs = (np.arange(width, dtype=np.float32) + np.float32(0.5) - np.float32(width / 2))[None, :]
    ys = (np.arange(height, dtype=np.float32) + np.float32(0.5) - np.float32(height / 2))[:, None]
    qx = np.abs(xs) - np.float32(width / 2 - radius)
    qy = np.abs(ys) - np.float32(height / 2 - radius)
    ox, oy = np.maximum(qx, 0), np.maximum(qy, 0)
    return np.sqrt(ox * ox + oy * oy) + np.minimum(np.maximum(qx, qy), 0) - np.float32(radius)


@lru_cache(maxsize=256)
def _face(width: int, height: int, inset: int, radius: float, look_name: str, border_w: float) -> np.ndarray:
    """A cell: a rounded face ``inset`` pixels inside it, filled and bordered as ``look_name`` says. Read-only."""
    look = _LOOKS[look_name]
    inner_w, inner_h = max(width - 2 * inset, 1), max(height - 2 * inset, 1)
    d = np.full((height, width), np.float32(1e3), np.float32)
    d[inset : inset + inner_h, inset : inset + inner_w] = _rrect_distance(inner_w, inner_h, radius)
    cover = np.clip(np.float32(0.5) - d, 0.0, 1.0)
    edge = np.clip(np.float32(0.5) + d + np.float32(border_w), 0.0, 1.0) if border_w > 0 else np.zeros_like(d)
    alpha = cover * (np.float32(look.fill_a) * (1 - edge) + np.float32(look.border_a) * edge)
    out = np.empty((height, width, 4), np.uint8)
    for i, (fill_c, border_c) in enumerate(zip(_rgb(look.fill), _rgb(look.border), strict=True)):
        colour = np.float32(fill_c) * (1 - edge) + np.float32(border_c) * edge
        out[..., i] = np.rint(colour * alpha)
    out[..., 3] = np.rint(alpha * 255)
    np.minimum(out[..., :3], out[..., 3:4], out=out[..., :3])
    out.flags.writeable = False
    return out


def _border_px(look: _Look, unit: float) -> float:
    return max(1.0, round(look.border_w * unit * 2) / 2) if look.border_w else 0.0


@lru_cache(maxsize=64)
def _outline(width: int, height: int, radius: float, colour: int, stroke: float) -> np.ndarray:
    """A rounded outline of ``stroke`` pixels along the edge of a ``width`` x ``height`` cell (drawn over a border)."""
    d = _rrect_distance(width, height, radius)
    out = _flatten(height, width, [(np.abs(d + stroke / 2) - stroke / 2, _bgr(colour), 1.0)])
    out.flags.writeable = False
    return out


# --------------------------------------------------------------------------- key sprites


@lru_cache(maxsize=512)
def _key_sprite(
    width: int, height: int, pitch: int, look_name: str, legend: str, guard: tuple[int, int, int] | None
) -> np.ndarray:
    """One cell as a whole: face, border, legend and, for a counted guard, its pips and ring. Read-only."""
    unit = pitch / BASE_PITCH
    look = _LOOKS[look_name]
    sprite = _face(width, height, _scaled(unit, 2), max(2.0, 6.0 * unit), look_name, _border_px(look, unit)).copy()
    if legend.strip() and look.legend_a > 0:
        font = _font(_scaled(unit, LEGEND_CHAR_PX if len(legend) == 1 else LEGEND_WORD_PX))
        baseline = height / 2 + _cap_height(font) / 2
        x = (width - font.getlength(legend)) / 2
        _over(sprite, _tint(_mask(width, height, font, [(legend, x, baseline, 255)]), look.legend, look.legend_a))
    if guard is not None:
        _over(sprite, _guard_overlay(width, height, pitch, *guard))
    sprite.flags.writeable = False
    return sprite


@lru_cache(maxsize=128)
def _guard_overlay(width: int, height: int, pitch: int, taps: int, need: int, left_q: int) -> np.ndarray:
    """``taps`` of ``need`` pips on an armed key and a ring that shrinks as the guard window runs out."""
    unit = pitch / BASE_PITCH
    xs = np.arange(width, dtype=np.float32)[None, :] + np.float32(0.5)
    ys = np.arange(height, dtype=np.float32)[:, None] + np.float32(0.5)
    strokes: list[reticle._Stroke] = []
    if left_q > 0:
        r = np.sqrt((xs - width / 2) ** 2 + (ys - height / 2) ** 2)
        reach = 0.36 * min(width, height) * left_q / GUARD_RING_STEPS
        strokes.append((np.abs(r - reach) - 1.0 * unit, _bgr(AMBER), 0.9))
    y = height - _scaled(unit, 11)
    for i in range(need):
        x = width / 2 + (i - (need - 1) / 2) * 8.5 * unit
        r = np.sqrt((xs - x) ** 2 + (ys - y) ** 2)
        if i < taps:
            strokes.append((r - 2.8 * unit, _bgr(AMBER), 1.0))
        else:
            strokes.append((np.abs(r - 2.8 * unit) - 0.7 * unit, _bgr(AMBER), 0.85))
    if not strokes:
        return np.zeros((height, width, 4), np.uint8)
    out = _flatten(height, width, strokes)
    out.flags.writeable = False
    return out


class _KeyState(NamedTuple):
    look: str
    #: The legend to draw instead of the layout's (Insert reads Stop during a run); None = the layout's.
    legend: str | None
    #: Counted guard: (taps, need, share of the window left in steps); None = none.
    guard: tuple[int, int, int] | None


def _kinds(layout: Layout) -> dict[str, int]:
    """Index of the key of each special kind (each exists once); characters and chips have no entry."""
    return {key.kind: key.index for key in layout.keys if key.kind not in ("char", "chip")}


def _unit(value: float) -> float:
    """``value`` clamped to 0..1; anything that is not a number is 0."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(max(v, 0.0), 1.0) if math.isfinite(v) else 0.0


def _key_states(view: KeyboardView, layout: Layout) -> dict[int, _KeyState]:
    """What to draw instead of the baked key, per key index: the session's lit keys and the state the view itself says.

    A private view drops everything that shows where a finger is or what it did (ghost, target, ok, drop: the whole
    keyboard pulses instead); the guard, the Send state and the toggles say nothing about the text and stay.
    """
    rank: dict[int, str] = {}

    def put(index: int | None, kind: str) -> None:
        if index is not None and (index not in rank or _LIT_RANK[kind] > _LIT_RANK[rank[index]]):
            rank[index] = kind

    for entry in view.lit:
        try:
            index, kind = entry
        except (TypeError, ValueError):
            continue
        if type(index) is not int or not 0 <= index < layout.count or kind not in _LIT_RANK:
            continue
        if view.private and kind in _TOUCH_KINDS:
            continue
        put(index, kind)
    by_kind = _kinds(layout)
    legends: dict[int, str] = {}
    guards: dict[int, tuple[int, int, int]] = {}
    if view.shift and view.lang == "en":
        put(by_kind.get("shift"), "armed")
    if view.private:
        put(by_kind.get("private"), "on")
    if view.armed_enter:
        put(by_kind.get("enter"), "armed")
    cv = view.compose
    if cv is not None:
        if cv.guard in _GUARD_KEY:
            guard_index = by_kind.get(_GUARD_KEY[cv.guard])
            put(guard_index, "armed")
            if guard_index is not None and cv.guard_need > 0:
                left_q = round(_unit(cv.guard_left) * GUARD_RING_STEPS)
                need = min(int(cv.guard_need), 8)
                guards[guard_index] = (max(0, min(int(cv.guard_taps), need)), need, left_q)
        if cv.state == "inserting" and "insert" in by_kind:
            put(by_kind["insert"], "on")
            legends[by_kind["insert"]] = "Stop"
        if cv.can_send:  # the baked Send key is the dim one (nothing to send); it lights when something can be
            put(by_kind.get("enter"), "on")
    return {index: _KeyState(kind, legends.get(index), guards.get(index)) for index, kind in rank.items()}


# --------------------------------------------------------------------------- text sprites


def _display(text: str) -> str:
    """``text`` in the order it is painted left to right; text with no Hebrew in it is returned as it is."""
    if any("א" <= c <= "ת" for c in text):
        return "".join(text[i] for i in bidi_order(text))
    return text


def _fit(text: str, font: ImageFont.FreeTypeFont, room: float, *, tail: bool = False) -> str:
    """``text``, or ``...`` and its start (its end with ``tail``) when it is wider than ``room`` pixels."""
    if font.getlength(text) <= room:
        return text
    dots = "..."
    k = len(text)
    while k > 0:
        cut = dots + text[len(text) - k :] if tail else text[:k] + dots
        if font.getlength(cut) <= room:
            return cut
        k -= 1
    return dots


def _text_row(
    text: str, font: ImageFont.FreeTypeFont, width: int, height: int, colour: int, align: str, *, tail: bool = False
) -> np.ndarray:
    """One fixed line of text in a ``width`` x ``height`` cell, vertically centred, an ellipsis when it is too long."""
    shown = _display(_fit(_clean(text), font, width, tail=tail))
    spare = width - font.getlength(shown)
    x = spare if align == "right" else spare / 2 if align == "centre" else 0.0
    ascent, descent = font.getmetrics()
    baseline = (height - (ascent + descent)) / 2 + ascent
    return _tint(_mask(width, height, font, [(shown, x, baseline, 255)]), colour)


@lru_cache(maxsize=48)
def _line_sprite(text: str, px: int, width: int, height: int, colour: int, align: str) -> np.ndarray:
    """A line of the fixed vocabulary (a strip sentence, the counter, a banner). Read-only."""
    out = _text_row(text, _font(px), width, height, colour, align)
    out.flags.writeable = False
    return out


class _Memo:
    """The last result for the sprites that are made of typed text (the box, the echo row, the chips)."""

    def __init__(self) -> None:
        self.key: Any = None
        self.value: Any = None

    def get(self, key: Any, make: Callable[[], _T]) -> _T:
        if self.key is None or self.key != key:
            value = make()
            self.key, self.value = key, value
        return self.value  # type: ignore[no-any-return]

    def clear(self) -> None:
        self.key = self.value = None


_BOX_MEMO = _Memo()
_ROW_MEMO = _Memo()
_CHIP_MEMO = _Memo()
#: The last composite: (base, geometry, the view without its counter, the image).
_LAST: list[Any] = []
_BASE_CACHE: dict[tuple[str, bool, int, str], np.ndarray] = {}
_BASE_CACHE_MAX = 12


def _pieces(display: str, dim: list[bool]) -> list[tuple[str, bool]]:
    """``display`` cut into runs of one brightness."""
    runs: list[tuple[str, bool]] = []
    for ch, is_dim in zip(display, dim, strict=True):
        if runs and runs[-1][1] == is_dim:
            runs[-1] = (runs[-1][0] + ch, is_dim)
        else:
            runs.append((ch, is_dim))
    return runs


class _BoxLine(NamedTuple):
    pieces: list[tuple[str, bool]]
    #: Left edge of the line in pixels (a line that ends in Hebrew leaves room for the caret on its left).
    x: float


def _box_lines(
    text: str, sent: int, font: ImageFont.FreeTypeFont, room: float, caret_w: int
) -> tuple[list[_BoxLine], float]:
    """The last ``BOX_LINES`` lines of ``text`` as paintable pieces, and the caret's x at the logical end.

    Wrapped by pixel width in logical order, then each line reordered on its own (bidi). When there are more lines, the
    first one shown starts with ``...`` and loses its own first characters as far as it takes to fit. The first
    ``sent`` logical characters are marked dim. The caret is a decoration: after the last character, or before it when
    that one is painted right to left.
    """
    spans = wrap_spans(text, room - caret_w, font.getlength)
    shown = spans[-BOX_LINES:]
    more = len(spans) > len(shown)
    lines: list[_BoxLine] = []
    caret = 0.0
    for n, (a, b) in enumerate(shown):
        segment, first = text[a:b], a
        prefix = ""
        if more and n == 0:
            prefix = "..."
            while segment and font.getlength(prefix + segment) > room - caret_w:
                segment, first = segment[1:], first + 1
        order = bidi_order(segment)
        display = "".join(segment[i] for i in order)
        pieces = _pieces(display, [first + i < sent for i in order])
        if prefix:
            pieces.insert(0, (prefix, False))
        ends_rtl = bool(segment) and bidi_directions(segment)[-1] == "R"
        x = float(caret_w) if ends_rtl else 0.0
        lines.append(_BoxLine(pieces, x))
        if n == len(shown) - 1:
            lead = font.getlength(prefix)
            if not segment:
                caret = x + lead
            else:
                where = order.index(len(segment) - 1)
                start = lead + font.getlength(display[:where])
                caret = x + (start - caret_w if ends_rtl else start + font.getlength(display[where]))
    return lines, caret


def _box_sprite(text: str, sent: int, placeholder: str, width: int, height: int, pitch: int) -> np.ndarray:
    """The box's text, the typed prefix dimmed, and the caret: premultiplied BGRA of the box's inner area."""
    unit = pitch / BASE_PITCH
    font = _font(_scaled(unit, BOX_PX))
    line_h = _scaled(unit, BOX_LINE_H)
    caret_w = max(2, _round(0.5 * _scaled(unit, BOX_PX)))
    ascent, descent = font.getmetrics()
    out = np.zeros((height, width, 4), np.uint8)
    top = max(0, (height - line_h * BOX_LINES) // 2)
    if not text and placeholder:
        shown = _display(_fit(placeholder, font, width))
        baseline = top + (line_h - (ascent + descent)) / 2 + ascent
        _over(out, _tint(_mask(width, height, font, [(shown, 0.0, baseline, 255)]), MUTED))
        return out
    lines, caret_x = _box_lines(text, sent, font, width, caret_w)
    dim_fill = round(255 * SENT_DIM)
    last_baseline = 0.0
    for i, line in enumerate(lines):
        baseline = top + i * line_h + (line_h - (ascent + descent)) / 2 + ascent
        items = []
        x = line.x
        for piece, is_dim in line.pieces:
            items.append((piece, x, baseline, dim_fill if is_dim else 255))
            x += font.getlength(piece)
        _over(out, _tint(_mask(width, height, font, items), LEGEND))
        last_baseline = baseline
    y0 = max(0, round(last_baseline - ascent))
    y1 = min(height, round(last_baseline + descent))
    x0 = max(0, min(round(caret_x), width - caret_w))
    if y1 > y0:
        block = np.zeros((height, width, 4), np.uint8)
        block[y0:y1, x0 : x0 + caret_w] = _tint(np.full((y1 - y0, caret_w), 255, np.uint8), CYAN, 0.85)
        _over(out, block)
    return out


def _row_sprite(echo: str, prompt: str, width: int, height: int, pitch: int) -> np.ndarray:
    """The echo row: the prompt in muted text above the typed tail, or whichever of the two there is."""
    unit = pitch / BASE_PITCH
    out = np.zeros((height, width, 4), np.uint8)
    both = bool(echo) and bool(prompt)
    font = _font(_scaled(unit, ROW_PX if both else STRIP_PX))
    rows = (
        [(prompt, MUTED, False), (echo, LEGEND, True)]
        if both
        else [(echo or prompt, LEGEND if echo else MUTED, bool(echo))]
    )
    cell = height // len(rows)
    for i, (line, colour, tail) in enumerate(rows):
        align = "right" if base_direction(line) == "R" else "left"
        _blit(out, _text_row(line, font, width, cell, colour, align, tail=tail), 0, i * cell)
    return out


def _chip_sprites(
    chips: tuple[str, ...], active: int | None, private: bool, cells: tuple[tuple[int, int], ...], pitch: int
) -> list[np.ndarray]:
    """The chip cells that have a chip: face (outlined in cyan for the active one) and the chip's text, ellipsised."""
    unit = pitch / BASE_PITCH
    font = _font(_scaled(unit, STRIP_PX))
    margin = _scaled(unit, 8)
    sprites = []
    for n, (text, (width, height)) in enumerate(zip(chips, cells, strict=False)):
        look = "on" if n == active else "normal"
        face = _face(width, height, _scaled(unit, 2), max(2.0, 6.0 * unit), look, _border_px(_LOOKS[look], unit))
        sprite = face.copy()
        room = max(width - 2 * margin, 1)
        shown = text
        if private:  # one bullet per character, as many as fit the cell and at most CHIP_MAX_BULLETS: never an ellipsis
            shown = BULLET * min(len(text), CHIP_MAX_BULLETS)
            while len(shown) > 1 and font.getlength(shown) > room:
                shown = shown[:-1]
        _blit(sprite, _text_row(shown, font, room, height, LEGEND, "centre"), margin, 0)
        sprite.flags.writeable = False
        sprites.append(sprite)
    return sprites


# --------------------------------------------------------------------------- fingertip rings


def _union(*distances: np.ndarray) -> np.ndarray:
    out = distances[0]
    for d in distances[1:]:
        out = np.minimum(out, d)
    return out


_NOTE_NAMES = frozenset({"noisy", "veto", "speed", "posture", "coherence", "hold"})
_RING_COLOURS = {"open": (WHITE, 0.95), "closing": (AMBER, 0.55), "pressed": (GREEN, 1.0), "latched": (GREY, 0.40)}


def _glyph(note: str, g: reticle._Grid, ox: float, width: float) -> list[reticle._Stroke]:
    """The mark for a ``note`` of the fixed vocabulary of 2.12.9, beside the ring. Never text: a shape per word."""
    x, y = g.x, g.y

    def seg(a: tuple[float, float], b: tuple[float, float]) -> np.ndarray:
        return reticle._segment(x, y, (ox + a[0], a[1]), (ox + b[0], b[1]), width)

    amber, red = _bgr(AMBER), _bgr(RED)
    if note == "noisy":  # a level meter that will not settle
        return [
            (_union(seg((-3.5, -1.5), (-3.5, 1.5)), seg((0, -4), (0, 4)), seg((3.5, -2.5), (3.5, 2.5))), amber, 1.0)
        ]
    if note == "veto":  # a cross
        return [(_union(seg((-3, -3), (3, 3)), seg((-3, 3), (3, -3))), red, 1.0)]
    if note == "speed":  # two chevrons
        chevrons = [seg((dx - 3, -3.5), (dx, 0)) for dx in (0.0, 4.0)] + [
            seg((dx - 3, 3.5), (dx, 0)) for dx in (0.0, 4.0)
        ]
        return [(_union(*chevrons), amber, 1.0)]
    if note == "posture":  # a caret pointing up
        return [(_union(seg((-4, 2.5), (0, -2.5)), seg((0, -2.5), (4, 2.5))), amber, 1.0)]
    if note == "coherence":  # an equals sign
        return [(_union(seg((-4, -2), (4, -2)), seg((-4, 2), (4, 2))), amber, 1.0)]
    if note == "hold":  # a pause sign
        return [(_union(seg((-2.5, -4), (-2.5, 4)), seg((2.5, -4), (2.5, 4))), amber, 1.0)]
    return []


@lru_cache(maxsize=256)
def _ring_sprite(
    r: int, state: str, filled: bool, fill_q: int, weak: bool, check: bool, named: bool, note: str
) -> np.ndarray:
    """One fingertip ring centred in a square sprite. Read-only.

    White is open, amber closing (the arc fills clockwise from 12 o'clock with the dip), green pressed, grey at two
    fifths opacity latched; a right hand is filled, a left hand an outline; a dotted ring is a shallow warm-up tap; a
    green dot is a recorded warm-up tap; a halo marks the finger the strip names; a small mark beside the ring is a
    note.
    """
    half = 2 * r + 10
    size = 2 * half + 1
    g = reticle._grid(size)
    width = max(1.6, 0.28 * r)
    colour, opacity = _RING_COLOURS[state]
    shade = 0.4 if state == "latched" else 1.0
    ring = reticle._ring(g, r, width)
    if weak:  # dotted: ten dashes round the ring
        on = (g.theta * np.float32(10 / (2 * math.pi))) % np.float32(1.0) < np.float32(0.55)
        ring = np.where(on, ring, np.float32(9.0))
    strokes: list[reticle._Stroke] = [(reticle._ring(g, r, width + 2.6), _bgr(SHADOW), 0.55 * shade)]
    if filled:
        strokes.append((reticle._disc(g, r), _bgr(colour), 0.28 * opacity))
    if named:
        strokes.append((reticle._ring(g, r + 4.5, 1.6), _bgr(CYAN), 0.9))
    strokes.append((ring, _bgr(colour), opacity))
    if state == "pressed":
        strokes.append((reticle._disc(g, r * 0.5), _bgr(GREEN), 0.7))
    if state == "closing" and fill_q > 0:
        sweep = 2 * math.pi * fill_q / RING_FILL_STEPS
        strokes.append((reticle._arc(g, r, width + 1.4, 0.0, sweep), _bgr(AMBER), 1.0))
    if check:
        dot = reticle._length(g.x - 0.9 * r, g.y + 0.9 * r)
        strokes.append((dot - 3.6, _bgr(SHADOW), 0.7))
        strokes.append((dot - 2.4, _bgr(GREEN), 1.0))
    if note in _NOTE_NAMES:
        strokes += _glyph(note, g, r + 10.0, 1.7)
    out = _flatten(size, size, strokes)
    out.flags.writeable = False
    return out


@lru_cache(maxsize=16)
def _home_sprite(r: int, filled: bool) -> np.ndarray:
    """A placement home: a thin cyan ring, filled for the right hand like the fingertip rings."""
    half = r + 5
    size = 2 * half + 1
    g = reticle._grid(size)
    strokes: list[reticle._Stroke] = [(reticle._ring(g, r, 3.6), _bgr(SHADOW), 0.5)]
    if filled:
        strokes.append((reticle._disc(g, r), _bgr(CYAN), 0.22))
    strokes.append((reticle._ring(g, r, 1.6), _bgr(CYAN), 0.9))
    out = _flatten(size, size, strokes)
    out.flags.writeable = False
    return out


@lru_cache(maxsize=16)
def _marker_sprite(pitch: int) -> np.ndarray:
    """The drift marker: an amber dot in a ring."""
    unit = pitch / BASE_PITCH
    r = max(3, _round(5 * unit))
    size = 2 * r + 3
    g = reticle._grid(size)
    out = _flatten(
        size,
        size,
        [(reticle._ring(g, r, 1.4), _bgr(AMBER), 1.0), (reticle._disc(g, r * 0.5), _bgr(AMBER), 1.0)],
    )
    out.flags.writeable = False
    return out


@lru_cache(maxsize=8)
def _pulse_sprite(width: int, height: int, pitch: int) -> np.ndarray:
    """The private-mode pulse: a cyan wash and outline over the whole keyboard block."""
    unit = pitch / BASE_PITCH
    d = _rrect_distance(width, height, 8.0 * unit)
    out = _flatten(
        height,
        width,
        [(d, _bgr(CYAN), 0.14), (np.abs(d + 1.5 * unit) - 1.5 * unit, _bgr(CYAN), 0.85)],
    )
    out.flags.writeable = False
    return out


# --------------------------------------------------------------------------- the baked keyboard


def _bake_faces(geometry: KeyboardGeometry, commit: Commit) -> np.ndarray:
    """Panels, key faces and the dim dead and chip cells, with no text: the part of the bake that needs no font."""
    m = _metrics(geometry.pitch, commit)
    unit = m.unit
    out = np.zeros((geometry.height, geometry.width, 4), np.uint8)
    radius = max(2.0, 8.0 * unit)
    panels = [(geometry.strip, "strip"), (geometry.compose if geometry.compose is not None else geometry.echo, "panel")]
    for (x, y, w, h), look_name in panels:
        if w > 0 and h > 0:
            _blit(out, _face(w, h, 0, radius, look_name, _border_px(_LOOKS[look_name], unit)), x, y)
    for key in layout_for(commit).keys:
        x, y, w, h = geometry.keys[key.index]
        name = "chip" if key.kind == "chip" else "normal"
        _blit(out, _face(w, h, _scaled(unit, 2), max(2.0, 6.0 * unit), name, _border_px(_LOOKS[name], unit)), x, y)
    for x, y, w, h in _gap_rects(geometry.pitch, commit):
        _blit(out, _face(w, h, _scaled(unit, 2), max(2.0, 6.0 * unit), "dead", 0.0), x, y)
    return out


def _bake(lang: Lang, shift: bool, geometry: KeyboardGeometry, commit: Commit) -> np.ndarray:
    """The faces with every legend: the whole baked keyboard."""
    out = _bake_faces(geometry, commit)
    for key in layout_for(commit).keys:
        text = _clean(key_legend(key, lang, shift, commit))
        if key.kind == "chip" or not text.strip():
            continue
        x, y, w, h = geometry.keys[key.index]
        # Send is baked dim: it is available only after a completed Insert, and `compose` lights it then (2.13.6)
        look = "dim" if commit == "review" and key.kind == "enter" else "normal"
        # the sprite is the whole cell (face, border, legend): it replaces the plain face drawn above
        _blit(out, _key_sprite(w, h, geometry.pitch, look, text, None), x, y, replace_pixels=True)
    return out


def _check_geometry(geometry: KeyboardGeometry, commit: Commit) -> None:
    """The geometry belongs to this layout and pitch; anything else would draw keys in the wrong places."""
    if commit not in ("direct", "review"):
        raise KeyboardDrawError("size")
    m = _metrics(geometry.pitch, commit)
    if (
        len(geometry.keys) != layout_for(commit).count
        or (geometry.width, geometry.height) != (m.width, m.height)
        or geometry.pitch < MIN_PITCH
    ):
        raise KeyboardDrawError("size")


def bake_base(lang: Lang, shift: bool, geometry: KeyboardGeometry, commit: Commit = "direct") -> np.ndarray:
    """Premultiplied BGRA ``(h, w, 4)``; cached by ``(lang, shift, pitch, commit)``."""
    return _guarded(_bake_cached, lang, bool(shift), geometry, commit)


def _bake_cached(lang: Lang, shift: bool, geometry: KeyboardGeometry, commit: Commit) -> np.ndarray:
    if lang not in ("en", "he"):
        raise KeyboardDrawError("internal")
    _check_geometry(geometry, commit)
    key = (lang, shift, geometry.pitch, commit)
    cached = _BASE_CACHE.get(key)
    if cached is not None:
        return cached
    base = _bake(lang, shift, geometry, commit)
    base.flags.writeable = False
    if len(_BASE_CACHE) >= _BASE_CACHE_MAX:
        _BASE_CACHE.pop(next(iter(_BASE_CACHE)))
    _BASE_CACHE[key] = base
    return base


# --------------------------------------------------------------------------- the frame


def compose(base: np.ndarray, geometry: KeyboardGeometry, view: KeyboardView) -> np.ndarray:
    """A new array each call; the previous array object when the view is equal apart from ``seq``."""
    return _guarded(_compose, base, geometry, view)


def _compose(base: np.ndarray, geometry: KeyboardGeometry, view: KeyboardView) -> np.ndarray:
    if not isinstance(base, np.ndarray) or base.dtype != np.uint8 or base.shape != (geometry.height, geometry.width, 4):
        raise KeyboardDrawError("surface")
    commit = view.commit
    _check_geometry(geometry, commit)
    signature = replace(view, seq=0)
    if _LAST:
        last_base, last_geometry, last_signature, last_image = _LAST
        if (
            last_base is base
            and (last_geometry is geometry or last_geometry == geometry)
            and last_signature == signature
        ):
            return last_image  # type: ignore[no-any-return]
    layout = layout_for(commit)
    m = _metrics(geometry.pitch, commit)
    out = base.copy()
    _paint_keys(out, geometry, view, layout)
    _paint_strip(out, geometry, m, view)
    cv = view.compose
    if cv is not None and geometry.compose is not None:
        _paint_box(out, geometry, m, view, cv)
    elif geometry.echo[3] > 0:
        _paint_echo(out, geometry, m, view)
    if view.private and view.pulse:
        _blit(
            out,
            _pulse_sprite(geometry.width - 2 * m.pad, geometry.height - m.y_keys - m.pad, geometry.pitch),
            m.pad,
            m.y_keys,
        )
    if view.hold is not None:
        _dim(out, geometry.strip[1] + geometry.strip[3])
    _paint_rings(out, geometry, m, view)
    out.flags.writeable = False
    _LAST[:] = [base, geometry, signature, out]
    return out


def _paint_keys(out: np.ndarray, geometry: KeyboardGeometry, view: KeyboardView, layout: Layout) -> None:
    pitch = geometry.pitch
    for index, state in _key_states(view, layout).items():
        x, y, w, h = geometry.keys[index]
        text = state.legend
        if text is None:
            text = key_legend(layout.keys[index], view.lang, view.shift, view.commit)
        _blit(out, _key_sprite(w, h, pitch, state.look, _clean(text), state.guard), x, y, replace_pixels=True)
    cv = view.compose
    if cv is not None and cv.chips:
        chips = tuple(str(c) for c in cv.chips[:CHIP_CELLS])
        rects = [geometry.keys[k.index] for k in layout.keys if k.kind == "chip"]
        cells = tuple((w, h) for _, _, w, h in rects)
        active = cv.chip_active if type(cv.chip_active) is int else None
        sprites = _CHIP_MEMO.get(
            (chips, active, view.private, pitch, cells),
            lambda: _chip_sprites(chips, active, view.private, cells, pitch),
        )
        for sprite, (x, y, _w, _h) in zip(sprites, rects, strict=False):
            _blit(out, sprite, x, y, replace_pixels=True)


def _paint_strip(out: np.ndarray, geometry: KeyboardGeometry, m: _Metrics, view: KeyboardView) -> None:
    sx, sy, sw, sh = geometry.strip
    unit = m.unit
    px = _scaled(unit, STRIP_PX)
    line_h = _scaled(unit, STRIP_LINE_H)
    margin = _scaled(unit, 10)
    right = sx + sw - margin
    cv = view.compose
    if cv is not None and view.commit == "review":
        length = max(0, int(cv.length))
        wide = math.ceil(_font(px).getlength(f"{COMPOSE_MAX}/{COMPOSE_MAX}")) + 2
        colour = AMBER if length >= COUNT_AMBER_FROM else MUTED
        _blit(
            out,
            _line_sprite(f"{length}/{COMPOSE_MAX}", px, wide, line_h, colour, "right"),
            right - wide,
            sy + (sh - line_h) // 2,
        )
        right -= wide + margin
    if view.drift:
        marker = _marker_sprite(geometry.pitch)
        _blit(out, marker, right - marker.shape[1], sy + (sh - marker.shape[0]) // 2)
        right -= marker.shape[1] + margin
    room = max(right - (sx + margin), 1)
    banner = _clean(view.banner)
    if banner:
        top = sy + (sh - 2 * line_h) // 2
        colour = AMBER if view.banner_level == "warn" else MUTED
        _blit(out, _line_sprite(banner, px, room, line_h, colour, "left"), sx + margin, top)
        status_y = top + line_h
    else:
        status_y = sy + (sh - line_h) // 2
    status = _clean(view.strip)
    if status:
        colour = AMBER if view.hold is not None else LEGEND
        _blit(out, _line_sprite(status, px, room, line_h, colour, "left"), sx + margin, status_y)
    progress = _unit(view.progress)
    if progress > 0:
        bar_h = max(2, _round(3 * unit))
        x0, x1 = sx + margin, sx + sw - margin
        y1 = sy + sh - _scaled(unit, 3)
        _blit(out, _tint(np.full((bar_h, x1 - x0), 255, np.uint8), WHITE, 0.18), x0, y1 - bar_h)
        filled = round((x1 - x0) * progress)
        if filled > 0:
            out[y1 - bar_h : y1, x0 : x0 + filled] = _tint(np.full((bar_h, filled), 255, np.uint8), CYAN)


def _paint_echo(out: np.ndarray, geometry: KeyboardGeometry, m: _Metrics, view: KeyboardView) -> None:
    ex, ey, ew, eh = geometry.echo
    echo = "" if view.private else _clean(view.echo)
    prompt = _clean(view.prompt)
    if prompt == _clean(view.strip):
        prompt = ""  # the practice strip already says it
    if not echo and not prompt:
        return
    pad = _scaled(m.unit, 10)
    width = ew - 2 * pad
    sprite = _ROW_MEMO.get(
        (echo, prompt, width, eh, geometry.pitch), lambda: _row_sprite(echo, prompt, width, eh, geometry.pitch)
    )
    _blit(out, sprite, ex + pad, ey)


def _paint_box(out: np.ndarray, geometry: KeyboardGeometry, m: _Metrics, view: KeyboardView, cv: ComposeView) -> None:
    bx, by, bw, bh = geometry.compose  # type: ignore[misc]
    unit = m.unit
    pad_x, pad_y = _scaled(unit, 8), _scaled(unit, 6)
    inner_w, inner_h = bw - 2 * pad_x, bh - 2 * pad_y
    # The box shows what Insert types and nothing else: a private view paints one bullet per character whatever the
    # text it was given (the session masks it too; this is the second lock).
    text = BULLET * len(cv.text) if view.private else _clean(cv.text)
    sent = max(0, min(int(cv.sent), len(text)))
    prompt = "" if view.private else _clean(view.prompt)
    if prompt == _clean(view.strip):
        prompt = ""
    sprite = _BOX_MEMO.get(
        (text, sent, prompt, inner_w, inner_h, geometry.pitch),
        lambda: _box_sprite(text, sent, prompt, inner_w, inner_h, geometry.pitch),
    )
    _blit(out, sprite, bx + pad_x, by + pad_y)
    cue = {"inserting": CYAN, "aborted": AMBER}.get(cv.state)
    if cue is not None:
        _blit(out, _outline(bw, bh, max(2.0, 8.0 * unit), cue, max(1.0, 2.0 * unit)), bx, by)


def _dim(out: np.ndarray, top: int) -> None:
    """Everything from row ``top`` down to ``HOLD_DIM`` of its brightness (premultiplied: scale colour and alpha)."""
    part = out[top:]
    part[...] = (part.astype(np.uint16) * round(255 * HOLD_DIM) + 127) // 255


def _paint_rings(out: np.ndarray, geometry: KeyboardGeometry, m: _Metrics, view: KeyboardView) -> None:
    pitch = geometry.pitch
    unit = m.unit
    r = max(3, _round(RING_RADIUS * unit))

    def at(u: float, v: float) -> tuple[int, int] | None:
        try:
            x, y = m.pad + u * pitch, m.y_keys + v * pitch
        except (TypeError, OverflowError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > 1e6 or abs(y) > 1e6:
            return None
        return _round(x), _round(y)

    for home in view.homes:
        try:
            u, v, side = home
        except (TypeError, ValueError):
            continue
        spot = at(u, v)
        if spot is not None:
            sprite = _home_sprite(max(4, _round(HOME_RADIUS * unit)), side == "right")
            half = sprite.shape[0] // 2
            _blit(out, sprite, spot[0] - half, spot[1] - half)
    order = {"latched": 0, "open": 1, "closing": 2, "pressed": 3}
    tips = [t for t in view.tips[:MAX_RINGS] if isinstance(t, TipView) and t.state in order]
    for t in sorted(tips, key=lambda t: order[t.state]):
        spot = at(t.u, t.v)
        if spot is None:
            continue
        fill_q = round(_unit(t.fill) * RING_FILL_STEPS) if t.state == "closing" and not view.private else 0
        note = t.note if t.note in _NOTE_NAMES or t.note == "weak" else ""
        sprite = _ring_sprite(
            r,
            t.state,
            t.side == "right",
            fill_q,
            note == "weak",
            bool(t.done) and view.phase == "warmup",
            bool(t.named),
            note,
        )
        half = sprite.shape[0] // 2
        _blit(out, sprite, spot[0] - half, spot[1] - half)


# --------------------------------------------------------------------------- housekeeping

_CACHES = (
    _face,
    _outline,
    _key_sprite,
    _guard_overlay,
    _line_sprite,
    _ring_sprite,
    _home_sprite,
    _marker_sprite,
    _pulse_sprite,
    _font_at,
)


def clear_cache() -> None:
    """Forgets every sprite and the last frame, the ones made of typed text included. The keyboard calls it on close."""
    for memo in (_BOX_MEMO, _ROW_MEMO, _CHIP_MEMO):
        memo.clear()
    _LAST.clear()
    _BASE_CACHE.clear()
    for cached in _CACHES:
        cached.cache_clear()
