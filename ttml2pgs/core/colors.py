"""
Color parsing/formatting for TTML (§8.3.2 <color>), CSS (WebVTT STYLE
blocks) and SRT <font color=...> values.

Internal representation: RGBA tuple of ints 0-255.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

RGBA = Tuple[int, int, int, int]

# TTML1 defined named colors (§8.3.2) + common CSS extras.
NAMED_COLORS = {
    'transparent': (0, 0, 0, 0),
    'black': (0, 0, 0, 255), 'silver': (192, 192, 192, 255),
    'gray': (128, 128, 128, 255), 'grey': (128, 128, 128, 255),
    'white': (255, 255, 255, 255), 'maroon': (128, 0, 0, 255),
    'red': (255, 0, 0, 255), 'purple': (128, 0, 128, 255),
    'fuchsia': (255, 0, 255, 255), 'magenta': (255, 0, 255, 255),
    'green': (0, 128, 0, 255), 'lime': (0, 255, 0, 255),
    'olive': (128, 128, 0, 255), 'yellow': (255, 255, 0, 255),
    'navy': (0, 0, 128, 255), 'blue': (0, 0, 255, 255),
    'teal': (0, 128, 128, 255), 'aqua': (0, 255, 255, 255),
    'cyan': (0, 255, 255, 255), 'orange': (255, 165, 0, 255),
    'pink': (255, 192, 203, 255), 'brown': (165, 42, 42, 255),
    'gold': (255, 215, 0, 255), 'ivory': (255, 255, 240, 255),
    'lightgray': (211, 211, 211, 255), 'lightgrey': (211, 211, 211, 255),
    'darkgray': (169, 169, 169, 255), 'darkgrey': (169, 169, 169, 255),
}

_HEX_RE = re.compile(r'^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_FUNC_RE = re.compile(r'^(rgba?)\s*\(\s*([^)]*)\)$', re.IGNORECASE)


def parse_color(text: Optional[str]) -> Optional[RGBA]:
    """Parse a color string. Returns RGBA or None if unparseable."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    low = s.lower()
    if low in NAMED_COLORS:
        return NAMED_COLORS[low]

    m = _HEX_RE.match(s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            r, g, b = (int(c * 2, 16) for c in h)
            return (r, g, b, 255)
        if len(h) == 4:
            r, g, b, a = (int(c * 2, 16) for c in h)
            return (r, g, b, a)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))

    m = _FUNC_RE.match(s)
    if m:
        parts = [p.strip() for p in m.group(2).replace('/', ',').split(',') if p.strip()]
        try:
            vals = []
            for i, p in enumerate(parts[:3]):
                if p.endswith('%'):
                    vals.append(int(round(float(p[:-1]) * 2.55)))
                else:
                    vals.append(int(round(float(p))))
            a = 255
            if len(parts) >= 4:
                af = parts[3]
                if af.endswith('%'):
                    a = int(round(float(af[:-1]) * 2.55))
                else:
                    fa = float(af)
                    a = int(round(fa * 255)) if fa <= 1.0 else int(round(fa))
            r, g, b = (max(0, min(255, v)) for v in vals)
            return (r, g, b, max(0, min(255, a)))
        except (ValueError, IndexError):
            return None
    return None


def to_hex(c: Optional[RGBA], include_alpha: bool = True) -> str:
    """RGBA -> '#rrggbb' or '#rrggbbaa' (alpha omitted when opaque)."""
    if c is None:
        return ''
    r, g, b, a = c
    if a >= 255 or not include_alpha:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"#{r:02x}{g:02x}{b:02x}{a:02x}"


def with_alpha(c: RGBA, alpha_mult: float) -> RGBA:
    """Multiply the alpha channel by *alpha_mult* (0..1)."""
    r, g, b, a = c
    return (r, g, b, max(0, min(255, int(round(a * alpha_mult)))))
