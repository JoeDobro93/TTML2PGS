"""
Relative length units.

Every visual parameter in the document model is stored as a :class:`Dim`
(value + unit) and resolved to pixels only at render time, against a
:class:`UnitContext` describing the target canvas.  This is what makes
every parameter scale with the output window:

* ``%``    – percentage of the reference box (context dependent: canvas
             axis for positions/extents, parent font size for fontSize,
             font size for lineHeight).
* ``px``   – *authored* pixels.  Interpreted relative to the document's
             declared pixel space (``tts:extent`` on ``<tt>``, else
             1920x1080) and rescaled to the target canvas, so a 3px
             outline authored for 1080p becomes 6px on a 4K render.
* ``em``   – multiple of the current font size.
* ``c``    – TTML cell unit (canvas height / cellResolution rows).
* ``vh/vw``– percent of canvas height/width (also ``rh``/``rw`` root
             variants used by some authoring tools).
* ``''``   – unitless scalar (line-height multiplier).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_UNIT_RE = re.compile(r'^\s*([+-]?[0-9]*\.?[0-9]+)\s*([a-z%]*)\s*$', re.IGNORECASE)

# Units understood by the resolver.
VALID_UNITS = {'%', 'px', 'em', 'c', 'vh', 'vw', 'rh', 'rw', ''}


@dataclass(frozen=True)
class Dim:
    """A dimension: numeric value + unit string."""
    value: float
    unit: str = 'px'

    def __str__(self) -> str:
        if self.unit == '':
            return f"{self.value:g}"
        return f"{self.value:g}{self.unit}"

    @staticmethod
    def parse(text: str, default_unit: str = 'px') -> Optional['Dim']:
        """Parse ``"80%"``, ``"12.5px"``, ``"1.2"`` … Returns None on failure."""
        if text is None:
            return None
        m = _UNIT_RE.match(str(text))
        if not m:
            return None
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit == '':
            unit = default_unit
        if unit not in VALID_UNITS:
            # Unknown unit (e.g. 'pt'): approximate pt -> px (96/72), else px.
            if unit == 'pt':
                return Dim(val * 96.0 / 72.0, 'px')
            return Dim(val, 'px')
        return Dim(val, unit)


def parse_dim_pair(text: str, default_unit: str = 'px'):
    """Parse a TTML coordinate pair like ``"10% 80%"`` -> (Dim, Dim) or None."""
    if not text:
        return None
    parts = str(text).split()
    if len(parts) != 2:
        return None
    a = Dim.parse(parts[0], default_unit)
    b = Dim.parse(parts[1], default_unit)
    if a is None or b is None:
        return None
    return a, b


@dataclass
class UnitContext:
    """
    Everything needed to turn a :class:`Dim` into device pixels.

    canvas_w/h    – the pixel box that regions resolve against (the content
                    rect: full output canvas, or the letterboxed area when
                    an aspect-ratio override is active).
    doc_w/h       – the document's authored pixel space (tts:extent) used
                    to rescale authored ``px`` values.
    cell_rows/cols– ttp:cellResolution (default 15 rows x 32 cols).
    font_size_px  – current font size (for em / lineHeight %).
    parent_font_px– parent font size (for fontSize %).
    """
    canvas_w: float = 1920.0
    canvas_h: float = 1080.0
    doc_w: float = 1920.0
    doc_h: float = 1080.0
    cell_rows: int = 15
    cell_cols: int = 32
    font_size_px: float = 48.0
    parent_font_px: float = 48.0

    # ------------------------------------------------------------------ #
    def px_scale(self, axis: str = 'y') -> float:
        """Scale factor from authored px to canvas px."""
        if axis == 'x':
            return self.canvas_w / self.doc_w if self.doc_w else 1.0
        return self.canvas_h / self.doc_h if self.doc_h else 1.0

    def cell_h(self) -> float:
        return self.canvas_h / max(1, self.cell_rows)

    def cell_w(self) -> float:
        return self.canvas_w / max(1, self.cell_cols)

    # ------------------------------------------------------------------ #
    def resolve(self, dim: Optional[Dim], axis: str = 'y',
                relative_to: Optional[float] = None,
                percent_of: str = 'canvas') -> Optional[float]:
        """
        Resolve *dim* to pixels.

        axis        – 'x' or 'y'; picks the canvas dimension for %/vw/vh
                      and the px rescale factor.
        relative_to – explicit base for '%' (overrides percent_of).
        percent_of  – 'canvas' | 'font' | 'parent-font': what '%' means.
        """
        if dim is None:
            return None
        v, u = dim.value, dim.unit
        if u == 'px':
            return v * self.px_scale(axis)
        if u == 'em':
            return v * self.font_size_px
        if u == 'c':
            return v * (self.cell_w() if axis == 'x' else self.cell_h())
        if u in ('vh', 'rh'):
            return v / 100.0 * self.canvas_h
        if u in ('vw', 'rw'):
            return v / 100.0 * self.canvas_w
        if u == '%':
            if relative_to is not None:
                return v / 100.0 * relative_to
            if percent_of == 'font':
                return v / 100.0 * self.font_size_px
            if percent_of == 'parent-font':
                return v / 100.0 * self.parent_font_px
            return v / 100.0 * (self.canvas_w if axis == 'x' else self.canvas_h)
        if u == '':
            # unitless: multiplier of font size (line-height semantics)
            return v * self.font_size_px
        return v

    def resolve_font_size(self, dim: Optional[Dim]) -> Optional[float]:
        """Font size resolution ('%' and 'em' are relative to parent font)."""
        if dim is None:
            return None
        if dim.unit == '%':
            return dim.value / 100.0 * self.parent_font_px
        if dim.unit == 'em':
            return dim.value * self.parent_font_px
        if dim.unit == 'c':
            return dim.value * self.cell_h()
        return self.resolve(dim, axis='y')
