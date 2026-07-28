"""
Global overrides — per-language style forcing + render-target layout
options.

The user can force font size / family / color / outline / shadow / alpha
per *language* (so Japanese can run a bigger font than English in the
same batch), plus target-level layout options (canvas policy, content
aspect ratio, safe-area padding).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from .colors import RGBA, parse_color, to_hex
from .model import Shadow, Style
from .units import Dim


@dataclass
class StyleOverrides:
    """Per-language forced styling. Each group has an enable flag."""
    override_font_size: bool = False
    font_size: Dim = field(default_factory=lambda: Dim(4.5, 'vh'))

    override_font_family: bool = False
    font_family: List[str] = field(default_factory=lambda: ['sans-serif'])

    override_color: bool = False
    color: RGBA = (255, 255, 255, 255)

    override_outline: bool = False
    outline_enabled: bool = True
    outline_color: RGBA = (0, 0, 0, 255)
    outline_width: Dim = field(default_factory=lambda: Dim(2.7, 'px'))

    override_shadow: bool = False
    shadow_enabled: bool = True
    shadow_color: RGBA = (0, 0, 0, 255)
    shadow_alpha: float = 0.6
    shadow_offset_x: Dim = field(default_factory=lambda: Dim(2, 'px'))
    shadow_offset_y: Dim = field(default_factory=lambda: Dim(2, 'px'))
    shadow_blur: Dim = field(default_factory=lambda: Dim(2, 'px'))

    #: global alpha multiplier (1.0 = opaque). Applied on top of styles.
    opacity_mult: float = 1.0

    override_line_height: bool = False
    line_height: Dim = field(default_factory=lambda: Dim(1.25, ''))

    #: Auto-color: pick text color/alpha from the *target video's* dynamic
    #: range, so batches mixing HDR and SDR episodes each get suitable
    #: levels (pure white is blinding in HDR; HDR grey is dim in SDR).
    auto_color: bool = False
    auto_sdr_color: RGBA = (229, 229, 229, 255)     # SDR White
    auto_sdr_alpha: float = 0.90
    auto_hdr_color: RGBA = (161, 161, 161, 255)     # HDR Grey
    auto_hdr_alpha: float = 0.90

    # ------------------------------------------------------------------ #
    def to_style(self, is_hdr: Optional[bool] = None) -> Style:
        """
        Convert enabled overrides into a Style applied over the cascade.
        is_hdr — the target video's dynamic range (None = unknown → SDR),
        consumed by auto-color.
        """
        st = Style(id='__overrides__')
        if self.override_font_size:
            st.font_size = self.font_size
        if self.override_font_family:
            st.font_family = list(self.font_family)
        if self.auto_color:
            st.color = self.auto_hdr_color if is_hdr else self.auto_sdr_color
        elif self.override_color:
            st.color = self.color
        if self.override_outline:
            st.outline_width = (self.outline_width if self.outline_enabled
                                else Dim(0, 'px'))
            st.outline_color = self.outline_color
        if self.override_shadow:
            if self.shadow_enabled:
                st.shadows = [Shadow(self.shadow_offset_x,
                                     self.shadow_offset_y,
                                     self.shadow_blur, self.shadow_color,
                                     self.shadow_alpha)]
            else:
                st.shadows = []
        if self.override_line_height:
            st.line_height = self.line_height
        alpha = self.opacity_mult
        if self.auto_color:
            alpha *= self.auto_hdr_alpha if is_hdr else self.auto_sdr_alpha
        if alpha != 1.0:
            st.opacity_mult = alpha
        return st

    # -- (de)serialization --------------------------------------------- #
    def to_dict(self) -> dict:
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, Dim):
                d[k] = str(v)
            elif isinstance(v, tuple):
                d[k] = to_hex(v)
            else:
                d[k] = v
        return d

    @staticmethod
    def from_dict(d: dict) -> 'StyleOverrides':
        so = StyleOverrides()
        for k, v in (d or {}).items():
            if not hasattr(so, k):
                continue
            cur = getattr(so, k)
            if isinstance(cur, Dim):
                dim = Dim.parse(str(v), default_unit=cur.unit)
                if dim:
                    setattr(so, k, dim)
            elif isinstance(cur, tuple):
                c = parse_color(v)
                if c:
                    setattr(so, k, c)
            elif isinstance(cur, list):
                setattr(so, k, list(v))
            else:
                setattr(so, k, v)
        return so


@dataclass
class LayoutOptions:
    """Render-target level options (not per-language)."""
    #: output canvas policy
    use_video_dims: bool = False       # canvas = video resolution
    scale_to_hd: bool = True           # ...scaled to fit 1920x1080
    #: content-area policy inside the canvas
    force_16_9: bool = False           # ignore video AR: content = canvas
    override_ar: bool = False          # use ar_w:ar_h letterboxed content
    ar_w: float = 1920.0
    ar_h: float = 800.0
    #: safe-area padding, total percent per axis (split between edges)
    use_padding: bool = False
    padding_v: float = 0.0
    padding_h: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> 'LayoutOptions':
        lo = LayoutOptions()
        for k, v in (d or {}).items():
            if hasattr(lo, k):
                setattr(lo, k, v)
        return lo


class OverrideSet:
    """
    The full override state: layout options + per-language style
    overrides. Language key '' (empty) = the Default set used for any
    language without its own tab.
    """

    def __init__(self):
        self.layout = LayoutOptions()
        self.by_lang: Dict[str, StyleOverrides] = {'': StyleOverrides()}

    def for_language(self, lang: str) -> StyleOverrides:
        lang = (lang or '').strip()
        if lang in self.by_lang:
            return self.by_lang[lang]
        base = lang.split('-')[0]
        if base in self.by_lang:
            return self.by_lang[base]
        return self.by_lang['']

    def ensure_language(self, lang: str) -> StyleOverrides:
        lang = (lang or '').strip()
        if lang and lang not in self.by_lang:
            import copy
            self.by_lang[lang] = copy.deepcopy(self.by_lang[''])
        return self.by_lang.get(lang, self.by_lang[''])

    # -- (de)serialization --------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            'layout': self.layout.to_dict(),
            'languages': {k: v.to_dict() for k, v in self.by_lang.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> 'OverrideSet':
        os_ = OverrideSet()
        if not d:
            return os_
        os_.layout = LayoutOptions.from_dict(d.get('layout', {}))
        langs = d.get('languages', {})
        if langs:
            os_.by_lang = {k: StyleOverrides.from_dict(v)
                           for k, v in langs.items()}
            os_.by_lang.setdefault('', StyleOverrides())
        return os_
