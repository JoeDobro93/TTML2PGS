"""
Cue renderer: document model + overrides + canvas → positioned bitmap.

Responsibilities:

* Canvas / content-rect computation (video dims, force 16:9, AR override
  letterboxing, safe-area padding). Regions resolve against the *content*
  rect; rendering may legitimately spill into the letterbox bars.
* Region geometry resolution (edge/center/point anchoring, shrink-wrap
  regions, overflow-visible).
* Span-tree flattening with stepwise font-size resolution (percentage
  font sizes chain through ancestors like CSS), ruby/TCY extraction, and
  per-node style resolution — including the per-language global
  overrides.
* Final rasterization via the layout engine + rasterizer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .fonts import FontManager
from .layout import (BreakItem, InlineItem, LayoutEngine, LayoutResult,
                     ResolvedShadow, RubyItem, RunStyle, TCYItem, TextItem)
from .model import (ComputedStyle, Cue, Region, Style, SubtitleDocument,
                    default_region)
from .overrides import OverrideSet, StyleOverrides
from .raster import RenderedBlock, render_layout
from .units import Dim, UnitContext

#: Base font size when neither the document, region nor cue specifies one.
#: v1 set the HTML body to 4.5vh and every ``em``/``%`` size multiplied
#: against it; keeping the same base keeps v2 output the same visual size
#: (the TTML spec default of 1c ≈ 6.67vh reads far too large for subs).
DEFAULT_BASE_FONT_SIZE = Dim(4.5, 'vh')

#: Stem darkening: Chrome (DirectWrite gamma / stem darkening) draws text
#: noticeably heavier than linear FreeType AA. This fraction of the font
#: size is added to every glyph's stem width so a Regular CJK face doesn't
#: look anemic next to the v1 output. Scaled by the per-language
#: ``weight_boost`` override.
STEM_DARKEN_FRAC = 0.014
STEM_DARKEN_MAX_PX = 1.6


# --------------------------------------------------------------------------- #
# Canvas
# --------------------------------------------------------------------------- #

@dataclass
class CanvasSpec:
    width: int
    height: int
    content_x: float
    content_y: float
    content_w: float
    content_h: float
    #: safe-area padding inset per edge. Like v1's #pad-box, it only moves
    #: the *region anchoring box* inward — fonts and other lengths keep
    #: resolving against the full content rect, so padding never scales
    #: text (aspect-ratio letterboxing does, by design).
    pad_x: float = 0.0
    pad_y: float = 0.0

    @property
    def content(self) -> Tuple[float, float, float, float]:
        return (self.content_x, self.content_y, self.content_w, self.content_h)

    @property
    def region_box(self) -> Tuple[float, float, float, float]:
        """Content rect inset by the safe-area padding."""
        return (self.content_x + self.pad_x, self.content_y + self.pad_y,
                self.content_w - 2 * self.pad_x,
                self.content_h - 2 * self.pad_y)


def compute_canvas(video_res: Optional[Tuple[int, int]],
                   opts) -> CanvasSpec:
    """
    Determine output canvas size and the content rect subtitles lay out in.

    Default canvas is 1920x1080 (Blu-ray standard). With use_video_dims
    the canvas matches the video (optionally scaled to fit HD). The
    content rect letterboxes the video's aspect (or a manual AR override)
    inside the canvas unless force_16_9 is set.
    """
    if opts.use_video_dims and video_res:
        vw, vh = video_res
        if opts.scale_to_hd and (vw > 1920 or vh > 1080):
            s = min(1920 / vw, 1080 / vh)
            cw, ch = int(round(vw * s / 2) * 2), int(round(vh * s / 2) * 2)
        else:
            cw, ch = vw, vh
    else:
        cw, ch = 1920, 1080

    # content rect
    if opts.override_ar and opts.ar_w > 0 and opts.ar_h > 0:
        ar = opts.ar_w / opts.ar_h
    elif opts.force_16_9 or not video_res:
        ar = cw / ch
    else:
        ar = video_res[0] / video_res[1]

    canvas_ar = cw / ch
    if abs(ar - canvas_ar) < 1e-3:
        cx, cy, cwid, chei = 0.0, 0.0, float(cw), float(ch)
    elif ar > canvas_ar:
        cwid = float(cw)
        chei = cw / ar
        cx, cy = 0.0, (ch - chei) / 2.0
    else:
        chei = float(ch)
        cwid = ch * ar
        cx, cy = (cw - cwid) / 2.0, 0.0

    # safe-area padding: stored separately — it insets only the region
    # anchoring box (see CanvasSpec.region_box), never the unit-reference
    # rect, so text size is unaffected.
    pad_x = pad_y = 0.0
    if opts.use_padding:
        pad_x = cwid * (opts.padding_h / 100.0) / 2.0
        pad_y = chei * (opts.padding_v / 100.0) / 2.0

    return CanvasSpec(cw, ch, cx, cy, cwid, chei, pad_x, pad_y)


# --------------------------------------------------------------------------- #
# Rendered cue
# --------------------------------------------------------------------------- #

@dataclass
class RenderedCue:
    cue_uid: int
    x: int
    y: int
    bitmap: np.ndarray          # HxWx4 uint8 straight RGBA
    canvas_w: int
    canvas_h: int

    @property
    def width(self) -> int:
        return self.bitmap.shape[1]

    @property
    def height(self) -> int:
        return self.bitmap.shape[0]


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

class CueRenderer:
    def __init__(self, doc: SubtitleDocument, canvas: CanvasSpec,
                 overrides: Optional[OverrideSet] = None,
                 is_hdr: bool = False):
        self.doc = doc
        self.canvas = canvas
        self.overrides = overrides or OverrideSet()
        self.is_hdr = is_hdr
        self.engine = LayoutEngine()
        self.fm = FontManager.instance()

    # ------------------------------------------------------------------ #
    def unit_ctx(self, font_px: float = 48.0) -> UnitContext:
        return UnitContext(
            canvas_w=self.canvas.content_w, canvas_h=self.canvas.content_h,
            doc_w=self.doc.px_width, doc_h=self.doc.px_height,
            cell_rows=self.doc.cell_rows, cell_cols=self.doc.cell_cols,
            font_size_px=font_px, parent_font_px=font_px)

    # ------------------------------------------------------------------ #
    def _profile(self, lang: str) -> Optional[Style]:
        """Default-profile fallback for a language (below doc initials)."""
        return self.overrides.profile_for(lang or self.doc.language)

    # ------------------------------------------------------------------ #
    def render_cue(self, cue: Cue) -> Optional[RenderedCue]:
        region = self.doc.get_region(cue)
        lang = cue.lang or self.doc.language
        so = self.overrides.for_language(lang)
        ov_style = so.to_style(is_hdr=self.is_hdr)

        reg_spec = self.doc.specified_style(region.style_refs, region.style)
        vertical = self._is_vertical(reg_spec, cue)
        rtl_cols = (reg_spec.writing_mode or 'tbrl') != 'tblr'

        items, para = self._flatten(cue, region, ov_style, lang, vertical, so)
        if not items:
            return None

        # region rect (pre-layout; shrink-wrap resolved after)
        rect = self._region_rect(region, so)
        measure = rect['h'] if vertical else rect['w']

        lh_px, lh_factor = self._line_height(para, so)
        result = self.engine.layout(
            items, measure,
            vertical=vertical,
            text_align=self._text_align(para, reg_spec, vertical),
            multi_row_align=para.multi_row_align,
            line_height_px=lh_px, line_height_factor=lh_factor,
            wrap=para.wrap,
            rtl_columns=rtl_cols)

        block = render_layout(result, extra_opacity=self._region_opacity(reg_spec))
        if block.bitmap.shape[0] <= 1 and block.bitmap.shape[1] <= 1:
            return None

        # position block inside region (display_align on block axis)
        bx, by = self._position_block(rect, result, reg_spec, para, vertical,
                                      rtl_cols)

        x = int(round(bx - block.origin_x))
        y = int(round(by - block.origin_y))

        bitmap = block.bitmap
        # region background behind text (whenActive or always)
        bitmap, x, y = self._apply_region_background(
            reg_spec, rect, bitmap, x, y)

        # clamp to canvas
        bitmap, x, y = _clamp_to_canvas(bitmap, x, y,
                                        self.canvas.width, self.canvas.height)
        if bitmap.size == 0:
            return None
        return RenderedCue(cue.uid, x, y, bitmap,
                           self.canvas.width, self.canvas.height)

    # ------------------------------------------------------------------ #
    def _is_vertical(self, reg_spec: Style, cue: Cue) -> bool:
        wm = reg_spec.writing_mode
        if not wm and cue.inline_style is not None:
            wm = cue.inline_style.writing_mode
        if not wm:
            spec = self.doc.specified_style(cue.style_refs, None)
            wm = spec.writing_mode
        return bool(wm) and (wm.startswith('tb') or 'vertical' in wm)

    def _region_opacity(self, reg_spec: Style) -> float:
        return reg_spec.opacity if reg_spec.opacity is not None else 1.0

    # ------------------------------------------------------------------ #
    def _region_rect(self, region: Region,
                     so: Optional[StyleOverrides] = None) -> dict:
        """
        Resolve region geometry to canvas-absolute pixels.

        Region positions and %-sizes resolve against the *padded* box
        (v1's #pad-box), while every other unit in the pipeline uses the
        full content rect — safe-area padding moves regions inward
        without shrinking text. Padding is per language (from the cue's
        override set) on top of any canvas-level inset.
        """
        cx, cy, cw, ch = self.canvas.region_box
        if so is not None and so.use_padding:
            px = cw * (so.padding_h / 100.0) / 2.0
            py = ch * (so.padding_v / 100.0) / 2.0
            cx, cy, cw, ch = cx + px, cy + py, cw - 2 * px, ch - 2 * py
        ctx = UnitContext(
            canvas_w=cw, canvas_h=ch,
            doc_w=self.doc.px_width, doc_h=self.doc.px_height,
            cell_rows=self.doc.cell_rows, cell_cols=self.doc.cell_cols,
            font_size_px=48.0, parent_font_px=48.0)

        w = ctx.resolve(region.width, axis='x') if region.width else None
        h = ctx.resolve(region.height, axis='y') if region.height else None

        return {
            'region': region, 'ctx': ctx,
            'cx': cx, 'cy': cy, 'cw': cw, 'ch': ch,
            'w': w, 'h': h,
        }

    def _anchor_pos(self, rect: dict, extent_w: float, extent_h: float
                    ) -> Tuple[float, float]:
        """Compute region top-left from anchoring, given final extent."""
        region: Region = rect['region']
        ctx: UnitContext = rect['ctx']
        cx, cy, cw, ch = rect['cx'], rect['cy'], rect['cw'], rect['ch']

        xv = ctx.resolve(region.x, axis='x') or 0.0
        yv = ctx.resolve(region.y, axis='y') or 0.0

        if region.x_edge == 'left':
            x = cx + xv
        elif region.x_edge == 'right':
            x = cx + cw - xv - extent_w
        elif region.x_edge == 'point':
            p = (region.x.value / 100.0) if region.x and region.x.unit == '%' \
                else (xv / cw if cw else 0)
            x = cx + (cw - extent_w) * p
        else:  # center: region center at xv from content left
            x = cx + xv - extent_w / 2.0

        if region.y_edge == 'top':
            y = cy + yv
        elif region.y_edge == 'bottom':
            y = cy + ch - yv - extent_h
        elif region.y_edge == 'point':
            p = (region.y.value / 100.0) if region.y and region.y.unit == '%' \
                else (yv / ch if ch else 0)
            y = cy + (ch - extent_h) * p
        else:
            y = cy + yv - extent_h / 2.0
        return x, y

    # ------------------------------------------------------------------ #
    def _position_block(self, rect: dict, result: LayoutResult,
                        reg_spec: Style, para: 'ParaStyle', vertical: bool,
                        rtl_cols: bool) -> Tuple[float, float]:
        bw, bh = result.width, result.height
        rw = rect['w'] if rect['w'] is not None else bw
        rh = rect['h'] if rect['h'] is not None else bh
        rx, ry = self._anchor_pos(rect, rw, rh)

        da = para.display_align or reg_spec.display_align or 'before'
        if vertical:
            # block axis is horizontal; tbrl grows leftward from the right
            free = rw - bw
            if da == 'after':
                off = 0.0 if rtl_cols else free
            elif da == 'center':
                off = free / 2.0
            else:  # before
                off = free if rtl_cols else 0.0
            return rx + off, ry
        free = rh - bh
        if da == 'after':
            off = free
        elif da == 'center':
            off = free / 2.0
        else:
            off = 0.0
        return rx, ry + off

    def _text_align(self, para: 'ParaStyle', reg_spec: Style,
                    vertical: bool) -> str:
        # Defaults when nothing is specified anywhere: horizontal subtitles
        # center (every authoring house sets textAlign explicitly; center is
        # the subtitle-appropriate fallback), vertical columns start at the
        # top (the spec 'start' default — matches Netflix vertical masters
        # whose vertical styles carry no textAlign).
        ta = para.text_align or reg_spec.text_align or \
            ('start' if vertical else 'center')
        # physical mapping (LTR assumption)
        return {'left': 'start', 'right': 'end'}.get(ta, ta)

    def _line_height(self, para: 'ParaStyle', so: StyleOverrides
                     ) -> Tuple[Optional[float], float]:
        lh = para.line_height
        if so.override_line_height:
            lh = so.line_height
        if lh is None:
            return None, 1.25
        if lh.unit == '':
            return None, max(0.5, lh.value)
        ctx = self.unit_ctx(para.base_font_px)
        px = ctx.resolve(lh, axis='y', percent_of='font')
        return (px, 1.25) if px else (None, 1.25)

    # ------------------------------------------------------------------ #
    def _apply_region_background(self, reg_spec: Style, rect: dict,
                                 bitmap: np.ndarray, x: int, y: int):
        bg = reg_spec.background_color
        if not bg or bg[3] == 0 or rect['w'] is None:
            return bitmap, x, y
        rw = rect['w']
        rh = rect['h'] if rect['h'] is not None else bitmap.shape[0]
        rx, ry = self._anchor_pos(rect, rw, rh)
        rx, ry = int(round(rx)), int(round(ry))
        w, h = int(round(rw)), int(round(rh))
        # merge region bg + text block into one bitmap
        x0 = min(rx, x)
        y0 = min(ry, y)
        x1 = max(rx + w, x + bitmap.shape[1])
        y1 = max(ry + h, y + bitmap.shape[0])
        canvas = np.zeros((y1 - y0, x1 - x0, 4), np.float32)
        canvas[ry - y0:ry - y0 + h, rx - x0:rx - x0 + w, :3] = \
            np.array(bg[:3], np.float32) / 255.0
        canvas[ry - y0:ry - y0 + h, rx - x0:rx - x0 + w, 3] = bg[3] / 255.0
        # premultiply region bg
        canvas[..., :3] *= canvas[..., 3:4]
        blk = bitmap.astype(np.float32) / 255.0
        blk_p = blk.copy()
        blk_p[..., :3] *= blk_p[..., 3:4]
        sub = canvas[y - y0:y - y0 + blk.shape[0], x - x0:x - x0 + blk.shape[1]]
        sub *= (1.0 - blk_p[..., 3:4])
        sub += blk_p
        out = np.zeros(canvas.shape, np.float32)
        a = canvas[..., 3:4]
        out[..., 3:4] = a
        out[..., :3] = np.where(a > 0, canvas[..., :3] / np.maximum(a, 1e-6), 0)
        return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), x0, y0

    # ------------------------------------------------------------------ #
    # Flattening
    # ------------------------------------------------------------------ #
    def _flatten(self, cue: Cue, region: Region, ov_style: Style,
                 lang: str, vertical: bool, so: StyleOverrides
                 ) -> Tuple[List[InlineItem], 'ParaStyle']:
        doc = self.doc
        items: List[InlineItem] = []

        # root font size: v1-compatible 4.5vh base (em/% chain from here)
        root_ctx = self.unit_ctx()
        root_font = root_ctx.resolve(DEFAULT_BASE_FONT_SIZE, axis='y') \
            or root_ctx.cell_h()

        base_chain = [(cue.style_refs, cue.inline_style)]
        para_computed = doc.resolve_style(base_chain, region, ov_style,
                                          lang,
                                          fallback=self._profile(lang))

        para = ParaStyle(
            text_align=para_computed.text_align,
            multi_row_align=para_computed.multi_row_align,
            display_align=para_computed.display_align,
            line_height=para_computed.line_height,
            wrap=para_computed.wrap,
            base_font_px=root_font)

        # stepwise font chain: profile/region/initial -> cue
        font_px = self._own_font_px(doc, [], None, root_font,
                                    initial=True, region=region,
                                    profile=self._profile(lang))
        font_px = self._own_font_px(doc, cue.style_refs, cue.inline_style,
                                    font_px, override=ov_style
                                    if so.override_font_size else None)
        para.base_font_px = font_px

        state = _WalkState(chain=list(base_chain), font_px=font_px,
                           opacity=1.0)
        self._walk(cue.root, state, items, region, ov_style, lang,
                   vertical, so)
        return items, para

    def _own_font_px(self, doc: SubtitleDocument, refs, inline,
                     parent_px: float, initial: bool = False,
                     region: Optional[Region] = None,
                     override: Optional[Style] = None,
                     profile: Optional[Style] = None) -> float:
        """Resolve the font size specified *directly* on this node."""
        fs: Optional[Dim] = None
        if initial:
            # profile fallback sits below the document's own initials
            if profile is not None and profile.font_size is not None:
                fs = profile.font_size
            # document initial + region-level font size
            if doc.initial.font_size is not None:
                fs = doc.initial.font_size
            if region is not None:
                rspec = doc.specified_style(region.style_refs, region.style)
                if rspec.font_size is not None:
                    fs = rspec.font_size
        spec = doc.specified_style(list(refs), inline) if (refs or inline) \
            else Style()
        if spec.font_size is not None:
            fs = spec.font_size
        if override is not None and override.font_size is not None:
            fs = override.font_size
        if fs is None:
            return parent_px
        ctx = self.unit_ctx(parent_px)
        ctx.parent_font_px = parent_px
        v = ctx.resolve_font_size(fs)
        return v if v and v > 0 else parent_px

    # ------------------------------------------------------------------ #
    def _walk(self, node, state: '_WalkState', items: List[InlineItem],
              region: Region, ov_style: Style, lang: str, vertical: bool,
              so: StyleOverrides):
        doc = self.doc
        for child in node.children:
            if child.kind == 'br':
                items.append(BreakItem())
                continue
            if child.kind == 'text':
                computed = doc.resolve_style(state.chain, region, ov_style,
                                             lang,
                                             fallback=self._profile(lang))
                rs = self._run_style(computed, state, lang, vertical)
                items.append(TextItem(child.text, rs))
                continue
            # span
            child_lang = child.meta.get('lang', lang)
            new_font = self._own_font_px(doc, child.style_refs,
                                         child.inline_style, state.font_px)
            spec = doc.specified_style(child.style_refs, child.inline_style)
            new_opacity = state.opacity * (spec.opacity
                                           if spec.opacity is not None else 1.0)
            sub = _WalkState(
                chain=state.chain + [(child.style_refs, child.inline_style)],
                font_px=new_font, opacity=new_opacity)
            computed = doc.resolve_style(sub.chain, region, ov_style,
                                         child_lang,
                                         fallback=self._profile(child_lang))

            if computed.ruby == 'container':
                item = self._build_ruby(child, sub, region, ov_style,
                                        child_lang, vertical, computed)
                if item is not None:
                    items.append(item)
                    continue
                # fall through if malformed
            if computed.text_combine and computed.text_combine != 'none' \
                    and vertical:
                rs = self._run_style(computed, sub, child_lang, vertical)
                items.append(TCYItem(child.plain_text(), rs))
                continue
            self._walk(child, sub, items, region, ov_style, child_lang,
                       vertical, so)

    def _build_ruby(self, container, state: '_WalkState', region: Region,
                    ov_style: Style, lang: str, vertical: bool,
                    container_computed: ComputedStyle) -> Optional[RubyItem]:
        doc = self.doc
        base_items: List[TextItem] = []
        ann_items: List[TextItem] = []

        def collect(node, st: _WalkState, target: str):
            for ch in node.children:
                if ch.kind == 'text':
                    computed = doc.resolve_style(st.chain, region,
                                                 ov_style, lang,
                                                 fallback=self._profile(lang))
                    rs = self._run_style(computed, st, lang, vertical)
                    (base_items if target == 'base' else ann_items).append(
                        TextItem(ch.text, rs))
                elif ch.kind == 'br':
                    continue
                else:
                    nf = self._own_font_px(doc, ch.style_refs,
                                           ch.inline_style, st.font_px)
                    sub = _WalkState(
                        chain=st.chain + [(ch.style_refs, ch.inline_style)],
                        font_px=nf, opacity=st.opacity)
                    comp = doc.resolve_style(sub.chain, region, ov_style,
                                             lang,
                                             fallback=self._profile(lang))
                    role = comp.ruby or ''
                    ntarget = target
                    if role in ('base', 'baseContainer'):
                        ntarget = 'base'
                    elif role in ('text', 'textContainer'):
                        ntarget = 'ann'
                    elif role == 'delimiter':
                        continue
                    # annotation spans that explicitly set their own font
                    # size are used as-is (no extra ruby scaling)
                    own_spec = doc.specified_style(ch.style_refs,
                                                   ch.inline_style)
                    explicit_size = own_spec.font_size is not None
                    if ntarget == 'ann' and (ch.kind == 'span'):
                        sub_items_before = len(ann_items)
                        collect(ch, sub, 'ann')
                        if explicit_size:
                            for ti in ann_items[sub_items_before:]:
                                ti.style.ruby_scale = 1.0
                    else:
                        collect(ch, sub, ntarget)

        collect(container, state, 'base')
        if not base_items and not ann_items:
            return None
        if not ann_items:
            return None
        rs = self._run_style(container_computed, state, lang, vertical)
        pos = container_computed.ruby_position or 'before'
        return RubyItem(base=base_items, annotation=ann_items, style=rs,
                        position=pos,
                        align=container_computed.ruby_align or 'center')

    # ------------------------------------------------------------------ #
    def _run_style(self, computed: ComputedStyle, state: '_WalkState',
                   lang: str, vertical: bool) -> RunStyle:
        ctx = self.unit_ctx(state.font_px)
        rs = RunStyle()
        rs.font_px = state.font_px
        rs.lang = lang
        rs.color = computed.color
        rs.opacity = max(0.0, min(1.0, state.opacity * computed.opacity_mult))
        rs.background = computed.background_color
        rs.bold = computed.font_weight in ('bold', '600', '700', '800', '900')
        rs.italic = computed.font_style in ('italic', 'oblique')
        boost = self.overrides.for_language(lang).weight_boost
        rs.embolden_px = min(state.font_px * STEM_DARKEN_FRAC,
                             STEM_DARKEN_MAX_PX) * max(0.0, boost)
        rs.shear_deg = computed.shear
        rs.shear_axis = 'y' if vertical else 'x'
        rs.text_decoration = computed.text_decoration
        rs.ruby_scale = computed.ruby_scale
        ow = ctx.resolve(computed.outline_width, axis='y', percent_of='font')
        rs.outline_px = max(0.0, ow or 0.0)
        rs.outline_color = computed.outline_color
        ls = ctx.resolve(computed.letter_spacing, axis='y', percent_of='font') \
            if computed.letter_spacing else None
        rs.letter_spacing_px = ls or 0.0
        shadows = []
        for sh in computed.shadows:
            dx = ctx.resolve(sh.offset_x, axis='x', percent_of='font') or 0.0
            dy = ctx.resolve(sh.offset_y, axis='y', percent_of='font') or 0.0
            bl = ctx.resolve(sh.blur, axis='y', percent_of='font') or 0.0
            col = sh.color
            if sh.alpha < 1.0:
                col = (col[0], col[1], col[2],
                       int(round(col[3] * sh.alpha)))
            shadows.append(ResolvedShadow(dx, dy, bl, col))
        rs.shadows = shadows
        if computed.text_emphasis_style:
            rs.emphasis_style = computed.text_emphasis_style
            rs.emphasis_color = computed.text_emphasis_color
            rs.emphasis_position = computed.text_emphasis_position or 'before'
        rs.faces = self.fm.resolve_stack(
            computed.font_family, lang=lang,
            weight='bold' if rs.bold else 'normal', italic=rs.italic,
            preferred=self.overrides.for_language(lang).default_font)
        return rs


@dataclass
class ParaStyle:
    text_align: Optional[str] = None
    multi_row_align: Optional[str] = None
    display_align: Optional[str] = None
    line_height: Optional[Dim] = None
    wrap: bool = True
    base_font_px: float = 48.0


@dataclass
class _WalkState:
    chain: list
    font_px: float
    opacity: float


# --------------------------------------------------------------------------- #

def _clamp_to_canvas(bitmap: np.ndarray, x: int, y: int,
                     cw: int, ch: int):
    h, w = bitmap.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(cw, x + w), min(ch, y + h)
    if x0 >= x1 or y0 >= y1:
        return np.zeros((0, 0, 4), np.uint8), 0, 0
    if x0 != x or y0 != y or x1 - x0 != w or y1 - y0 != h:
        bitmap = bitmap[y0 - y:y1 - y, x0 - x:x1 - x]
    return bitmap, x0, y0
