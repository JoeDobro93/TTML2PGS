"""
HarfBuzz shaping wrapper.

Shapes text at font-unit scale and returns pixel-scaled glyph positions.
Vertical upright text is shaped with direction=ttb so fonts' ``vert``/
``vrt2`` features apply (proper rotated punctuation, brackets, small kana
placement etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import uharfbuzz as hb

from .fonts import FaceRecord, FontManager


@dataclass
class ShapedGlyph:
    gid: int
    cluster: int
    x_adv: float
    y_adv: float
    x_off: float
    y_off: float


@dataclass
class FaceMetrics:
    ascent: float       # px above baseline (positive)
    descent: float      # px below baseline (positive)
    line_gap: float
    upem: int


_metrics_cache: dict = {}


def face_metrics(rec: FaceRecord, size_px: float) -> FaceMetrics:
    key = (rec.path, rec.index, round(size_px, 2))
    m = _metrics_cache.get(key)
    if m is not None:
        return m
    fm = FontManager.instance()
    font = fm.hb_font(rec.path, rec.index)
    if font is None:
        m = FaceMetrics(size_px * 0.8, size_px * 0.2, 0.0, 1000)
        _metrics_cache[key] = m
        return m
    upem = font.face.upem or 1000
    scale = size_px / upem
    ext = font.get_font_extents('ltr')
    asc = ext.ascender * scale
    desc = -ext.descender * scale
    gap = ext.line_gap * scale
    if asc <= 0:
        asc, desc = size_px * 0.8, size_px * 0.2
    m = FaceMetrics(asc, desc, gap, upem)
    _metrics_cache[key] = m
    return m


def shape_run(text: str, rec: FaceRecord, size_px: float,
              vertical: bool = False, language: str = '',
              letter_spacing_px: float = 0.0,
              features: Optional[dict] = None) -> Tuple[List[ShapedGlyph], float]:
    """
    Shape *text* with the given face.

    Returns (glyphs, total_main_axis_advance_px). For vertical, advances
    are positive distances *downward*; offsets keep HB's coordinate sense
    (y up) and are converted by the rasterizer.
    """
    fm = FontManager.instance()
    font = fm.hb_font(rec.path, rec.index)
    if font is None:
        return [], 0.0
    upem = font.face.upem or 1000
    scale = size_px / upem

    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = 'ttb' if vertical else 'ltr'
    if language:
        try:
            buf.language = language.split('-')[0]
        except Exception:
            pass
    buf.guess_segment_properties()
    if vertical:
        buf.direction = 'ttb'

    hb.shape(font, buf, features or {})

    glyphs: List[ShapedGlyph] = []
    total = 0.0
    infos = buf.glyph_infos
    poss = buf.glyph_positions
    for info, pos in zip(infos, poss):
        if vertical:
            adv = -pos.y_advance * scale       # ttb advances are negative
        else:
            adv = pos.x_advance * scale
        adv += letter_spacing_px
        glyphs.append(ShapedGlyph(
            gid=info.codepoint, cluster=info.cluster,
            x_adv=(adv if not vertical else 0.0),
            y_adv=(adv if vertical else 0.0),
            x_off=pos.x_offset * scale,
            y_off=pos.y_offset * scale))
        total += adv
    return glyphs, total
