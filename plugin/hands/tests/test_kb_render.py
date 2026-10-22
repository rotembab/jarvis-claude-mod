"""overlay/keyboard_render.py: geometry, the baked keys and the per-frame composite (O1-O3, O8-O10, O40-O45, O47, O80,
O81, X46, S22b).

Pure numpy and Pillow, so everything here runs without a screen. Pixels are premultiplied BGRA. Three kinds of check:

* what is drawn where (a region differs from the plain keyboard, or does not),
* colours of the looks the design names (amber target, green ok, red drop, cyan on),
* what the painter is asked to draw: a spy on Pillow's text call sees every string, so "the Hebrew echo is painted in
  visual order" and "a private box paints bullets only" are tested on the real path, not on a re-implementation.

Key counts are never written here (P11): they come from the layout.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from PIL import ImageDraw, ImageFont

from jarvis_hands.clock import now
from jarvis_hands.keyboard.layout import layout_for
from jarvis_hands.keyboard.limits import COMPOSE_MAX
from jarvis_hands.overlay import keyboard_render as kr
from jarvis_hands.overlay.base import ComposeView, KeyboardView, TipView
from jarvis_hands.overlay.text import bidi_display

HEBREW = "שלום"
WORK = (0, 0, 3840, 2160)
RECT = tuple[int, int, int, int]

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    """Every test starts from empty sprite caches, so a spy on the text call sees the real drawing."""
    kr.clear_cache()
    yield
    kr.clear_cache()


needs_font = pytest.mark.skipif(not kr.font_available(), reason="no font with Latin and Hebrew glyphs on this machine")


# --------------------------------------------------------------------------- builders


def view(**kw: Any) -> KeyboardView:
    fields: dict[str, Any] = {
        "seq": 0,
        "mode": "live",
        "phase": "typing",
        "lang": "en",
        "shift": False,
        "private": False,
        "pulse": False,
        "hold": None,
        "armed_enter": False,
        "drift": False,
        "tips": (),
        "homes": (),
        "lit": (),
        "strip": "",
        "echo": "",
        "prompt": "",
        "progress": 0.0,
    }
    fields.update(kw)
    return KeyboardView(**fields)


def box(text: str = "", **kw: Any) -> ComposeView:
    fields: dict[str, Any] = {
        "text": text,
        "length": len(text),
        "state": "composing",
        "sent": 0,
        "guard": None,
        "guard_taps": 0,
        "guard_need": 0,
        "guard_left": 0.0,
        "can_send": False,
        "full": False,
    }
    fields.update(kw)
    return ComposeView(**fields)


def review(text: str = "", **kw: Any) -> KeyboardView:
    """A review-mode view; ``compose`` keyword arguments go to the box."""
    compose = kw.pop("compose", None) or box(text)
    return view(commit="review", compose=compose, **kw)


def tip(u: float, v: float, state: str = "open", side: str = "right", **kw: Any) -> TipView:
    return TipView(u, v, side, kw.pop("finger", 0), state, **kw)  # type: ignore[arg-type]


def geometry_of(v: KeyboardView, dpi: int = 96) -> kr.KeyboardGeometry:
    return kr.keyboard_geometry(WORK, v.size, dpi, v.dock, v.commit)[0]


def render(v: KeyboardView) -> np.ndarray:
    g = geometry_of(v)
    return kr.compose(kr.bake_base(v.lang, v.shift, g, v.commit), g, v)


def plain(v: KeyboardView) -> np.ndarray:
    """The same keyboard with nothing on it: the reference that a region is compared against."""
    return kr.bake_base(v.lang, v.shift, geometry_of(v), v.commit)


def region(img: np.ndarray, rect: RECT) -> np.ndarray:
    x, y, w, h = rect
    return img[y : y + h, x : x + w]


def changed(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.any(a != b, axis=-1)


def changed_bbox(a: np.ndarray, b: np.ndarray) -> RECT | None:
    mask = changed(a, b)
    if not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def inside(inner: RECT, outer: RECT) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def outside_equal(a: np.ndarray, b: np.ndarray, rect: RECT) -> bool:
    """``a`` and ``b`` are the same everywhere but inside ``rect``."""
    x, y, w, h = rect
    mask = np.ones(a.shape[:2], bool)
    mask[y : y + h, x : x + w] = False
    return not changed(a, b)[mask].any()


def rgb(pixel: np.ndarray) -> tuple[int, int, int]:
    """Straight (R, G, B) of a premultiplied BGRA pixel."""
    b, g, r, a = (int(c) for c in pixel)
    if a == 0:
        return 0, 0, 0
    return min(255, round(r * 255 / a)), min(255, round(g * 255 / a)), min(255, round(b * 255 / a))


def amber_like(img: np.ndarray, rect: RECT | None = None) -> int:
    """Pixels of the region that look like the design's amber (``#F5B544``)."""
    part = img if rect is None else region(img, rect)
    a = part[..., 3].astype(int)
    safe = np.maximum(a, 1)
    r, g, b = (part[..., i].astype(int) * 255 // safe for i in (2, 1, 0))
    return int(((a > 120) & (r >= 165) & (g > 105) & (g <= 215) & (b <= 140)).sum())


def cyan_like(img: np.ndarray, rect: RECT | None = None) -> int:
    part = img if rect is None else region(img, rect)
    a = part[..., 3].astype(int)
    safe = np.maximum(a, 1)
    r, g, b = (part[..., i].astype(int) * 255 // safe for i in (2, 1, 0))
    return int(((a > 120) & (r < 90) & (g > 170) & (b > 200)).sum())


def key_rect(v: KeyboardView, kind: str, char: str | None = None) -> RECT:
    layout = layout_for(v.commit)
    key = layout.find(kind=None if char else kind, char=char, lang=v.lang)  # type: ignore[arg-type]
    return geometry_of(v).keys[key.index]


def face_sample(img: np.ndarray, rect: RECT) -> tuple[int, int, int]:
    """Straight colour of the face near a key's top-left corner, away from its legend."""
    x, y, _w, _h = rect
    return rgb(img[y + 8, x + 8])


class TextSpy:
    """Every string handed to Pillow's text call while it is installed, with where it was put.

    ``calls`` is what the content painters draw (strip, counter, box, echo, chips); ``legends`` is what the key sprites
    draw, so a test about the typed text is not confused by the letters on the keys.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float]] = []
        self.legends: list[str] = []
        self.in_key = 0

    def reset(self) -> None:
        self.calls.clear()
        self.legends.clear()

    @property
    def texts(self) -> list[str]:
        return [t for t, _, _ in self.calls]

    def joined(self) -> str:
        return "".join(self.texts)

    def without_counter(self) -> list[str]:
        return [t for t in self.texts if not re.fullmatch(r"\d+/\d+", t)]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> TextSpy:
    seen = TextSpy()
    original = ImageDraw.ImageDraw.text
    key_sprite = kr._key_sprite

    def recording(self: Any, xy: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
        if seen.in_key:
            seen.legends.append(text)
        else:
            seen.calls.append((text, float(xy[0]), float(xy[1])))
        return original(self, xy, text, *args, **kwargs)

    def in_key_sprite(*args: Any) -> Any:
        seen.in_key += 1
        try:
            return key_sprite(*args)
        finally:
            seen.in_key -= 1

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", recording)
    monkeypatch.setattr(kr, "_key_sprite", in_key_sprite)
    return seen


# --------------------------------------------------------------------------- geometry (O1, O40)

DPIS = (96, 144, 192)
SIZES = (0.6, 1.0, 1.6)


def test_o1_direct_geometry_at_the_design_size() -> None:
    g, origin = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")
    assert g.pitch == 56
    assert (g.width, g.height) == (660, 312)  # "about 660 x 310"
    assert g.strip == (8, 8, 644, 40)  # not a key count: pixels
    assert g.echo == (8, 48, 644, 32)
    assert g.compose is None
    assert len(g.keys) == layout_for("direct").count
    assert origin == ((WORK[2] - 660) // 2, 12)


@pytest.mark.parametrize("commit", ["direct", "review"])
@pytest.mark.parametrize("dock", ["top", "bottom"])
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dpi", DPIS)
def test_o1_o40_pitch_and_position_follow_size_dpi_and_dock(dpi: int, size: float, dock: str, commit: str) -> None:
    work = (100, 50, 3840, 2160)
    g, (left, top) = kr.keyboard_geometry(work, size, dpi, dock, commit)  # type: ignore[arg-type]
    assert g.pitch == round(56 * size * dpi / 96)
    assert left == work[0] + (work[2] - g.width) // 2  # centred
    assert top == (work[1] + 12 if dock == "top" else work[1] + work[3] - g.height - 12)
    assert work[0] <= left and left + g.width <= work[0] + work[2]
    assert work[1] <= top and top + g.height <= work[1] + work[3]
    assert len(g.keys) == layout_for(commit).count  # type: ignore[arg-type]


def test_o1_a_secondary_monitor_left_of_the_primary_has_a_negative_origin() -> None:
    g, (left, top) = kr.keyboard_geometry((-1920, 0, 1920, 1080), 1.0, 96, "bottom", "direct")
    assert left >= -1920 and left + g.width <= 0
    assert top == 1080 - g.height - 12


@pytest.mark.parametrize("commit", ["direct", "review"])
@pytest.mark.parametrize("size", SIZES)
def test_o1_keys_tile_their_rows_without_overlap_and_stay_inside_the_window(size: float, commit: str) -> None:
    g, _ = kr.keyboard_geometry(WORK, size, 144, "top", commit)  # type: ignore[arg-type]
    window = (0, 0, g.width, g.height)
    taken = np.zeros((g.height, g.width), np.uint8)
    for x, y, w, h in g.keys:
        assert w > 0 and h == g.pitch
        assert inside((x, y, w, h), window)
        taken[y : y + h, x : x + w] += 1
    assert taken.max() == 1  # no two cells share a pixel
    assert inside(g.strip, window) and inside(g.echo, window)
    # the strip sits above everything else, then the echo row (direct) or the box (review), then the keys
    assert g.strip[1] + g.strip[3] <= min(r[1] for r in g.keys)
    if g.compose is not None:
        assert inside(g.compose, window)
        assert g.strip[1] + g.strip[3] <= g.compose[1]
        assert g.compose[1] + g.compose[3] <= min(r[1] for r in g.keys)


def test_o40_review_geometry_at_the_design_size() -> None:
    g, _ = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")
    assert (g.width, g.height) == (660, 420)
    assert g.strip == (8, 8, 644, 40)  # not a key count: pixels
    assert g.compose == (8, 48, 644, 78)  # the box: three lines of 22 plus 12 of padding
    assert g.echo[3] == 0  # no echo row in review mode; its place is the box's
    assert g.compose[1] + g.compose[3] + 6 == min(r[1] for r in g.keys)  # the 6 px gap
    assert len(g.keys) == layout_for("review").count


def test_o40_row_four_cells_sit_at_the_design_columns() -> None:
    g, _ = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")
    layout = layout_for("review")
    chips = [k for k in layout.keys if k.kind == "chip"]
    spans = {
        layout.find("clear").index: (0.0, 1.5),
        layout.find("enter").index: (1.5, 3.0),
        layout.find("insert").index: (9.5, 11.5),
        **dict(zip((k.index for k in chips), [(3.25, 5.25), (5.25, 7.25), (7.25, 9.25)], strict=True)),
    }
    for index, (u0, u1) in spans.items():
        x, y, w, _h = g.keys[index]
        assert (x - 8, x - 8 + w) == (round(u0 * 56), round(u1 * 56))
        assert y == g.keys[layout.find("clear").index][1]  # one row
    assert g.keys[layout.find("clear").index][1] > g.keys[layout.find("space").index][1]  # below the Space row


def test_o40_every_key_sits_at_its_layout_column_in_both_layouts() -> None:
    for commit in ("direct", "review"):
        g, _ = kr.keyboard_geometry(WORK, 1.0, 96, "top", commit)  # type: ignore[arg-type]
        for key in layout_for(commit).keys:  # type: ignore[arg-type]
            x, y, w, _h = g.keys[key.index]
            assert x == 8 + round(key.col * 56)
            assert x + w == 8 + round((key.col + key.width) * 56)
            assert y == g.keys[0][1] + key.row * 56 - layout_for(commit).keys[0].row * 56  # type: ignore[arg-type]


def test_o40_dead_cells_have_no_rect_and_are_drawn_dim() -> None:
    v = review("")
    g = geometry_of(v)
    base = plain(v)
    layout = layout_for("review")
    assert layout.gaps  # row 0's tail and the two slivers of row 4
    key_alpha = int(base[g.keys[layout.find(char="e").index][1] + 28, g.keys[layout.find(char="e").index][0] + 8, 3])
    pitch = g.pitch
    for row, c0, c1 in layout.gaps:
        x0 = 8 + round(c0 * pitch)
        x1 = 8 + round(c1 * pitch)
        y0 = g.keys[0][1] + row * pitch
        cell = region(base, (x0, y0, x1 - x0, pitch))
        for key_rect_ in g.keys:  # a dead cell is none of the key rects
            assert not (key_rect_[0] < x1 and x0 < key_rect_[0] + key_rect_[2] and key_rect_[1] == y0)
        if x1 - x0 > pitch // 2:  # the wide dead cell of row 0: dim, not empty, not a key
            centre = int(cell[pitch // 2, (x1 - x0) // 2, 3])
            assert 0 < centre < key_alpha


def test_geometry_is_deterministic_and_hashable() -> None:
    a = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")
    b = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")
    assert a == b
    assert hash(a[0]) == hash(b[0])


@pytest.mark.parametrize(
    "args",
    [
        ((0, 0, 0, 0), 1.0, 96, "top"),
        ((0, 0, 1920, 1080), 0.0, 96, "top"),
        ((0, 0, 1920, 1080), -1.0, 96, "top"),
        ((0, 0, 1920, 1080), float("nan"), 96, "top"),
        ((0, 0, 1920, 1080), float("inf"), 96, "top"),
        ((0, 0, 1920, 1080), 1.0, 0, "top"),
        ((0, 0, 1920, 1080), 1.0, 96, "left"),
        ((0, 0, 150, 100), 1.0, 96, "top"),  # smaller than the smallest keyboard
        (None, 1.0, 96, "top"),
    ],
)
def test_a_size_that_cannot_be_drawn_is_a_size_error(args: Any) -> None:
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.keyboard_geometry(*args)
    assert caught.value.code == "size"
    assert str(caught.value) == "size"


def test_a_keyboard_wider_than_the_work_area_shrinks_to_fit() -> None:
    work = (0, 0, 900, 600)
    g, (left, top) = kr.keyboard_geometry(work, 1.6, 144, "top", "review")
    assert g.width <= work[2] and g.height + 24 <= work[3]
    assert g.pitch < round(56 * 1.6 * 1.5)
    assert left >= 0 and top >= 12


# --------------------------------------------------------------------------- fonts (O9)


@needs_font
def test_o9_a_font_with_latin_and_hebrew_glyphs_loads() -> None:
    assert kr.font_available() is True
    img = render(view(echo="a" + HEBREW))
    assert img.any()


def test_o9_with_no_font_none_is_offered_and_drawing_says_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kr, "_font_candidates", lambda: [])
    kr._font_path.cache_clear()
    try:
        assert kr.font_available() is False
        g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
        with pytest.raises(kr.KeyboardDrawError) as caught:
            kr.bake_base("en", False, g, "direct")
        assert caught.value.code == "font"
    finally:
        monkeypatch.undo()
        kr._font_path.cache_clear()


@needs_font
def test_o9_a_font_that_lacks_hebrew_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    real = kr._covers
    monkeypatch.setattr(kr, "_covers", lambda font, ch: False if "\u05d0" <= ch <= "\u05ea" else real(font, ch))
    kr._font_path.cache_clear()
    try:
        assert kr.font_available() is False
    finally:
        monkeypatch.undo()
        kr._font_path.cache_clear()


@needs_font
def test_o9_an_unreadable_candidate_is_skipped_for_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    good = kr._font_path()
    monkeypatch.setattr(kr, "_font_candidates", lambda: ["/no/such/dir/missing.ttf", good])
    kr._font_path.cache_clear()
    try:
        assert kr.font_available() is True
        assert kr._font_path() == good
    finally:
        monkeypatch.undo()
        kr._font_path.cache_clear()


def test_the_font_candidates_are_the_windows_fonts_first_then_dejavu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINDIR", "C:\\Windows")
    names = [c.replace("\\", "/").rsplit("/", 1)[-1].lower() for c in kr._font_candidates()]
    assert names[:3] == ["segoeui.ttf", "arial.ttf", "tahoma.ttf"]
    assert "dejavusans.ttf" in names


# --------------------------------------------------------------------------- the baked keyboard (O2, O45)


@needs_font
def test_o2_bake_gives_a_premultiplied_read_only_image_of_the_window_size() -> None:
    for commit in ("direct", "review"):
        g, _ = kr.keyboard_geometry(WORK, 1.0, 96, "top", commit)  # type: ignore[arg-type]
        base = kr.bake_base("en", False, g, commit)  # type: ignore[arg-type]
        assert base.shape == (g.height, g.width, 4) and base.dtype == np.uint8
        assert not base.flags.writeable
        assert (base[..., :3] <= base[..., 3:4]).all()  # premultiplied: no colour above its alpha
        assert base[..., 3].max() > 0
        assert base[0, 0, 3] == 0  # the window corner is clear


@needs_font
def test_o2_the_bake_is_cached_per_lang_shift_pitch_and_commit() -> None:
    g1 = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    g2 = kr.keyboard_geometry(WORK, 1.6, 96, "top", "direct")[0]
    gr = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")[0]
    a = kr.bake_base("en", False, g1, "direct")
    assert kr.bake_base("en", False, g1, "direct") is a
    assert kr.bake_base("en", False, kr.keyboard_geometry(WORK, 1.0, 96, "bottom", "direct")[0], "direct") is a
    others = [
        kr.bake_base("he", False, g1, "direct"),
        kr.bake_base("en", True, g1, "direct"),
        kr.bake_base("en", False, g2, "direct"),
        kr.bake_base("en", False, gr, "review"),
    ]
    assert all(o is not a for o in others)
    assert len({id(o) for o in others}) == len(others)
    assert kr.bake_base("he", False, g1, "direct") is others[0]


@needs_font
def test_o2_english_and_hebrew_have_different_letters_and_the_same_special_keys() -> None:
    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    en, he = kr.bake_base("en", False, g, "direct"), kr.bake_base("he", False, g, "direct")
    layout = layout_for("direct")
    space = g.keys[layout.find("space").index]
    assert not changed(region(en, space), region(he, space)).any()
    assert changed(
        region(en, g.keys[layout.find(char="e").index]), region(he, g.keys[layout.find(char="e").index])
    ).any()
    lang = g.keys[layout.find("lang").index]
    assert changed(region(en, lang), region(he, lang)).any()  # EN / HE


@needs_font
def test_o2_shift_changes_english_letters_and_is_inert_in_hebrew() -> None:
    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    layout = layout_for("direct")
    q = g.keys[layout.find(char="q").index]
    shift_key = g.keys[layout.find("shift").index]
    off, on = kr.bake_base("en", False, g, "direct"), kr.bake_base("en", True, g, "direct")
    assert changed(region(off, q), region(on, q)).any()  # q -> Q
    assert not changed(region(off, shift_key), region(on, shift_key)).any()  # the Shift key itself is lit by `lit`
    assert np.array_equal(kr.bake_base("he", False, g, "direct"), kr.bake_base("he", True, g, "direct"))


@needs_font
def test_o2_a_legend_is_centred_on_its_key() -> None:
    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    base = kr.bake_base("en", True, g, "direct")  # capitals: the cap height is what is centred
    x, y, w, h = g.keys[layout_for("direct").find(char="h").index]
    ink = (base[y + 4 : y + h - 4, x + 4 : x + w - 4, :3] > 170).all(axis=-1) & (
        base[y + 4 : y + h - 4, x + 4 : x + w - 4, 3] > 200
    )
    ys, xs = np.nonzero(ink)
    assert ink.any()
    assert abs((xs.min() + xs.max()) / 2 + 4 - w / 2) <= 2
    assert abs((ys.min() + ys.max()) / 2 + 4 - h / 2) <= 2


@needs_font
def test_the_legends_painted_are_the_layouts_legends(spy: TextSpy) -> None:
    for commit in ("direct", "review"):
        spy.reset()
        kr.clear_cache()
        g = kr.keyboard_geometry(WORK, 1.0, 96, "top", commit)[0]  # type: ignore[arg-type]
        kr.bake_base("en", False, g, commit)  # type: ignore[arg-type]
        painted = set(spy.legends)
        layout = layout_for(commit)  # type: ignore[arg-type]
        from jarvis_hands.keyboard.layout import legend

        expected = {legend(k, "en", False, commit) for k in layout.keys} - {"", " "}  # type: ignore[arg-type]
        assert expected <= painted
        assert ("Send" in painted) == (commit == "review")
        assert ("Enter" in painted) == (commit == "direct")
        assert ("Clear" in painted) == (commit == "review")


@needs_font
def test_o45_the_bake_is_cached_across_calls_with_no_redraw(spy: TextSpy) -> None:
    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "direct")[0]
    kr.bake_base("en", False, g, "direct")
    drawn = len(spy.legends)
    assert drawn > 0
    kr.bake_base("en", False, g, "direct")
    assert len(spy.legends) == drawn


#: sha256 of the font-free faces (panels, key faces, dead and chip cells) at 96 DPI and size 1.0. There is no first
#: version of this layer to hash against (the keyboard is new), so the hash is the one this implementation produced; it
#: pins the faces against an accidental change of the shapes, the alphas or the rounding. Regenerate on purpose only.
FACES_SHA256 = {
    "direct": "597dd7152eecdc588549309175a90102db0291b8ccba352a54d88a445ee99c34",
    "review": "e7dfba6b068d5f86ab77d3db5ecea14d19b5d243667a8f172f00dc28e46a2b17",
}


@pytest.mark.parametrize("commit", ["direct", "review"])
def test_o45_the_font_free_faces_match_their_golden_hash(commit: str) -> None:
    import hashlib

    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", commit)[0]  # type: ignore[arg-type]
    faces = kr._bake_faces(g, commit)  # type: ignore[arg-type]
    assert faces.shape == (g.height, g.width, 4)
    digest = hashlib.sha256(np.ascontiguousarray(faces).tobytes()).hexdigest()
    assert digest == FACES_SHA256[commit], digest


def test_the_faces_are_the_same_on_every_call() -> None:
    g = kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")[0]
    assert np.array_equal(kr._bake_faces(g, "review"), kr._bake_faces(g, "review"))


# --------------------------------------------------------------------------- compose: identity and purity


@needs_font
def test_compose_makes_a_new_array_each_time_and_returns_the_old_one_for_an_equal_view() -> None:
    v = view(strip="Typing", tips=(tip(2.0, 1.5),))
    g = geometry_of(v)
    base = kr.bake_base(v.lang, v.shift, g, v.commit)
    a = kr.compose(base, g, v)
    assert kr.compose(base, g, view(seq=7, strip="Typing", tips=(tip(2.0, 1.5),))) is a  # equal apart from seq
    b = kr.compose(base, g, view(strip="Typing", tips=(tip(2.5, 1.5),)))
    assert b is not a and changed(a, b).any()
    c = kr.compose(base, g, v)
    assert c is not a and np.array_equal(c, a)  # a different view in between: a new array each call


@needs_font
def test_compose_leaves_the_base_untouched_and_returns_a_read_only_image() -> None:
    v = review("hello", lit=((3, "target"),), tips=(tip(2.0, 1.5),), strip="x")
    g = geometry_of(v)
    base = kr.bake_base(v.lang, v.shift, g, v.commit)
    before = base.copy()
    out = kr.compose(base, g, v)
    assert np.array_equal(base, before)
    assert not out.flags.writeable
    assert out.shape == base.shape and out.dtype == np.uint8
    assert (out[..., :3] <= out[..., 3:4]).all()


@needs_font
def test_a_view_equal_apart_from_seq_costs_nothing_and_a_new_base_is_not_confused_with_the_old() -> None:
    v = view(strip="a")
    g = geometry_of(v)
    base = kr.bake_base("en", False, g, "direct")
    first = kr.compose(base, g, v)
    other = kr.bake_base("en", True, g, "direct")
    assert kr.compose(other, g, v) is not first


@needs_font
def test_a_base_of_the_wrong_shape_or_type_is_a_surface_error() -> None:
    v = view()
    g = geometry_of(v)
    for bad in (np.zeros((3, 3, 4), np.uint8), np.zeros((g.height, g.width, 4), np.float32), None, "x"):
        with pytest.raises(kr.KeyboardDrawError) as caught:
            kr.compose(bad, g, v)  # type: ignore[arg-type]
        assert caught.value.code == "surface"


# --------------------------------------------------------------------------- keys: lit kinds (O3)

LIT_KINDS = ("ghost", "target", "ok", "drop", "armed", "on")


@needs_font
def test_o3_every_lit_kind_paints_its_key_and_nothing_else() -> None:
    v0 = view()
    rect = key_rect(v0, "char", "g")
    index = layout_for("direct").find(char="g").index
    base = plain(v0)
    seen = []
    for kind in LIT_KINDS:
        out = render(view(lit=((index, kind),)))
        bbox = changed_bbox(out, base)
        assert bbox is not None, kind
        assert inside(bbox, rect), kind
        seen.append(region(out, rect).tobytes())
    assert len(set(seen)) == len(LIT_KINDS)  # six looks, six different keys


@needs_font
def test_o3_the_looks_have_the_designs_colours() -> None:
    v0 = view()
    index = layout_for("direct").find(char="g").index
    rect = key_rect(v0, "char", "g")
    normal = face_sample(plain(v0), rect)
    ghost = face_sample(render(view(lit=((index, "ghost"),))), rect)
    assert all(g > n for g, n in zip(ghost, normal, strict=True)) and max(ghost) < 200  # 25% white: lighter, not white
    r, g_, b = face_sample(render(view(lit=((index, "target"),))), rect)
    assert r > 200 and 140 < g_ < 215 and b < 130  # amber
    r, g_, b = face_sample(render(view(lit=((index, "ok"),))), rect)
    assert g_ > r and g_ > 150 and g_ > b - 30  # green
    r, g_, b = face_sample(render(view(lit=((index, "drop"),))), rect)
    assert r > 190 and g_ < 120 and b < 120  # red
    on = render(view(lit=((index, "on"),)))
    assert cyan_like(on, rect) > 20  # the reticle's cyan outline
    armed = render(view(lit=((index, "armed"),)))
    assert amber_like(armed, rect) > 20  # an amber outline


@needs_font
def test_o3_the_strongest_look_wins_and_a_bad_index_is_ignored() -> None:
    index = layout_for("direct").find(char="g").index
    strong = render(view(lit=((index, "drop"),)))
    assert np.array_equal(render(view(lit=((index, "ghost"), (index, "drop")))), strong)
    assert np.array_equal(render(view(lit=((index, "drop"), (index, "ghost")))), strong)
    count = layout_for("direct").count
    assert np.array_equal(render(view(lit=((count, "drop"), (-1, "ok"), (10**6, "armed")))), plain(view()))


@needs_font
def test_o3_a_lit_key_keeps_its_legend() -> None:
    v0 = view()
    index = layout_for("direct").find(char="g").index
    rect = key_rect(v0, "char", "g")
    lit = region(render(view(lit=((index, "ghost"),))), rect)
    assert ((lit[..., :3] > 170).all(axis=-1) & (lit[..., 3] > 200)).sum() > 15  # the legend's ink is still there


@needs_font
def test_o3_a_private_view_highlights_no_key_and_draws_no_echo(spy: TextSpy) -> None:
    index = layout_for("direct").find(char="g").index
    lit = tuple((i, k) for i in (index, index + 1) for k in ("ghost", "target", "ok", "drop"))
    quiet = render(view(private=True))
    spy.reset()
    noisy = render(view(private=True, lit=lit, echo="secret"))
    assert np.array_equal(noisy, quiet)
    assert "secret" not in spy.joined()
    shown = render(view(private=False, lit=lit, echo="secret"))
    assert changed(shown, plain(view())).any()  # and the same input does draw when it is not private


@needs_font
def test_o3_a_private_view_still_shows_the_guard_and_the_private_key_is_on() -> None:
    v = review("", private=True, compose=box("", guard="close", guard_taps=1, guard_need=2, guard_left=1.0))
    out = render(v)
    close = key_rect(v, "close")
    assert amber_like(out, close) > 20
    priv = key_rect(v, "private")
    assert cyan_like(out, priv) > 20  # the toggle shows that private mode is on


@needs_font
def test_o3_the_private_pulse_lights_the_whole_keyboard() -> None:
    off = render(view(private=True, pulse=False))
    on = render(view(private=True, pulse=True))
    g = geometry_of(view())
    top = min(r[1] for r in g.keys)
    bbox = changed_bbox(on, off)
    assert bbox is not None
    assert bbox[0] <= g.keys[0][0] + 4 and bbox[0] + bbox[2] >= g.width - 12  # spans the keyboard's width
    assert bbox[1] >= top - 8  # below the strip
    # a pulse without private mode is nothing
    assert np.array_equal(render(view(private=False, pulse=True)), render(view(private=False, pulse=False)))


@needs_font
def test_a_hold_dims_the_keyboard_to_under_half_and_leaves_the_strip_bright() -> None:
    plain_v = view(strip="Paused: x")
    held = view(strip="Paused: x", hold="focus")
    g = geometry_of(plain_v)
    a, b = render(plain_v), render(held)
    strip_a = int(region(a, g.strip)[..., 3].astype(int).sum())
    strip_b = int(region(b, g.strip)[..., 3].astype(int).sum())
    assert strip_b >= strip_a * 0.99  # the strip says why: it is not dimmed
    keys = (0, g.keys[0][1], g.width, g.height - g.keys[0][1])
    alpha_a = int(region(a, keys)[..., 3].astype(int).sum())
    alpha_b = int(region(b, keys)[..., 3].astype(int).sum())
    assert 0.40 < alpha_b / alpha_a < 0.50  # dimmed to 0.45
    assert (b[..., :3] <= b[..., 3:4]).all()


# --------------------------------------------------------------------------- strip, banner, counter, progress (X46)


@needs_font
def test_the_strip_text_is_drawn_in_the_strip_and_only_there() -> None:
    v = view(strip="Typing into Notepad  EN")
    out, base = render(v), plain(v)
    bbox = changed_bbox(out, base)
    assert bbox is not None and inside(bbox, geometry_of(v).strip)


@needs_font
def test_a_strip_sentence_wider_than_the_strip_ends_in_an_ellipsis_and_stays_inside(spy: TextSpy) -> None:
    v = view(strip="word " * 80)
    out = render(v)
    assert inside(changed_bbox(out, plain(v)), geometry_of(v).strip)  # type: ignore[arg-type]
    assert any(t.endswith("...") for t in spy.texts)


@needs_font
def test_x46_banner_pixels_exist_only_when_there_is_a_banner() -> None:
    g = geometry_of(view())
    strip = g.strip
    none = render(view(strip="status"))
    assert amber_like(none, strip) == 0
    warn = render(view(strip="status", banner="Hands unsteady", banner_level="warn"))
    assert amber_like(warn, strip) > 20
    assert not np.array_equal(region(warn, strip), region(none, strip))
    info = render(view(strip="status", banner="Hands unsteady", banner_level="info"))
    assert amber_like(info, strip) == 0  # grey, not amber
    assert changed(info, none).any()
    # the banner takes the upper line and the status the lower: the window height does not change
    assert none.shape == warn.shape == info.shape
    assert outside_equal(warn, none, strip)


@needs_font
def test_x46_a_banner_with_no_level_is_drawn_as_information() -> None:
    a = render(view(banner="x", banner_level=""))
    b = render(view(banner="x", banner_level="info"))
    assert np.array_equal(a, b)


@needs_font
def test_the_counter_is_right_aligned_in_the_strip_and_amber_from_one_hundred_eighty(spy: TextSpy) -> None:
    low = review("a", compose=box("a", length=179))
    high = review("a", compose=box("a", length=180))
    strip = geometry_of(low).strip
    right = (strip[0] + strip[2] - 90, strip[1], 90, strip[3])
    out_low, out_high = render(low), render(high)
    assert amber_like(out_low, right) == 0
    assert amber_like(out_high, right) > 20
    spy.reset()
    kr.clear_cache()
    render(review("", compose=box("", length=12)))
    assert f"12/{COMPOSE_MAX}" in spy.texts


@needs_font
def test_an_empty_view_draws_nothing_on_the_baked_keyboard() -> None:
    assert np.array_equal(render(view()), plain(view()))


@needs_font
def test_the_counter_is_a_review_mode_thing_in_the_right_half_of_the_strip() -> None:
    v = review("")
    strip = geometry_of(v).strip
    mask = changed(render(v), plain(v))
    mask &= (np.arange(mask.shape[0]) < strip[1] + strip[3])[:, None]  # the box's caret is below the strip
    ys, xs = np.nonzero(mask)
    assert xs.size
    assert xs.min() > strip[0] + strip[2] // 2
    assert ys.min() >= strip[1] and ys.max() < strip[1] + strip[3]


@needs_font
def test_the_progress_bar_grows_with_progress() -> None:
    counts = []
    for p in (0.0, 0.25, 0.5, 1.0):
        out = render(view(progress=p))
        counts.append(cyan_like(out, geometry_of(view()).strip))
    assert counts[0] == 0 and counts[0] < counts[1] < counts[2] < counts[3]
    out = render(view(progress=0.5))
    assert changed_bbox(out, plain(view())) is not None
    assert inside(changed_bbox(out, plain(view())), geometry_of(view()).strip)  # type: ignore[arg-type]


@needs_font
def test_a_progress_outside_zero_to_one_is_clamped() -> None:
    assert np.array_equal(render(view(progress=7.0)), render(view(progress=1.0)))
    assert np.array_equal(render(view(progress=-3.0)), render(view(progress=0.0)))
    assert np.array_equal(render(view(progress=float("nan"))), render(view(progress=0.0)))


@needs_font
def test_the_drift_marker_is_amber_and_in_the_strip() -> None:
    a, b = render(view(drift=False)), render(view(drift=True))
    bbox = changed_bbox(b, a)
    assert bbox is not None and inside(bbox, geometry_of(view()).strip)
    assert amber_like(b, geometry_of(view()).strip) > 5


@needs_font
def test_the_hold_text_is_amber() -> None:
    held = render(view(strip="Paused: your hands are out of view", hold="slow"))
    assert amber_like(held, geometry_of(view()).strip) > 20


# --------------------------------------------------------------------------- text Pillow cannot draw


@needs_font
@pytest.mark.parametrize("bad", ["\x00", "\n", "\t", "\x7f", "\u202e", "\ud800"])
@pytest.mark.parametrize("where", ["strip", "banner", "echo", "prompt", "box"])
def test_characters_pillow_cannot_draw_are_replaced_one_for_one_and_never_raise(
    where: str, bad: str, spy: TextSpy
) -> None:
    """A control character, a newline or a lone surrogate must not make the frame fail or change line breaks."""

    def frame(middle: str) -> KeyboardView:
        word = f"ab{middle}cd"
        if where == "box":
            return review(word, compose=box(word))
        if where == "echo":
            return view(echo=word)
        return view(**{where: word})

    out = render(frame(bad))
    assert all(t.isprintable() for t in spy.texts), spy.texts  # nothing unprintable reached Pillow
    kr.clear_cache()
    assert np.array_equal(out, render(frame("\ufffd")))  # drawn as the replacement mark, in place


# --------------------------------------------------------------------------- the echo row (O10)


@needs_font
def test_o10_a_hebrew_echo_is_painted_in_visual_order(spy: TextSpy) -> None:
    for echo in (HEBREW, "שלום עולם", "hello " + HEBREW, HEBREW + " hello", "ג'ק"):
        spy.reset()
        kr.clear_cache()
        render(view(echo=echo, lang="he"))
        assert bidi_display(echo) in spy.joined().replace("\ufffd", ""), echo
        assert spy.joined() != echo or bidi_display(echo) == echo


@needs_font
def test_an_english_echo_is_painted_as_typed(spy: TextSpy) -> None:
    render(view(echo="hello world"))
    assert "hello world" in spy.texts


@needs_font
def test_the_echo_is_in_the_echo_row_and_a_long_one_keeps_its_tail(spy: TextSpy) -> None:
    v = view(echo="x" * 5)
    assert inside(changed_bbox(render(v), plain(v)), geometry_of(v).echo)  # type: ignore[arg-type]
    spy.reset()
    kr.clear_cache()
    long = "a" * 80 + "tail"
    render(view(echo=long))
    shown = [t for t in spy.texts if "tail" in t]
    assert shown and shown[0].startswith("...") and shown[0].endswith("tail")


@needs_font
def test_the_prompt_is_drawn_in_the_echo_row_unless_it_is_the_strip_text(spy: TextSpy) -> None:
    v = view(prompt="the quick brown fox", strip="Practice")
    assert inside(changed_bbox(render(v), render(view(strip="Practice"))), geometry_of(v).echo)  # type: ignore[arg-type]
    spy.reset()
    kr.clear_cache()
    render(view(prompt="Tap your left index finger", strip="Tap your left index finger"))
    assert spy.texts.count("Tap your left index finger") == 1  # not twice


@needs_font
def test_the_prompt_stands_in_for_an_empty_box_in_review_mode() -> None:
    v = review("", prompt="Press the highlighted key", strip="x")
    out = render(v)
    box_rect = geometry_of(v).compose
    assert box_rect is not None
    assert changed(region(out, box_rect), region(render(review("", strip="x")), box_rect)).any()


def practice(**kw: Any) -> KeyboardView:
    """The air practice's view: the review layout with no compose box (nothing reaches a window), so the phrase and
    the typed echo have the box's panel to themselves."""
    return view(commit="review", compose=None, **kw)


@needs_font
def test_the_practice_phrase_and_echo_are_painted_in_the_box_panel(spy: TextSpy) -> None:
    v = practice(strip="Phrase 1/6", prompt="the quick brown fox", echo="the q")
    out = render(v)
    panel = geometry_of(v).compose
    assert panel is not None
    # the phrase alone moves the box panel, and so does the echo alone
    assert changed(region(out, panel), region(render(practice(strip="Phrase 1/6", echo="the q")), panel)).any()
    assert changed(region(out, panel), region(render(practice(strip="Phrase 1/6", prompt=v.prompt)), panel)).any()
    assert {"the quick brown fox", "the q"} <= set(spy.texts)
    # and nothing outside the panel and the strip changes: the keys are the keys' own business
    bare = render(practice(strip="Phrase 1/6"))
    strip = geometry_of(v).strip
    mask = changed(out, bare)
    mask[strip[1] : strip[1] + strip[3], :] = False
    ys, xs = np.nonzero(mask)
    assert inside((int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)), panel)


@needs_font
def test_a_practice_prompt_that_is_the_strip_text_is_not_painted_twice(spy: TextSpy) -> None:
    render(practice(strip="Tap your left index finger", prompt="Tap your left index finger"))
    assert spy.texts.count("Tap your left index finger") == 1


@needs_font
def test_a_private_practice_view_paints_no_echo_in_the_panel(spy: TextSpy) -> None:
    v = practice(strip="Phrase 1/6", prompt="the quick brown fox", echo="secret", private=True)
    render(v)
    assert "secret" not in spy.joined()
    assert "the quick brown fox" in spy.texts  # the phrase is the script's, not the person's


@needs_font
def test_a_practice_view_with_nothing_to_say_leaves_the_panel_as_baked() -> None:
    v = practice(strip="Phrase 1/6")
    panel = geometry_of(v).compose
    assert panel is not None
    assert not changed(region(render(v), panel), region(plain(v), panel)).any()


# --------------------------------------------------------------------------- the box (O41, O42, O44)


@needs_font
@pytest.mark.parametrize("text", ["hello world", HEBREW, "שלום עולם טוב", "hello " + HEBREW + " world", "ג'ק it's"])
def test_o41_the_box_renders_english_hebrew_and_mixed_text(text: str, spy: TextSpy) -> None:
    v = review(text, compose=box(text))
    out = render(v)
    rect = geometry_of(v).compose
    assert rect is not None
    bbox = changed_bbox(out, plain(v))
    assert bbox is not None
    box_part = changed_bbox(region(out, rect), region(plain(v), rect))
    assert box_part is not None
    assert bidi_display(text) in spy.without_counter()  # one line, painted in visual order


@needs_font
def test_o41_the_box_never_paints_outside_itself_or_wider_than_its_text_area() -> None:
    text = "the quick brown fox jumps over the lazy dog " * 5
    v = review(text[:COMPOSE_MAX], compose=box(text[:COMPOSE_MAX]))
    out, base = render(v), plain(v)
    rect = geometry_of(v).compose
    strip = geometry_of(v).strip
    assert rect is not None
    mask = changed(out, base)
    mask[strip[1] : strip[1] + strip[3], :] = False  # the counter lives in the strip
    ys, xs = np.nonzero(mask)
    assert inside((int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)), rect)
    # and not into the box's own padding on the right
    assert xs.max() < rect[0] + rect[2] - 6


