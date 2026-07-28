"""
The unified subtitle document model.

Design principles (fixing the core flaws of the v1 model):

* **Live styling** — cues keep *references* to named styles plus their own
  inline style. Nothing is baked at parse time; the style cascade is
  resolved at render/preview time, so editing a named style instantly
  changes every cue that uses it.
* **Nested spans** — cue content is a tree of :class:`SpanNode`, matching
  TTML/VTT nesting. The innermost node wins for conflicting properties.
* **Relative units** — every length is a :class:`~ttml2pgs.core.units.Dim`
  resolved only at render time (see units.py).
* **Regions are first-class** — every cue points at a region; parsers
  derive regions for formats that have none (VTT positional settings,
  SRT ``{\\anX}``).
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple

from .colors import RGBA, parse_color
from .units import Dim

# --------------------------------------------------------------------------- #
# Style
# --------------------------------------------------------------------------- #

#: Properties that inherit from parent content elements (TTML §8.4.4.2 table).
INHERITED_PROPS = {
    'color', 'font_family', 'font_size', 'font_style', 'font_weight',
    'line_height', 'text_align', 'multi_row_align', 'visibility', 'wrap',
    'shear', 'letter_spacing', 'ruby_align', 'ruby_position', 'ruby_scale',
    'text_emphasis_style', 'text_emphasis_color', 'text_emphasis_position',
    'text_combine', 'outline_color', 'outline_width', 'shadows',
    'text_orientation', 'writing_mode', 'display_align', 'opacity_mult',
    'text_decoration',
}

#: Properties that do NOT inherit (apply to the element they're set on).
NON_INHERITED_PROPS = {
    'background_color', 'opacity', 'padding', 'show_background',
    'ruby', 'origin', 'extent', 'position', 'display',
}


@dataclass
class Shadow:
    """One drop shadow (TTML tts:textShadow / CSS text-shadow term)."""
    offset_x: Dim = field(default_factory=lambda: Dim(2, 'px'))
    offset_y: Dim = field(default_factory=lambda: Dim(2, 'px'))
    blur: Dim = field(default_factory=lambda: Dim(2, 'px'))
    color: RGBA = (0, 0, 0, 255)
    alpha: float = 1.0  # extra multiplier on color alpha

    def copy(self) -> 'Shadow':
        return replace(self)


@dataclass
class Style:
    """
    A set of *specified* style properties. ``None`` everywhere means
    "not specified here" — resolution walks the cascade.

    Used for: named styles (head), inline styles, region styles, the
    document initial style and global overrides.
    """
    id: str = ""
    #: chained style references (TTML style="a b" — later wins)
    parent_ids: List[str] = field(default_factory=list)

    # -- font --------------------------------------------------------- #
    font_family: Optional[List[str]] = None
    font_size: Optional[Dim] = None
    font_style: Optional[str] = None        # normal | italic | oblique
    font_weight: Optional[str] = None       # normal | bold
    letter_spacing: Optional[Dim] = None

    # -- color / opacity ---------------------------------------------- #
    color: Optional[RGBA] = None
    background_color: Optional[RGBA] = None
    opacity: Optional[float] = None         # element opacity (region/p)
    opacity_mult: Optional[float] = None    # inherited multiplier (overrides)
    visibility: Optional[str] = None        # visible | hidden
    display: Optional[str] = None           # auto | none

    # -- text layout --------------------------------------------------- #
    text_align: Optional[str] = None        # start|center|end|left|right|justify
    multi_row_align: Optional[str] = None   # ebutts:multiRowAlign
    display_align: Optional[str] = None     # before|center|after|justify
    line_height: Optional[Dim] = None       # Dim('' unit) = multiplier; None=normal
    wrap: Optional[bool] = None             # tts:wrapOption wrap|noWrap
    writing_mode: Optional[str] = None      # lrtb|rltb|tblr|tbrl|lr|rl|tb
    text_orientation: Optional[str] = None  # mixed | upright | sideways
    padding: Optional[Tuple[Dim, Dim, Dim, Dim]] = None  # t r b l

    # -- effects ------------------------------------------------------- #
    outline_color: Optional[RGBA] = None
    outline_width: Optional[Dim] = None     # Dim(0) disables
    shadows: Optional[List[Shadow]] = None  # empty list disables
    shear: Optional[float] = None           # degrees (tts:shear/fontShear)
    text_decoration: Optional[str] = None   # none|underline|lineThrough|overline

    # -- ruby / emphasis / combine ------------------------------------- #
    ruby: Optional[str] = None              # container|base|text|baseContainer|textContainer|delimiter
    ruby_align: Optional[str] = None        # center|start|end|spaceAround|spaceBetween|withBase
    ruby_position: Optional[str] = None     # before|after|outside (over/under normalized)
    ruby_scale: Optional[float] = None      # annotation size vs base (default 0.5)
    text_combine: Optional[str] = None      # none | all (tate-chu-yoko)
    text_emphasis_style: Optional[str] = None     # e.g. 'filled dot', 'circle', 'sesame'
    text_emphasis_color: Optional[RGBA] = None
    text_emphasis_position: Optional[str] = None  # before | after (over/under)

    # -- region-ish (styles can carry these via TTML region style refs) - #
    origin: Optional[Tuple[Dim, Dim]] = None
    extent: Optional[Tuple[Dim, Dim]] = None
    position: Optional[str] = None          # raw tts:position string
    show_background: Optional[str] = None   # always | whenActive

    # ------------------------------------------------------------------ #
    def is_empty(self) -> bool:
        return not any(v is not None for k, v in self.__dict__.items()
                       if k not in ('id', 'parent_ids'))

    def set_props(self) -> Dict[str, object]:
        """All non-None properties (excluding identity fields)."""
        return {k: v for k, v in self.__dict__.items()
                if k not in ('id', 'parent_ids') and v is not None}

    def merged_over(self, base: Optional['Style']) -> 'Style':
        """Return base <- self (self's specified props win)."""
        out = copy.deepcopy(base) if base is not None else Style()
        out.id = self.id or (base.id if base else "")
        for k, v in self.set_props().items():
            setattr(out, k, copy.deepcopy(v))
        return out

    def copy(self) -> 'Style':
        return copy.deepcopy(self)


# --------------------------------------------------------------------------- #
# Computed style — every field concrete, produced by resolve_style()
# --------------------------------------------------------------------------- #

@dataclass
class ComputedStyle:
    font_family: List[str] = field(default_factory=lambda: ['sans-serif'])
    font_size: Dim = field(default_factory=lambda: Dim(4.5, 'vh'))
    font_style: str = 'normal'
    font_weight: str = 'normal'
    letter_spacing: Optional[Dim] = None
    color: RGBA = (255, 255, 255, 255)
    background_color: RGBA = (0, 0, 0, 0)
    opacity: float = 1.0
    opacity_mult: float = 1.0
    visibility: str = 'visible'
    display: str = 'auto'
    text_align: Optional[str] = None
    multi_row_align: Optional[str] = None
    display_align: Optional[str] = None
    line_height: Optional[Dim] = None
    wrap: bool = True
    writing_mode: Optional[str] = None
    text_orientation: str = 'mixed'
    padding: Optional[Tuple[Dim, Dim, Dim, Dim]] = None
    outline_color: RGBA = (0, 0, 0, 255)
    outline_width: Dim = field(default_factory=lambda: Dim(0.15, 'em'))
    shadows: List[Shadow] = field(default_factory=list)
    shear: float = 0.0
    text_decoration: str = 'none'
    ruby: Optional[str] = None
    ruby_align: str = 'center'
    ruby_position: Optional[str] = None
    ruby_scale: float = 0.5
    text_combine: Optional[str] = None
    text_emphasis_style: Optional[str] = None
    text_emphasis_color: Optional[RGBA] = None
    text_emphasis_position: Optional[str] = None
    show_background: str = 'whenActive'

    def copy(self) -> 'ComputedStyle':
        return copy.deepcopy(self)

    def key(self) -> tuple:
        """Hashable identity for run-splitting / caching."""
        return (tuple(self.font_family), str(self.font_size), self.font_style,
                self.font_weight, self.color, self.background_color,
                round(self.opacity * self.opacity_mult, 4), self.shear,
                str(self.outline_width), self.outline_color,
                tuple((str(s.offset_x), str(s.offset_y), str(s.blur), s.color, s.alpha)
                      for s in self.shadows),
                self.text_combine, self.text_emphasis_style,
                str(self.letter_spacing))


# System fallback defaults, per language, applied beneath everything.
def system_default_style(language: str = "") -> Style:
    lang = (language or "").lower()
    if lang.startswith('ja'):
        stack = ['Hiragino Sans', 'Noto Sans CJK JP', 'Noto Sans JP',
                 'Yu Gothic', 'Meiryo', 'sans-serif']
    elif lang.startswith('zh'):
        if 'hant' in lang or lang.endswith(('-tw', '-hk', '-mo')):
            stack = ['Noto Sans CJK TC', 'Microsoft JhengHei', 'PingFang TC', 'sans-serif']
        else:
            stack = ['Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif']
    elif lang.startswith('ko'):
        stack = ['Noto Sans CJK KR', 'Malgun Gothic', 'Apple SD Gothic Neo', 'sans-serif']
    else:
        stack = ['Arial', 'Helvetica', 'Roboto', 'sans-serif']
    return Style(
        id='__system__',
        font_family=stack,
        font_size=Dim(4.5, 'vh'),
        color=(255, 255, 255, 255),
        outline_color=(0, 0, 0, 255),
        outline_width=Dim(2.7, 'px'),   # authored @1080p; scales with canvas
        shadows=[Shadow(Dim(2, 'px'), Dim(2, 'px'), Dim(2, 'px'), (0, 0, 0, 255), 0.6)],
        line_height=Dim(1.25, ''),
        wrap=True,
    )


# --------------------------------------------------------------------------- #
# Content tree
# --------------------------------------------------------------------------- #

@dataclass
class SpanNode:
    """
    A node in a cue's content tree.

    kind:
      'root' – cue root (the <p>)
      'span' – styled container (TTML <span>, VTT <c>/<i>/<b>/<u>/<v>/<lang>)
      'text' – text leaf
      'br'   – line break
    Ruby roles are expressed through style.ruby on 'span' nodes
    (container/base/text/baseContainer/textContainer).
    """
    kind: str = 'span'
    text: str = ''
    style_refs: List[str] = field(default_factory=list)
    inline_style: Optional[Style] = None
    children: List['SpanNode'] = field(default_factory=list)
    #: annotations for tooling (VTT class names, voice, lang…)
    meta: Dict[str, str] = field(default_factory=dict)

    # -- helpers ------------------------------------------------------- #
    def plain_text(self) -> str:
        if self.kind == 'text':
            return self.text
        if self.kind == 'br':
            return '\n'
        out = []
        for c in self.children:
            out.append(c.plain_text())
        return ''.join(out)

    def iter_text_nodes(self) -> Iterable['SpanNode']:
        if self.kind == 'text':
            yield self
        for c in self.children:
            yield from c.iter_text_nodes()

    def copy(self) -> 'SpanNode':
        return copy.deepcopy(self)

    @staticmethod
    def text_node(text: str) -> 'SpanNode':
        return SpanNode(kind='text', text=text)

    @staticmethod
    def br() -> 'SpanNode':
        return SpanNode(kind='br')


# --------------------------------------------------------------------------- #
# Region
# --------------------------------------------------------------------------- #

@dataclass
class Region:
    """
    A layout box, anchored to the content rect.

    Position uses edge anchoring so both TTML tts:origin (left/top) and
    tts:position (arbitrary edges) map losslessly:
        x/x_edge: offset of the region's x_edge from that canvas edge
                  ('center' = region center offset from canvas center-line,
                   with 50% meaning dead center).
    """
    id: str = ""
    x: Dim = field(default_factory=lambda: Dim(50, '%'))
    x_edge: str = 'center'                  # left | right | center
    y: Dim = field(default_factory=lambda: Dim(90, '%'))
    y_edge: str = 'center'                  # top | bottom | center
    width: Optional[Dim] = field(default_factory=lambda: Dim(90, '%'))
    height: Optional[Dim] = None            # None = shrink to content
    #: styles applying to the region box + content root (displayAlign,
    #: textAlign, writingMode, backgroundColor, padding, opacity, …)
    style: Style = field(default_factory=Style)
    style_refs: List[str] = field(default_factory=list)
    #: True when this region was derived from cue settings (VTT/SRT) —
    #: such regions are re-derivable and excluded from TTML head export.
    derived: bool = False

    def is_vertical(self) -> bool:
        wm = self.style.writing_mode or ''
        return wm.startswith('tb') or wm == 'tb' or 'vertical' in wm

    def copy(self) -> 'Region':
        return copy.deepcopy(self)


def default_region() -> Region:
    """Bottom-centered 90%-wide region used when a format defines none."""
    r = Region(id='__default__',
               x=Dim(50, '%'), x_edge='center',
               y=Dim(90, '%'), y_edge='center',
               width=Dim(90, '%'), height=None)
    r.style.display_align = 'after'
    r.style.text_align = 'center'
    return r


# --------------------------------------------------------------------------- #
# Cue
# --------------------------------------------------------------------------- #

_cue_ids = itertools.count(1)


@dataclass
class Cue:
    begin_ms: float = 0.0
    end_ms: float = 0.0
    region_id: Optional[str] = None
    style_refs: List[str] = field(default_factory=list)
    inline_style: Optional[Style] = None
    root: SpanNode = field(default_factory=lambda: SpanNode(kind='root'))
    #: render toggle (UI checkbox)
    enabled: bool = True
    #: stable identity within a session (used by preview/queue caching)
    uid: int = field(default_factory=lambda: next(_cue_ids))
    #: source id (VTT cue identifier / TTML xml:id)
    source_id: str = ''
    lang: str = ''

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.begin_ms

    def plain_text(self) -> str:
        return self.root.plain_text()

    def copy(self) -> 'Cue':
        c = copy.deepcopy(self)
        c.uid = next(_cue_ids)
        return c


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

@dataclass
class SubtitleDocument:
    source_path: str = ''
    source_format: str = ''                  # ttml | vtt | srt | t2p
    language: str = ''
    #: authored pixel space (tts:extent on <tt>, else 1920x1080 assumption)
    px_width: int = 1920
    px_height: int = 1080
    cell_rows: int = 15
    cell_cols: int = 32
    #: declared frame rate of the *subtitle master*, if known
    fps: Optional[Fraction] = None
    styles: Dict[str, Style] = field(default_factory=dict)
    regions: Dict[str, Region] = field(default_factory=dict)
    #: merged <initial> values (document-level defaults under named styles)
    initial: Style = field(default_factory=lambda: Style(id='__initial__'))
    cues: List[Cue] = field(default_factory=list)
    #: free-form source metadata kept for round-tripping
    metadata: Dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def sorted_cues(self) -> List[Cue]:
        return sorted(self.cues, key=lambda c: (c.begin_ms, c.end_ms, c.uid))

    def get_region(self, cue: Cue) -> Region:
        if cue.region_id and cue.region_id in self.regions:
            return self.regions[cue.region_id]
        if '__default__' not in self.regions:
            self.regions['__default__'] = default_region()
        return self.regions['__default__']

    def ensure_region(self, region: Region) -> str:
        """Add region if new; returns its id (renaming to avoid clashes)."""
        rid = region.id or f"region{len(self.regions) + 1}"
        base = rid
        n = 2
        while rid in self.regions and self.regions[rid] is not region:
            rid = f"{base}_{n}"
            n += 1
        region.id = rid
        self.regions[rid] = region
        return rid

    def unique_style_id(self, base: str) -> str:
        sid, n = base, 2
        while sid in self.styles:
            sid = f"{base}_{n}"
            n += 1
        return sid

    def rename_style(self, old: str, new: str) -> bool:
        """Rename a named style, updating every reference to it (style
        chains, regions, cues and their span trees)."""
        if old not in self.styles or not new or new in self.styles:
            return False
        st = self.styles.pop(old)
        st.id = new
        self.styles[new] = st

        def fix(refs: List[str]) -> List[str]:
            return [new if r == old else r for r in refs]

        for s in self.styles.values():
            s.parent_ids = fix(s.parent_ids)
        for r in self.regions.values():
            r.style_refs = fix(r.style_refs)
            r.style.parent_ids = fix(r.style.parent_ids)
        self.initial.parent_ids = fix(self.initial.parent_ids)

        def walk(node: SpanNode):
            node.style_refs = fix(node.style_refs)
            for ch in node.children:
                walk(ch)

        for cue in self.cues:
            cue.style_refs = fix(cue.style_refs)
            walk(cue.root)
        return True

    def rename_region(self, old: str, new: str) -> bool:
        """Rename a region, updating every cue that references it."""
        if old not in self.regions or not new or new in self.regions:
            return False
        region = self.regions.pop(old)
        region.id = new
        self.regions[new] = region
        for cue in self.cues:
            if cue.region_id == old:
                cue.region_id = new
        return True

    def languages_used(self) -> List[str]:
        langs = {c.lang or self.language for c in self.cues}
        return sorted(l for l in langs if l)

    # ------------------------------------------------------------------ #
    # Style resolution (the cascade)
    # ------------------------------------------------------------------ #
    def _expand_refs(self, refs: List[str], _seen=None) -> List[Style]:
        """Expand style references depth-first (chained referential styling)."""
        _seen = _seen or set()
        out: List[Style] = []
        for ref in refs:
            if ref in _seen or ref not in self.styles:
                continue
            _seen.add(ref)
            st = self.styles[ref]
            out.extend(self._expand_refs(st.parent_ids, _seen))
            out.append(st)
        return out

    def specified_style(self, style_refs: List[str],
                        inline: Optional[Style]) -> Style:
        """Flatten refs + inline into a single specified Style."""
        acc = Style()
        for st in self._expand_refs(list(style_refs)):
            acc = st.merged_over(acc)
        if inline is not None:
            acc = inline.merged_over(acc)
        return acc

    def resolve_style(self, chain: List[Tuple[List[str], Optional[Style]]],
                      region: Optional[Region] = None,
                      overrides: Optional[Style] = None,
                      language: str = '') -> ComputedStyle:
        """
        Compute the final style for a content node.

        chain    – list of (style_refs, inline_style) from outermost
                   (body/p) to innermost (the node itself).
        region   – the presentation region (root of the inheritance chain
                   for inherited props, per TTML §8.4.4.2).
        overrides– global override style applied on top of everything.
        """
        # Base: system defaults <- document initial
        spec = self.initial.merged_over(system_default_style(language or self.language))
        # Region styles participate as the outermost ancestor.
        if region is not None:
            reg_spec = self.specified_style(region.style_refs, region.style)
            for k, v in reg_spec.set_props().items():
                if k in INHERITED_PROPS or k in ('text_align', 'display_align',
                                                 'writing_mode', 'line_height'):
                    setattr(spec, k, copy.deepcopy(v))
        # Walk the content chain outermost -> innermost.
        for refs, inline in chain:
            node_spec = self.specified_style(refs, inline)
            for k, v in node_spec.set_props().items():
                setattr(spec, k, copy.deepcopy(v))
        # Global overrides win over everything.
        if overrides is not None:
            for k, v in overrides.set_props().items():
                setattr(spec, k, copy.deepcopy(v))
        return _computed_from_spec(spec)


def _computed_from_spec(spec: Style) -> ComputedStyle:
    c = ComputedStyle()
    for k, v in spec.set_props().items():
        if hasattr(c, k):
            setattr(c, k, copy.deepcopy(v))
    # normalize ruby position over/under aliases
    if c.ruby_position in ('over', 'above'):
        c.ruby_position = 'before'
    elif c.ruby_position in ('under', 'below'):
        c.ruby_position = 'after'
    if c.text_emphasis_position in ('over', 'above'):
        c.text_emphasis_position = 'before'
    elif c.text_emphasis_position in ('under', 'below'):
        c.text_emphasis_position = 'after'
    return c
