"""
Glyph rasterization and effect compositing.

Renders a :class:`~ttml2pgs.core.layout.LayoutResult` into an RGBA numpy
bitmap, directly — no browser involved. Pipeline per block:

    span backgrounds → drop shadows → outlines (stroke) → fills
    (+ underline / strikethrough / emphasis marks)

* Glyph bitmaps come from FreeType with hinting disabled (resolution
  independent) and are cached per (face, glyph, size, transform, stroke).
* Outlines use the FreeType stroker (round joins), painted *under* the
  fill across the whole block — same visual as the old CSS
  ``paint-order: stroke fill`` without a stroke overlapping neighbouring
  fills.
* Drop shadows blur the union alpha of fill+outline per shadow style.
* Synthetic bold (embolden) and synthetic italic (shear) when the family
  lacks real bold/italic faces; tts:shear as a glyph transform (skew along
  the column for vertical text).
"""

from __future__ import annotations

import ctypes
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import freetype
import numpy as np
from freetype import (FT_LOAD_DEFAULT, FT_LOAD_NO_BITMAP, FT_LOAD_NO_HINTING,
                      FT_RENDER_MODE_NORMAL, FT_STROKER_LINECAP_ROUND,
                      FT_STROKER_LINEJOIN_ROUND, Matrix, Stroker, Vector)
from PIL import Image, ImageDraw, ImageFilter

from .colors import RGBA
from .fonts import FontManager
from .layout import LayoutResult, Mark, PlacedGlyph

_F16 = 0x10000


@dataclass
class GlyphBitmap:
    alpha: np.ndarray      # HxW float32 0..1
    left: int
    top: int


class GlyphCache:
    def __init__(self, max_entries: int = 8192):
        self._cache: Dict[tuple, Optional[GlyphBitmap]] = {}
        self._max = max_entries

    def clear(self):
        self._cache.clear()

    @staticmethod
    def _key(g: PlacedGlyph, stroke_px: float) -> tuple:
        st = g.style
        return (g.face.path, g.face.index, g.gid,
                round(g.font_px * 4) / 4,
                g.synth_bold, g.synth_italic, round(st.shear_deg * 2) / 2,
                g.rot90, round(g.scale_x * 100),
                round(stroke_px * 4) / 4,
                round(st.embolden_px * 8) / 8)

    def get(self, g: PlacedGlyph, stroke_px: float = 0.0
            ) -> Optional[GlyphBitmap]:
        key = self._key(g, stroke_px)
        if key in self._cache:
            return self._cache[key]
        bmp = self._render(g, stroke_px)
        if len(self._cache) >= self._max:
            self._cache.clear()
        self._cache[key] = bmp
        return bmp

    # ------------------------------------------------------------------ #
    def _render(self, g: PlacedGlyph, stroke_px: float
                ) -> Optional[GlyphBitmap]:
        fm = FontManager.instance()
        face = fm.ft_face(g.face.path, g.face.index)
        if face is None:
            return None
        try:
            face.set_char_size(max(1, int(round(g.font_px * 64))))
        except freetype.FT_Exception:
            return None

        # ---- build transform: rot90 ∘ shear ∘ scale_x (FT is y-up) ---- #
        sx = g.scale_x
        shear = g.style.shear_deg
        if g.synth_italic and shear == 0:
            shear = 12.0
        t = math.tan(math.radians(max(-80.0, min(80.0, shear))))
        # base: scale then shear. Horizontal glyphs shear along x (italic);
        # the same matrix pre-rotation gives the correct vertical slant for
        # rotated runs. Upright vertical glyphs shear along y instead.
        if g.rot90:
            # F_rot(cw, y-up) = [[0,1],[-1,0]] composed onto
            # F_shear@F_scale = [[sx, t],[0,1]]  →  [[0,1],[-sx,-t]]
            xx, xy, yx, yy = 0.0, 1.0, -sx, -t
        else:
            # Upright glyphs in vertical flow slant along the inline (y)
            # axis instead (classic vertical italics).
            if g.style.shear_axis == 'y':
                xx, xy, yx, yy = sx, 0.0, -t, 1.0
            else:
                xx, xy, yx, yy = sx, t, 0.0, 1.0

        mat = Matrix(int(xx * _F16), int(xy * _F16),
                     int(yx * _F16), int(yy * _F16))
        face.set_transform(mat, Vector(0, 0))
        try:
            face.load_glyph(g.gid, FT_LOAD_DEFAULT | FT_LOAD_NO_BITMAP |
                            FT_LOAD_NO_HINTING)
        except freetype.FT_Exception:
            face.set_transform(Matrix(_F16, 0, 0, _F16), Vector(0, 0))
            return None

        # stem darkening (Chrome-weight match) + synthetic bold on top
        strength = int(g.style.embolden_px * 64)
        if g.synth_bold:
            strength += int(g.font_px * 0.028 * 64)
        if strength > 0:
            freetype.FT_Outline_Embolden(
                ctypes.byref(face.glyph.outline._FT_Outline), strength)

        try:
            glyph = face.glyph.get_glyph()
            if stroke_px > 0:
                stroker = Stroker()
                stroker.set(int(round(stroke_px * 64)),
                            FT_STROKER_LINECAP_ROUND,
                            FT_STROKER_LINEJOIN_ROUND, 0)
                glyph.stroke(stroker, True)
            blyph = glyph.to_bitmap(FT_RENDER_MODE_NORMAL, Vector(0, 0), True)
        except freetype.FT_Exception:
            face.set_transform(Matrix(_F16, 0, 0, _F16), Vector(0, 0))
            return None
        finally:
            face.set_transform(Matrix(_F16, 0, 0, _F16), Vector(0, 0))

        bmp = blyph.bitmap
        w, h = bmp.width, bmp.rows
        if w == 0 or h == 0:
            return GlyphBitmap(np.zeros((0, 0), np.float32), blyph.left, blyph.top)
        buf = np.array(bmp.buffer, dtype=np.uint8)
        if bmp.pitch != w:
            buf = buf.reshape(h, abs(bmp.pitch))[:, :w]
        else:
            buf = buf.reshape(h, w)
        return GlyphBitmap(buf.astype(np.float32) / 255.0, blyph.left, blyph.top)