@needs_font
def test_o41_two_hundred_characters_of_prose_fit_three_lines(spy: TextSpy) -> None:
    assert len(PROSE) == COMPOSE_MAX
    render(review(PROSE, compose=box(PROSE)))
    lines = {round(y) for t, _, y in spy.calls if not re.fullmatch(r"\d+/\d+", t)}
    assert len(lines) == 3
    assert not any(t.startswith("...") for t in spy.texts)


@needs_font
def test_o41_with_more_than_three_lines_the_last_three_are_shown_after_an_ellipsis(spy: TextSpy) -> None:
    words = " ".join(f"word{i:03d}" for i in range(60))  # more than three lines wide
    render(review(words, compose=box(words, length=len(words))))
    body = [(t, y) for t, _, y in spy.calls if not re.fullmatch(r"\d+/\d+", t)]
    assert len({round(y) for _, y in body}) <= 3
    first = min(body, key=lambda c: c[1])
    assert first[0].startswith("...")
    assert "word059" in "".join(t for t, _ in body)  # the end of the text is what stays
    assert "word000" not in "".join(t for t, _ in body)


@needs_font
def test_the_box_wraps_by_measured_width_not_by_a_character_count(spy: TextSpy) -> None:
    """Every painted line is no wider than the text area: wide letters break earlier than narrow ones.

    The sprite clips at the box edge, so ink alone cannot show an overflowing line; the widths are measured with the
    font the painter used, on what it handed to Pillow.
    """
    font = kr._font(kr.BOX_PX)
    for text in ("mmmm wwww " * 20, "iiii llll " * 20):
        spy.reset()
        kr.clear_cache()
        text = text[:COMPOSE_MAX]
        v = review(text, compose=box(text))
        render(v)
        rect = geometry_of(v).compose
        assert rect is not None
        inner = rect[2] - 16
        rows: dict[int, float] = {}
        for t, x, y in spy.calls:
            if re.fullmatch(r"\d+/\d+", t):
                continue
            rows[round(y)] = max(rows.get(round(y), 0.0), x + font.getlength(t))
        assert rows
        assert max(rows.values()) <= inner, (len(rows), max(rows.values()), inner)
    # and the wide text really needed the pixel measure: a narrow-letter text of the same length fits more per line
    wide = "mmmm wwww " * 20
    narrow = "iiii llll " * 20
    assert font.getlength(wide[:COMPOSE_MAX]) > 1.4 * font.getlength(narrow[:COMPOSE_MAX])