_glyph_cache = GlyphCache()


def clear_caches():
    _glyph_cache.clear()


# --------------------------------------------------------------------------- #
# Layer compositing helpers (premultiplied float32)
# --------------------------------------------------------------------------- #

def _blit_mask(layer: np.ndarray, mask: np.ndarray, x: int, y: int,
               color: RGBA, opacity: float = 1.0):
    """Composite mask*color over the premultiplied float32 layer."""
    H, W = layer.shape[:2]
    h, w = mask.shape
    if h == 0 or w == 0:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
    a = (sub * (color[3] / 255.0) * opacity)[..., None]
    if a.max() <= 0:
        return
    rgb = np.array([color[0], color[1], color[2]], np.float32) / 255.0
    src = np.empty((y1 - y0, x1 - x0, 4), np.float32)
    src[..., :3] = rgb * a
    src[..., 3:] = a
    dst = layer[y0:y1, x0:x1]
    dst *= (1.0 - a)
    dst += src


def _blit_max(layer: np.ndarray, mask: np.ndarray, x: int, y: int,
              scale: float = 1.0):
    """Accumulate max-alpha into a single-channel float layer."""
    H, W = layer.shape[:2]
    h, w = mask.shape
    if h == 0 or w == 0:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    sub = mask[y0 - y:y1 - y, x0 - x:x1 - x] * scale
    np.maximum(layer[y0:y1, x0:x1], sub, out=layer[y0:y1, x0:x1])


def _over(dst: np.ndarray, src: np.ndarray):
    a = src[..., 3:4]
    dst *= (1.0 - a)
    dst += src


def _fill_rect(layer: np.ndarray, x: float, y: float, w: float, h: float,
               color: RGBA, opacity: float = 1.0):
    x0, y0 = int(round(x)), int(round(y))
    x1, y1 = int(round(x + w)), int(round(y + h))
    H, W = layer.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x0 >= x1 or y0 >= y1:
        return
    a = color[3] / 255.0 * opacity
    rgb = np.array(color[:3], np.float32) / 255.0 * a
    src = np.empty((y1 - y0, x1 - x0, 4), np.float32)
    src[..., :3] = rgb
    src[..., 3] = a
    dst = layer[y0:y1, x0:x1]
    dst *= (1.0 - a)
    dst += src


def _mark_mask(mark: Mark) -> Tuple[np.ndarray, int, int]:
    """Render an emphasis mark (dot/circle/sesame) to an alpha tile."""
    ss = 4
    r = max(1.0, mark.radius)
    size = int(math.ceil(r * 2)) + 4
    img = Image.new('L', (size * ss, size * ss), 0)
    d = ImageDraw.Draw(img)
    cx = cy = size * ss / 2
    rr = r * ss
    if mark.sesame:
        # sesame: slanted teardrop-ish ellipse
        bbox = [cx - rr, cy - rr * 0.62, cx + rr, cy + rr * 0.62]
        if mark.filled:
            d.ellipse(bbox, fill=255)
        else:
            d.ellipse(bbox, outline=255, width=max(1, int(rr * 0.35)))
        img = img.rotate(-45, resample=Image.BICUBIC, center=(cx, cy))
    else:
        bbox = [cx - rr, cy - rr, cx + rr, cy + rr]
        if mark.filled:
            d.ellipse(bbox, fill=255)
        else:
            d.ellipse(bbox, outline=255, width=max(1, int(rr * 0.35)))
    img = img.resize((size, size), Image.LANCZOS)
    mask = np.asarray(img, np.float32) / 255.0
    return mask, int(round(mark.cx - size / 2)), int(round(mark.cy - size / 2))


# --------------------------------------------------------------------------- #
# Block renderer
# --------------------------------------------------------------------------- #

@dataclass
class RenderedBlock:
    bitmap: np.ndarray        # HxWx4 uint8 straight-alpha RGBA
    origin_x: int             # position of block (0,0) inside bitmap
    origin_y: int