@needs_font
def test_the_box_wraps_by_pixels_so_wide_letters_never_run_out_of_the_box() -> None:
    text = ("mmmm wwww " * 12)[:COMPOSE_MAX]
    v = review(text, compose=box(text))
    out, base = render(v), plain(v)
    rect = geometry_of(v).compose
    strip = geometry_of(v).strip
    assert rect is not None
    mask = changed(out, base)
    mask[strip[1] : strip[1] + strip[3], :] = False
    _ys, xs = np.nonzero(mask)
    assert xs.max() < rect[0] + rect[2] - 4 and xs.min() >= rect[0]


@needs_font
def test_the_caret_is_drawn_at_the_logical_end_and_an_empty_box_has_only_the_caret() -> None:
    rect = geometry_of(review()).compose
    assert rect is not None
    empty = render(review(""))
    caret_only = changed_bbox(region(empty, rect), region(plain(review()), rect))
    assert caret_only is not None
    assert cyan_like(empty, rect) > 10  # a block in the reticle's cyan
    longer = changed_bbox(region(render(review("abc")), rect), region(plain(review()), rect))
    assert longer is not None and longer[0] + longer[2] > caret_only[0] + caret_only[2]  # the caret moved right


@needs_font
def test_the_caret_of_a_hebrew_line_sits_left_of_the_last_letter() -> None:
    rect = geometry_of(review()).compose
    assert rect is not None

    def columns(img: np.ndarray) -> tuple[float, np.ndarray]:
        part = region(img, rect)
        a = part[..., 3].astype(int)
        safe = np.maximum(a, 1)
        straight = np.stack([part[..., i].astype(int) * 255 // safe for i in (2, 1, 0)], axis=-1)
        caret = (a > 120) & (straight[..., 0] < 90) & (straight[..., 2] > 200)
        text = (a > 200) & (straight > 150).all(axis=-1)
        return float(np.nonzero(caret)[1].mean()), np.nonzero(text)[1]

    caret, ink = columns(render(review(HEBREW, compose=box(HEBREW))))
    assert caret < ink.min()  # left of the Hebrew word: where the next letter will go
    caret, ink = columns(render(review("abcd", compose=box("abcd"))))
    assert caret > ink.max()  # right of the English word


@needs_font
def test_o42_a_private_box_paints_bullets_and_the_count_only(spy: TextSpy) -> None:
    secret = "my secret password"
    masked = "\u2022" * len(secret)
    v_real = review(secret, private=True, compose=box(secret, length=len(secret)))
    v_masked = review(masked, private=True, compose=box(masked, length=len(secret)))
    out_real = render(v_real)
    pieces = spy.without_counter()
    assert pieces and set("".join(pieces)) <= {"\u2022"}  # not one letter of the text got to the painter
    assert "secret" not in spy.joined() and "my" not in spy.joined()
    assert np.array_equal(out_real, render(v_masked))  # even a real text paints as the bullets would
    assert f"{len(secret)}/{COMPOSE_MAX}" in spy.texts
    assert len(pieces) >= 1 and "".join(pieces).count("\u2022") == len(secret)


@needs_font
def test_o42_the_private_count_is_the_real_length_not_the_number_of_bullets(spy: TextSpy) -> None:
    render(review("\u2022\u2022\u2022", private=True, compose=box("\u2022\u2022\u2022", length=77)))
    assert f"77/{COMPOSE_MAX}" in spy.texts


@needs_font
def test_o44_the_typed_prefix_of_a_run_is_painted_at_reduced_brightness() -> None:
    """Where "hello" lands depends on the font (Segoe UI on Windows, an Arial-class font on macOS, DejaVu Sans on Linux:
    the CI runners fail a test with fixed pixel columns), so the typed letters are found as the pixels that change when
    five characters are marked sent."""
    text = "hello world"
    rect = geometry_of(review()).compose
    assert rect is not None
    full = region(render(review(text, compose=box(text, sent=0))), rect)
    half = region(render(review(text, compose=box(text, sent=5))), rect)
    panel = region(plain(review(text)), rect)  # what the box looks like with no text on it
    red = 2  # premultiplied red: the legend colour is light, the panel's is dark
    assert 225 <= full[..., red].max() <= 240
    dimmed = changed(full, half)
    assert dimmed.any()
    # How far a pixel sits above the panel is the ink's coverage times the legend colour, so for a pixel that the ink
    # covers well it is 0.45 of what it was whatever the glyph shape (the panel's own red would skew a raw ratio).
    lift_full = full[..., red].astype(float) - panel[..., red]
    lift_half = half[..., red].astype(float) - panel[..., red]
    solid = dimmed & (lift_full > 100)
    assert solid.sum() >= 20
    ratios = lift_half[solid] / lift_full[solid]
    assert ratios.min() >= 0.40 and ratios.max() <= 0.50
    last = int(np.nonzero(dimmed.any(axis=0))[0].max())  # "hello" ends here, whatever the font
    assert np.array_equal(full[:, last + 1 :], half[:, last + 1 :])  # the rest of the text is as bright as before
    assert full[:, last + 1 :][..., red].max() > 200  # and there is some: the part that was left alone is not empty


@needs_font
def test_o44_a_hebrew_prefix_dims_the_rightmost_letters() -> None:
    rect = geometry_of(review()).compose
    assert rect is not None
    full = region(render(review(HEBREW, compose=box(HEBREW, sent=0))), rect)
    part = region(render(review(HEBREW, compose=box(HEBREW, sent=2))), rect)
    red = 2
    cols = np.nonzero((full[..., red] > 150).any(axis=0))[0]
    # painted left to right as the final mem, vav, lamed, shin: the first two typed letters are the two rightmost
    mid = (int(cols.min()) + int(cols.max())) // 2
    assert part[:, mid + 2 :, red].max() < 140  # the dimmed, right half
    assert part[:, : mid - 2, red].max() > 220  # the rest is full strength


@needs_font
@pytest.mark.parametrize(
    "line",
    [
        "Typing into Notepad  EN",
        "Place your hands on the table as if on a keyboard",
        "Paused: the window lost focus",
        "Enter again to send",
        "Tap with your left index finger  (3/8)",
        "Review: Insert types this into Notepad",
    ],
)
def test_o44_a_fixed_strip_sentence_is_painted_verbatim(line: str, spy: TextSpy) -> None:
    render(view(strip=line))
    assert line in spy.texts


@needs_font
def test_the_border_of_the_box_shows_the_state_of_a_run() -> None:
    rect = geometry_of(review()).compose
    assert rect is not None
    comp = render(review("abc", compose=box("abc", state="composing")))
    ins = render(review("abc", compose=box("abc", state="inserting", sent=1)))
    abo = render(review("abc", compose=box("abc", state="aborted")))
    assert cyan_like(ins, rect) > cyan_like(comp, rect)
    assert amber_like(abo, rect) > amber_like(comp, rect)


# --------------------------------------------------------------------------- guards, Stop, Send (O43)


def guarded(guard: str, taps: int, need: int, left: float, **kw: Any) -> KeyboardView:
    return review("hello", compose=box("hello", guard=guard, guard_taps=taps, guard_need=need, guard_left=left, **kw))


@needs_font
@pytest.mark.parametrize(
    ("guard", "kind"), [("insert", "insert"), ("clear", "clear"), ("send", "enter"), ("close", "close")]
)
def test_o43_an_armed_guard_outlines_its_key_in_amber(guard: str, kind: str) -> None:
    v = guarded(guard, 1, 3, 1.0)
    rect = key_rect(v, kind)
    out, base = render(v), plain(v)
    x, y, _w, h = rect
    assert changed(region(out, rect), region(base, rect)).any()
    assert amber_like(out, (x, y, 6, h)) > 8  # the outline on the key's left edge
    untouched = key_rect(v, "lang")  # a key that is not the guard's stays as baked
    assert np.array_equal(region(out, untouched), region(base, untouched))


@needs_font
def test_o43_the_pips_count_taps_out_of_the_taps_needed() -> None:
    rect = key_rect(guarded("insert", 0, 3, 1.0), "insert")
    x, y, w, h = rect
    pips = (x + 8, y + h - 16, w - 16, 12)
    solid = [int((region(render(guarded("insert", t, 3, 1.0)), pips)[..., 3] > 250).sum()) for t in (0, 1, 2, 3)]
    assert solid[0] < solid[1] < solid[2] < solid[3]  # one more filled pip per tap
    # a different number needed is a different number of pips
    assert not np.array_equal(
        region(render(guarded("insert", 1, 3, 1.0)), rect), region(render(guarded("insert", 1, 2, 1.0)), rect)
    )


@needs_font
def test_o43_the_ring_shrinks_with_the_guard_window() -> None:
    rect = key_rect(guarded("send", 0, 3, 1.0), "enter")
    x, y, w, h = rect
    middle = (x + 6, y + 6, w - 12, h - 22)
    counts = [amber_like(render(guarded("send", 1, 3, left)), middle) for left in (1.0, 0.5, 0.0)]
    assert counts[0] > counts[1] > counts[2]
    # a lapsed window draws no ring at all
    assert counts[2] < counts[0] // 4


@needs_font
def test_o43_the_insert_key_reads_stop_during_a_run_and_insert_otherwise(spy: TextSpy) -> None:
    render(review("abc", compose=box("abc", state="composing")))
    assert "Insert" in spy.legends and "Stop" not in spy.legends
    spy.reset()
    kr.clear_cache()
    v = review("abc", compose=box("abc", state="inserting", sent=1))
    out = render(v)
    assert "Stop" in spy.legends
    assert cyan_like(out, key_rect(v, "insert")) > 20  # and it is the "on" look: the run is going


@needs_font
def test_o43_send_is_lit_only_when_it_can_send_and_dim_otherwise() -> None:
    v = review("abc")
    rect = key_rect(v, "enter")
    index = layout_for("review").find("enter").index
    on = render(review("abc", compose=box("abc", can_send=True)))
    off = render(review("abc", compose=box("abc", can_send=False)))
    full = render(review("abc", lit=((index, "ghost"),)))  # the same key at full strength, for comparison

    def legend_ink(img: np.ndarray) -> int:
        part = region(img, rect)
        return int(((part[..., :3] > 150).all(axis=-1) & (part[..., 3] > 200)).sum())

    assert cyan_like(on, rect) > 20 and cyan_like(off, rect) == 0
    assert legend_ink(off) == 0 < legend_ink(full)  # a dim legend is not the bright one
    assert legend_ink(on) > 0
    assert np.array_equal(region(off, rect), region(plain(v), rect))  # the baked Send key is the dim one


@needs_font
def test_o43_a_refused_tap_flashes_the_key_red_and_a_full_box_does_not_change_the_keys() -> None:
    v0 = review("abc")
    idx = layout_for("review").find("clear").index
    out = render(review("abc", lit=((idx, "drop"),)))
    r, g_, _b = face_sample(out, key_rect(v0, "clear"))
    assert r > 190 and g_ < 120
    full = render(review("abc", compose=box("abc", full=True, length=COMPOSE_MAX)))
    amber_counter = amber_like(full, geometry_of(v0).strip)
    assert amber_counter > 20  # the counter turns amber: that is the sign of a full box


@needs_font
def test_the_direct_layout_arms_enter_with_an_outline_and_no_pips() -> None:
    v = view(armed_enter=True, strip="Enter again to send")
    rect = key_rect(v, "enter")
    out = render(v)
    assert amber_like(out, rect) > 20
    x, y, w, h = rect
    assert amber_like(out, (x + 10, y + h - 14, w - 20, 10)) == 0  # no pips: this is not a counted guard


@needs_font
def test_shift_is_lit_while_armed_in_english_only() -> None:
    en = render(view(shift=True))
    assert amber_like(en, key_rect(view(), "shift")) > 20
    he = render(view(shift=True, lang="he"))
    assert amber_like(he, key_rect(view(lang="he"), "shift")) == 0


# --------------------------------------------------------------------------- chips (O80, O81)


def chip_rects(v: KeyboardView) -> list[RECT]:
    g = geometry_of(v)
    return [g.keys[k.index] for k in layout_for("review").keys if k.kind == "chip"]


@needs_font
def test_o80_three_chip_cells_are_drawn_from_the_chips_and_the_active_one_is_outlined() -> None:
    chips = ("alpha", "beta", "gamma")
    none = render(review("x"))
    v = review("x", compose=box("x", chips=chips, chip_active=1))
    out = render(v)
    rects = chip_rects(v)
    assert len(rects) == 3
    for rect in rects:
        assert changed(region(out, rect), region(none, rect)).any()
    cyan = [cyan_like(out, rect) for rect in rects]
    assert cyan[1] > 20 and cyan[0] == 0 and cyan[2] == 0
    # nothing outside the chip cells moved
    assert outside_equal(out, none, (rects[0][0], rects[0][1], rects[2][0] + rects[2][2] - rects[0][0], rects[0][3]))


@needs_font
def test_o80_a_chip_wider_than_its_cell_ends_in_an_ellipsis(spy: TextSpy) -> None:
    v = review("x", compose=box("x", chips=("a very long suggestion indeed",), chip_active=None))
    out = render(v)
    assert any(t.endswith("...") for t in spy.texts)
    rects = chip_rects(v)
    none = render(review("x"))
    assert inside(
        changed_bbox(
            region(out, (0, rects[0][1], out.shape[1], rects[0][3])),
            region(none, (0, rects[0][1], out.shape[1], rects[0][3])),
        ),
        (rects[0][0] - 0, 0, rects[0][2], rects[0][3]),
    )  # type: ignore[arg-type]


@needs_font
def test_o80_no_chips_draws_nothing_and_the_inert_cells_stay_dim() -> None:
    a = render(review("x", compose=box("x", chips=())))
    b = render(review("x"))
    assert np.array_equal(a, b)
    v = review("x")
    base = plain(v)
    layout = layout_for("review")
    g = geometry_of(v)
    e = region(base, g.keys[layout.find(char="e").index])[..., 3]
    chip = region(base, chip_rects(v)[0])[..., 3]
    assert 0 < chip[28, 10] < e[28, 8]


@needs_font
def test_o80_a_chip_index_out_of_range_and_more_than_three_chips_are_ignored() -> None:
    v = review("x", compose=box("x", chips=("a", "b", "c", "d", "e"), chip_active=9))
    out = render(v)
    assert out.shape == plain(v).shape
    same = render(review("x", compose=box("x", chips=("a", "b", "c"), chip_active=None)))
    assert np.array_equal(out, same)


@needs_font
def test_o80_hebrew_chips_are_painted_in_visual_order(spy: TextSpy) -> None:
    render(review("x", compose=box("x", chips=(HEBREW,))))
    assert bidi_display(HEBREW) in spy.texts


@needs_font
def test_o81_private_chips_are_bullets_at_most_fourteen_per_chip(spy: TextSpy) -> None:
    render(review("", private=True, compose=box("", chips=("secretword", "x" * 30))))
    shown = spy.without_counter()
    assert len(shown) == 2
    assert set("".join(shown)) <= {"\u2022"}  # no letter, no ellipsis: bullets only
    assert shown[0] == "\u2022" * len("secretword")  # one bullet per character
    assert 1 < len(shown[1]) <= 14  # as many as fit the cell, never more than fourteen


# --------------------------------------------------------------------------- fingertip rings and homes (X46)


def ring_ink(u: float, v: float, **kw: Any) -> int:
    """How much a ring adds to the keyboard: the summed change of every channel (a ring's halo is the same size
    whatever is drawn in it, so a count of changed pixels would not tell a full ring from an empty one)."""
    base = render(view()).astype(int)
    out = render(view(tips=(tip(u, v, **kw),))).astype(int)
    return int(np.abs(out - base).sum())


@needs_font
def test_x46_the_arming_ring_fills_clockwise_with_three_distinct_pixel_counts() -> None:
    def amber(f: float) -> int:
        return amber_like(render(view(tips=(tip(6.0, 1.5, "closing", fill=f),))))

    counts = [amber(f) for f in (0.0, 0.5, 1.0)]
    assert counts[0] < counts[1] < counts[2]
    # a fill outside 0..1 is clamped
    assert amber(3.0) == counts[2] and amber(-1.0) == counts[0]


@needs_font
def test_x46_the_fill_starts_at_twelve_oclock_and_runs_clockwise() -> None:
    u, v = 6.0, 1.5
    cx, cy = 8 + round(u * 56), geometry_of(view()).keys[0][1] + round(v * 56)

    def amber_mask(f: float) -> np.ndarray:
        img = render(view(tips=(tip(u, v, "closing", fill=f),)))
        a = img[..., 3].astype(int)
        safe = np.maximum(a, 1)
        r, g, b = (img[..., i].astype(int) * 255 // safe for i in (2, 1, 0))
        return (a > 120) & (r >= 165) & (g > 105) & (g <= 215) & (b <= 140)

    empty = amber_mask(0.0)  # the dim amber ring that is there before anything fills
    ys, xs = np.nonzero(amber_mask(0.25) & ~empty)
    assert xs.size
    assert (ys <= cy + 3).all() and (xs >= cx - 3).all()  # the first quarter is the upper right, from 12 to 3 o'clock
    ys, xs = np.nonzero(amber_mask(0.75) & ~empty)
    assert (ys > cy + 3).any() and (xs < cx - 3).any()  # three quarters have come round the bottom to the left


@needs_font
def test_x46_ring_colours_follow_the_state() -> None:
    def pixels(state: str, **kw: Any) -> np.ndarray:
        img = render(view(tips=(tip(6.0, 1.5, state, **kw),)))
        a = img[..., 3].astype(int)
        safe = np.maximum(a, 1)
        straight = np.stack([img[..., i].astype(int) * 255 // safe for i in (2, 1, 0)], axis=-1)
        return np.concatenate([straight, a[..., None]], axis=-1)[changed(img, render(view()))]

    open_ = pixels("open")
    assert ((open_[:, :3] > 215).all(axis=1) & (open_[:, 3] > 200)).sum() >= 20  # white
    green = pixels("pressed")
    assert (((green[:, 1] > green[:, 0] + 38) & (green[:, 1] > 160)) & (green[:, 3] > 200)).sum() >= 20
    closing = render(view(tips=(tip(6.0, 1.5, "closing", fill=1.0),)))
    assert amber_like(closing) >= 20
    latched = pixels("latched")
    assert not ((latched[:, :3] > 215).all(axis=1) & (latched[:, 3] > 200)).any()  # never white: grey at 0.4


@needs_font
def test_x46_a_latched_ring_is_drawn_at_forty_percent() -> None:
    open_ink = ring_ink(6.0, 1.5, state="open")
    latched_ink = ring_ink(6.0, 1.5, state="latched")
    assert 0 < latched_ink < 0.6 * open_ink


@needs_font
def test_x46_the_right_hand_ring_is_filled_and_the_left_is_an_outline() -> None:
    right = ring_ink(6.0, 1.5, state="open", side="right")
    left = ring_ink(6.0, 1.5, state="open", side="left")
    assert right > left


@needs_font
def test_x46_a_ghost_key_under_a_tip_is_lit_by_the_lit_tuple() -> None:
    index = layout_for("direct").find(char="g").index
    # u=4.5, v=1.5 is the centre of the g key
    out = render(view(tips=(tip(4.5, 1.5, "open"),), lit=((index, "ghost"),)))
    plain_ring = render(view(tips=(tip(4.5, 1.5, "open"),)))
    rect = key_rect(view(), "char", "g")
    bbox = changed_bbox(out, plain_ring)
    assert bbox is not None and inside(bbox, rect)


@needs_font
def test_x46_closing_with_a_frozen_aim_lights_the_target_key_when_the_view_says_so() -> None:
    index = layout_for("direct").find(char="g").index
    low = render(view(tips=(tip(4.5, 1.5, "closing", fill=0.4),), lit=((index, "ghost"),)))
    high = render(view(tips=(tip(4.5, 1.5, "closing", fill=0.7),), lit=((index, "target"),)))
    rect = key_rect(view(), "char", "g")
    assert amber_like(high, rect) > amber_like(low, rect) + 20


@needs_font
def test_a_private_view_draws_the_ring_without_its_fill() -> None:
    private_no = render(view(private=True, tips=(tip(6.0, 1.5, "closing", fill=0.0),)))
    private_fill = render(view(private=True, tips=(tip(6.0, 1.5, "closing", fill=1.0),)))
    assert np.array_equal(private_no, private_fill)
    assert changed(private_no, render(view(private=True))).any()  # the ring itself is still there


@needs_font
def test_a_tip_outside_the_keyboard_is_clipped_not_an_error() -> None:
    base = render(view())
    g = geometry_of(view())
    far = [(-5.0, 1.5), (30.0, 1.5), (5.0, -9.0), (5.0, 40.0), (float("nan"), 1.0), (1.0, float("inf")), (1e300, 1e300)]
    for u, v in far:
        out = render(view(tips=(tip(u, v, "open"),)))
        assert np.array_equal(out, base), (u, v)
    # partly outside: some of the ring is drawn
    edge = render(view(tips=(tip(-0.05, 1.5, "open"),)))
    bbox = changed_bbox(edge, base)
    assert bbox is not None and bbox[0] >= 0 and bbox[0] + bbox[2] <= g.width


@needs_font
def test_at_most_eight_rings_are_drawn() -> None:
    tips8 = tuple(tip(1.0 + i * 1.2, 1.5, "open", finger=i % 4) for i in range(8))
    tips9 = (*tips8, tip(10.5, 3.5, "open"))
    assert np.array_equal(render(view(tips=tips8)), render(view(tips=tips9)))
    assert not np.array_equal(render(view(tips=tips8[:7])), render(view(tips=tips8)))


@needs_font
def test_a_warmup_tip_with_its_tap_recorded_gets_a_check_dot_only_in_the_warmup_phase() -> None:
    def ink(phase: str, done: bool) -> int:
        base = render(view(phase=phase)).astype(int)
        out = render(view(phase=phase, tips=(tip(6.0, 1.5, "open", done=done),))).astype(int)
        return int(np.abs(out - base).sum())

    assert ink("warmup", True) > ink("warmup", False)
    assert ink("typing", True) == ink("typing", False)  # `done` defaults to True outside warm-up: no dots


@needs_font
def test_the_named_finger_ring_is_marked_and_the_notes_have_a_fixed_vocabulary() -> None:
    plain_ring = ring_ink(6.0, 1.5, state="open")
    assert ring_ink(6.0, 1.5, state="open", named=True) > plain_ring
    assert 0 < ring_ink(6.0, 1.5, state="open", note="weak") < plain_ring  # dotted: less ring
    glyphs = set()
    for note in ("noisy", "veto", "speed", "posture", "coherence", "hold"):
        assert ring_ink(6.0, 1.5, state="open", note=note) > plain_ring, note  # a glyph beside the ring
        glyphs.add(render(view(tips=(tip(6.0, 1.5, "open", note=note),))).tobytes())
    assert len(glyphs) == 6  # six different marks
    # anything outside the vocabulary draws nothing extra: a note is never shown as text
    assert ring_ink(6.0, 1.5, state="open", note="my typed words") == plain_ring


@needs_font
def test_a_note_never_reaches_the_painter_as_text(spy: TextSpy) -> None:
    render(view(tips=(tip(6.0, 1.5, "open", note="noisy"), tip(8.0, 1.5, "open", note="zzqxjv"))))
    assert "zzqxjv" not in spy.joined() and "noisy" not in spy.joined()


@needs_font
def test_the_placement_home_rings_are_drawn_by_hand() -> None:
    base = render(view())
    left = render(view(homes=((2.0, 1.5, "left"),)))
    right = render(view(homes=((9.0, 1.5, "right"),)))
    assert changed(left, base).any() and changed(right, base).any()
    both = render(view(homes=((2.0, 1.5, "left"), (9.0, 1.5, "right"))))
    assert changed(both, base).sum() > changed(left, base).sum()


# --------------------------------------------------------------------------- failure and privacy (S22b, F4)

SENTINEL = "zzqxjv-typed-words"
SAMPLES = 36
#: Two hundred characters of ordinary English: about nine pixels a character at 18 px, as the design's probe assumed.
PROSE = (
    "i would like to meet you at the cafe on the corner near the station at noon so we can talk about the plan for "
    "the trip and then walk to the park if it is not raining ok see you soon and thanks again for the help"
)[:200]


@needs_font
def test_s22b_a_drawing_failure_carries_a_code_and_never_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    v = review(SENTINEL, compose=box(SENTINEL))
    g = geometry_of(v)
    base = kr.bake_base(v.lang, v.shift, g, v.commit)
    kr.clear_cache()

    def hostile(self: Any, xy: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"cannot draw {text!r}")

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", hostile)
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.compose(base, g, v)
    err = caught.value
    assert err.code == "internal" and str(err) == "internal" and err.args == ("internal",)
    assert err.__cause__ is None and err.__context__ is None
    rendered = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert SENTINEL not in rendered and SENTINEL not in repr(err)


@needs_font
def test_s22b_a_font_failure_is_the_font_code(monkeypatch: pytest.MonkeyPatch) -> None:
    v = review(SENTINEL, compose=box(SENTINEL))
    g = geometry_of(v)
    base = kr.bake_base(v.lang, v.shift, g, v.commit)
    kr.clear_cache()

    def broken(px: int) -> Any:
        raise kr.KeyboardDrawError("font")

    monkeypatch.setattr(kr, "_font", broken)
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.compose(base, g, v)
    assert caught.value.code == "font"


@needs_font
def test_s22b_bake_and_geometry_fail_with_a_code_too(monkeypatch: pytest.MonkeyPatch) -> None:
    g = geometry_of(view())

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(kr, "_bake", boom)
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.bake_base("en", False, g, "direct")
    assert caught.value.code == "internal" and caught.value.__context__ is None
    monkeypatch.setattr(kr, "_geometry_for", boom)
    kr._metrics.cache_clear()
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.keyboard_geometry(WORK, 1.0, 96, "top", "review")
    assert caught.value.code == "internal" and SENTINEL not in repr(caught.value)


@needs_font
def test_nothing_is_logged_while_drawing_the_typed_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        render(review(SENTINEL, compose=box(SENTINEL), echo=SENTINEL, tips=(tip(2.0, 1.5),)))
    assert caplog.records == []


@needs_font
def test_text_with_characters_no_font_has_never_raises() -> None:
    weird = ["\ud800", "\x00\x01\x1f", "😀 a 😀", "é à ü", "٣٤ مرحبا", "日本語", "‮‏", "\n\t a", "\u2022" * 300]
    for sample in weird:
        out = render(review(sample[:COMPOSE_MAX], compose=box(sample[:COMPOSE_MAX]), echo=sample, strip=sample))
        assert out.shape[2] == 4
        render(review("x", compose=box("x", chips=(sample,))))
        render(view(echo=sample, prompt=sample, strip=sample, banner=sample, banner_level="warn"))


@needs_font
def test_clear_cache_forgets_every_sprite_made_of_typed_text() -> None:
    render(review(SENTINEL, compose=box(SENTINEL, chips=(SENTINEL,)), strip="status"))
    render(view(echo=SENTINEL))
    assert kr._BOX_MEMO.key is not None and kr._ROW_MEMO.key is not None
    assert kr._CHIP_MEMO.key is not None and kr._LAST
    kr.clear_cache()
    assert kr._BOX_MEMO.key is None and kr._ROW_MEMO.key is None and kr._CHIP_MEMO.key is None
    assert not kr._LAST
    # and the next frame is drawn from scratch without the old one
    assert render(view()).shape[2] == 4


def test_clear_cache_and_font_available_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    kr.clear_cache()
    kr.clear_cache()
    monkeypatch.setattr(kr, "_font_candidates", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    kr._font_path.cache_clear()
    try:
        assert kr.font_available() is False
    finally:
        monkeypatch.undo()
        kr._font_path.cache_clear()


@needs_font
def test_the_font_uses_the_basic_layout_so_hebrew_is_not_reordered_a_second_time() -> None:
    """Raqm, where Pillow has it, would apply the bidi algorithm again to the string ``text.py`` already ordered."""
    assert kr._font(18).layout_engine == ImageFont.Layout.BASIC


@needs_font
def test_a_geometry_of_another_layout_is_a_size_error() -> None:
    direct = geometry_of(view())
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.bake_base("en", False, direct, "review")
    assert caught.value.code == "size"
    base = kr.bake_base("en", False, direct, "direct")
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.compose(base, direct, review("x"))
    assert caught.value.code == "size"
    with pytest.raises(kr.KeyboardDrawError) as caught:
        kr.bake_base("klingon", False, direct, "direct")  # type: ignore[arg-type]
    assert caught.value.code == "internal"


# --------------------------------------------------------------------------- cost (O8, O47)


def median_ms(commit: str, n: int = 60) -> float:
    base_view = review("hello world", strip="Review") if commit == "review" else view(strip="Typing")
    g = geometry_of(base_view)
    base = kr.bake_base(base_view.lang, base_view.shift, g, base_view.commit)
    index = layout_for(base_view.commit).find(char="g").index
    samples = []
    for i in range(n):
        tips = tuple(
            tip(1.0 + (i % 7) * 0.2 + j * 1.3, 1.5, "closing" if j % 2 else "open", fill=(i % 5) / 5) for j in range(8)
        )
        v = (
            review(
                f"hello world {i}",
                compose=box(f"hello world {i}", sent=i % 5),
                strip="Review",
                tips=tips,
                lit=((index, "ghost"),),
            )
            if commit == "review"
            else view(strip="Typing", tips=tips, lit=((index, "ghost"),), echo="hello")
        )
        t0 = now()
        kr.compose(base, g, v)
        samples.append((now() - t0) * 1000.0)
    return float(np.median(samples))


@needs_font
@pytest.mark.parametrize(("commit", "ci_limit_ms"), [("direct", 30.0), ("review", 30.0)])
def test_o8_o47_compose_is_cheap(commit: str, ci_limit_ms: float) -> None:
    median = median_ms(commit)
    print(f"compose median {commit}: {median:.2f} ms")
    assert median < ci_limit_ms


@needs_font
def test_a_still_view_costs_next_to_nothing() -> None:
    v = review("hello world", strip="Review", tips=(tip(2.0, 1.5),))
    g = geometry_of(v)
    base = kr.bake_base(v.lang, v.shift, g, v.commit)
    kr.compose(base, g, v)
    t0 = now()
    for i in range(200):
        kr.compose(base, g, review("hello world", strip="Review", tips=(tip(2.0, 1.5),), seq=i))
    assert (now() - t0) / 200 * 1000 < 1.0


# --------------------------------------------------------------------------- real session views render


@needs_font
@pytest.mark.parametrize("commit", ["direct", "review"])
def test_every_view_the_session_makes_renders(commit: str) -> None:
    from jarvis_hands.keyboard.rig import KbRig

    rig = KbRig(commit=commit)  # type: ignore[arg-type]
    rig.arm()
    rig.type_text("hello")
    views = [*rig.views[:: max(1, len(rig.views) // SAMPLES)], rig.view]
    assert views
    seen = set()
    for v in views:
        assert v is not None
        g = kr.keyboard_geometry(WORK, 1.0, 96, v.dock, v.commit)[0]
        base = kr.bake_base(v.lang, v.shift, g, v.commit)
        out = kr.compose(base, g, v)
        assert out.shape == (g.height, g.width, 4)
        seen.add(v.phase)
    assert {"placing", "warmup", "typing"} & seen