def render_layout(result: LayoutResult, extra_opacity: float = 1.0
                  ) -> RenderedBlock:
    """Rasterize a laid-out block. Returns bitmap + block-origin offset."""
    # ---- canvas size: block + generous padding for effect overhang ---- #
    max_font = max((g.font_px for g in result.glyphs), default=24.0)
    max_stroke = 0.0
    max_reach_x = max_reach_y = 0.0
    for g in result.glyphs:
        st = g.style
        max_stroke = max(max_stroke, st.outline_px)
        for sh in st.shadows:
            max_reach_x = max(max_reach_x, abs(sh.dx) + sh.blur * 2)
            max_reach_y = max(max_reach_y, abs(sh.dy) + sh.blur * 2)
    pad = int(math.ceil(max(max_reach_x, max_reach_y) + max_stroke
                        + max_font * 0.75 + 4))
    W = int(math.ceil(result.width)) + pad * 2
    H = int(math.ceil(result.height)) + pad * 2
    W, H = max(W, 2), max(H, 2)

    bg = np.zeros((H, W, 4), np.float32)
    outline = np.zeros((H, W, 4), np.float32)
    fill = np.zeros((H, W, 4), np.float32)

    # shadow groups: signature -> (mask, shadows, opacity)
    shadow_groups: Dict[tuple, dict] = {}

    # ---- span backgrounds -------------------------------------------- #
    for rect in result.rects:
        if rect.kind == 'background':
            _fill_rect(bg, rect.x + pad, rect.y + pad, rect.w, rect.h,
                       rect.color)

    # ---- glyphs -------------------------------------------------------- #
    for g in result.glyphs:
        st = g.style
        op = st.opacity
        x = int(round(g.x)) + pad
        y = int(round(g.y)) + pad

        stroke = st.outline_px
        fill_bmp = _glyph_cache.get(g, 0.0)
        if fill_bmp is None:
            continue
        stroke_bmp = _glyph_cache.get(g, stroke) if stroke > 0.05 else None

        # shadow mask accumulation (union of stroke+fill silhouette)
        if st.shadows:
            sig = (tuple((round(s.dx, 2), round(s.dy, 2), round(s.blur, 2),
                          s.color) for s in st.shadows), round(op, 3))
            grp = shadow_groups.get(sig)
            if grp is None:
                grp = {'mask': np.zeros((H, W), np.float32),
                       'shadows': st.shadows, 'opacity': op}
                shadow_groups[sig] = grp
            src = stroke_bmp if stroke_bmp is not None else fill_bmp
            _blit_max(grp['mask'], src.alpha, x + src.left, y - src.top)
            if stroke_bmp is not None:
                _blit_max(grp['mask'], fill_bmp.alpha,
                          x + fill_bmp.left, y - fill_bmp.top)

        if stroke_bmp is not None:
            _blit_mask(outline, stroke_bmp.alpha, x + stroke_bmp.left,
                       y - stroke_bmp.top, st.outline_color, op)
        _blit_mask(fill, fill_bmp.alpha, x + fill_bmp.left,
                   y - fill_bmp.top, st.color, op)

    # ---- decorations (underline etc.) --------------------------------- #
    for rect in result.rects:
        if rect.kind in ('underline', 'strikethrough'):
            _fill_rect(fill, rect.x + pad, rect.y + pad, rect.w, rect.h,
                       rect.color)

    # ---- emphasis marks ------------------------------------------------ #
    for mark in result.marks:
        mask, mx, my = _mark_mask(mark)
        op = mark.style.opacity if mark.style else 1.0
        # marks get the outline treatment too (visibility over video)
        st = mark.style
        if st is not None and st.outline_px > 0.05:
            ow = max(1, int(round(st.outline_px * 0.85)))
            big = _dilate(mask, ow)
            _blit_mask(outline, big, mx + pad - ow, my + pad - ow,
                       st.outline_color, op)
        _blit_mask(fill, mask, mx + pad, my + pad, mark.color, op)
        if st is not None and st.shadows:
            sig = (tuple((round(s.dx, 2), round(s.dy, 2), round(s.blur, 2),
                          s.color) for s in st.shadows), round(op, 3))
            grp = shadow_groups.get(sig)
            if grp is None:
                grp = {'mask': np.zeros((H, W), np.float32),
                       'shadows': st.shadows, 'opacity': op}
                shadow_groups[sig] = grp
            _blit_max(grp['mask'], mask, mx + pad, my + pad)

    # ---- compose: bg, shadows, outline, fill --------------------------- #
    canvas = bg
    for grp in shadow_groups.values():
        for sh in grp['shadows']:
            mask = grp['mask']
            img = Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8))
            if sh.blur > 0.1:
                img = img.filter(ImageFilter.GaussianBlur(sh.blur / 2.0))
            m = np.asarray(img, np.float32) / 255.0
            dx, dy = int(round(sh.dx)), int(round(sh.dy))
            layer = np.zeros((H, W, 4), np.float32)
            _blit_mask(layer, m, dx, dy, sh.color, grp['opacity'])
            _over(canvas, layer)
    _over(canvas, outline)
    _over(canvas, fill)

    # ---- unpremultiply + crop ------------------------------------------ #
    if extra_opacity < 1.0:
        canvas *= extra_opacity
    a = canvas[..., 3]
    ys, xs = np.nonzero(a > 1.5 / 255.0)
    if len(xs) == 0:
        return RenderedBlock(np.zeros((1, 1, 4), np.uint8), pad, pad)
    x0, x1 = xs.min(), xs.max() + 1
    y0, y1 = ys.min(), ys.max() + 1
    cropped = canvas[y0:y1, x0:x1]
    out = np.zeros(cropped.shape, np.float32)
    ca = cropped[..., 3:4]
    out[..., 3:4] = ca
    with np.errstate(divide='ignore', invalid='ignore'):
        rgb = np.where(ca > 0, cropped[..., :3] / np.maximum(ca, 1e-6), 0)
    out[..., :3] = rgb
    out8 = (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)
    return RenderedBlock(out8, pad - x0, pad - y0)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Cheap square dilation used for emphasis-mark outlines."""
    img = Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8))
    img = img.filter(ImageFilter.MaxFilter(2 * r + 1))
    out = np.zeros((mask.shape[0] + 2 * r, mask.shape[1] + 2 * r), np.float32)
    out[r:-r or None, r:-r or None] = np.asarray(img, np.float32) / 255.0
    return out
