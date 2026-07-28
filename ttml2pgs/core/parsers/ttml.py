"""
TTML1 / TTML2 / IMSC parser.

Follows the W3C recommendations:
  * TTML1 (2018-11-08): https://www.w3.org/TR/2018/REC-ttml1-20181108/
  * TTML2 (2018-11-08): https://www.w3.org/TR/2018/REC-ttml2-20181108/

Coverage highlights
-------------------
* Namespaced attribute handling by local name (tolerates Netflix/BBC/
  iTunes/EBU-TT-D namespace variants and custom prefixes).
* ``ttp:`` timing parameters: frameRate, frameRateMultiplier, tickRate,
  subFrameRate, timeBase (media/smpte), dropMode, cellResolution — and
  the Netflix ``nttm:Smpte24TimingAdjusted`` quirk.
* Full time expressions (clock-time with frames, offset-time h/m/s/ms/f/t).
* Referential styling (chained ``style`` attributes on style elements),
  inline styling, region styling (incl. nested <style> inside <region>),
  <initial> elements, and inheritance-preserving span trees — styles are
  kept as *references* so later edits re-cascade.
* tts:origin/extent/position region geometry (CSS-position-like syntax),
  writingMode, displayAlign, textAlign, padding, showBackground.
* TTML2 ruby (container/base/text/baseContainer/textContainer/delimiter),
  textCombine (tate-chu-yoko), shear/fontShear, textEmphasis, textOutline,
  textShadow, rubyAlign/rubyPosition/rubyReserve, textOrientation.
* xml:space handling (default = collapse, preserve honored).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from ..colors import parse_color
from ..model import (Cue, Region, Shadow, SpanNode, Style, SubtitleDocument)
from ..timing import TTMLTimeContext, parse_ttml_time
from ..units import Dim, parse_dim_pair

XML_NS = 'http://www.w3.org/XML/1998/namespace'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _get_attr(el: ET.Element, name: str) -> Optional[str]:
    """Attribute lookup by local name (any namespace)."""
    if name in el.attrib:
        return el.attrib[name]
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _get_xml_attr(el: ET.Element, name: str) -> Optional[str]:
    return el.attrib.get(f'{{{XML_NS}}}{name}') or el.attrib.get(f'xml:{name}')


# generic family mapping (TTML §8.3.3 <generic-family-name>)
_GENERIC_FAMILIES = {
    'default': 'sans-serif',
    'sansserif': 'sans-serif', 'proportionalsansserif': 'sans-serif',
    'serif': 'serif', 'proportionalserif': 'serif',
    'monospace': 'monospace', 'monospacesansserif': 'monospace',
    'monospaceserif': 'monospace',
}


def _parse_font_family(value: str) -> List[str]:
    fams: List[str] = []
    for raw in value.split(','):
        f = raw.strip().strip('"\'')
        if not f:
            continue
        fams.append(_GENERIC_FAMILIES.get(f.replace(' ', '').lower(), f))
    return fams


def _parse_padding(value: str) -> Optional[Tuple[Dim, Dim, Dim, Dim]]:
    parts = [Dim.parse(p) for p in value.split()]
    if not parts or any(p is None for p in parts):
        return None
    if len(parts) == 1:
        t = r = b = l = parts[0]
    elif len(parts) == 2:
        t, r = parts
        b, l = t, r
    elif len(parts) == 3:
        t, r, b = parts
        l = r
    else:
        t, r, b, l = parts[:4]
    return (t, r, b, l)


class TTMLParser:
    def parse_file(self, path: str) -> SubtitleDocument:
        with open(path, 'rb') as f:
            data = f.read()
        doc = self.parse_bytes(data)
        doc.source_path = path
        return doc

    def parse_string(self, text: str) -> SubtitleDocument:
        return self.parse_bytes(text.encode('utf-8'))

    # ------------------------------------------------------------------ #
    def parse_bytes(self, data: bytes) -> SubtitleDocument:
        root = ET.fromstring(data)
        doc = SubtitleDocument(source_format='ttml')
        self._parse_root_params(root, doc)

        head = self._find(root, 'head')
        if head is not None:
            self._parse_head(head, doc)

        body = self._find(root, 'body')
        if body is not None:
            self._parse_body(body, doc)

        self._normalize_whitespace(doc, preserve=self._space_preserved)
        return doc

    # ------------------------------------------------------------------ #
    def _find(self, parent: ET.Element, name: str) -> Optional[ET.Element]:
        for child in parent:
            if _local(child.tag) == name:
                return child
        return None

    # ------------------------------------------------------------------ #
    def _parse_root_params(self, root: ET.Element, doc: SubtitleDocument):
        self._space_preserved = (_get_xml_attr(root, 'space') == 'preserve')

        lang = _get_xml_attr(root, 'lang')
        if lang:
            doc.language = lang.strip()

        extent = _get_attr(root, 'extent')
        if extent:
            pair = parse_dim_pair(extent)
            if pair and pair[0].unit == 'px' and pair[1].unit == 'px':
                doc.px_width = int(pair[0].value)
                doc.px_height = int(pair[1].value)
            doc.metadata['tts:extent'] = extent

        cell = _get_attr(root, 'cellResolution')
        if cell:
            m = re.match(r'\s*(\d+)\s+(\d+)\s*$', cell)
            if m:
                doc.cell_cols = int(m.group(1))
                doc.cell_rows = int(m.group(2))

        # --- timing parameters ---------------------------------------- #
        ctx = TTMLTimeContext()
        fr = _get_attr(root, 'frameRate')
        rate = None
        if fr:
            try:
                rate = Fraction(int(fr), 1)
            except ValueError:
                pass
        mult = _get_attr(root, 'frameRateMultiplier')
        if rate is not None and mult:
            m = re.match(r'\s*(\d+)\s+(\d+)\s*$', mult)
            if m:
                rate = rate * Fraction(int(m.group(1)), int(m.group(2)))
        # Netflix quirk: SMPTE-24 adjusted timing => effective 23.976
        smpte24 = _get_attr(root, 'Smpte24TimingAdjusted')
        if smpte24 and smpte24.strip().lower() == 'true':
            rate = Fraction(24000, 1001)
        if rate is not None:
            ctx.frame_rate = rate
            doc.fps = rate

        sfr = _get_attr(root, 'subFrameRate')
        if sfr:
            try:
                ctx.sub_frame_rate = max(1, int(sfr))
            except ValueError:
                pass

        tick = _get_attr(root, 'tickRate')
        if tick:
            try:
                ctx.tick_rate = max(1, int(tick))
            except ValueError:
                pass
        elif fr:
            ctx.tick_rate = int(round(float(ctx.frame_rate))) * ctx.sub_frame_rate

        tb = _get_attr(root, 'timeBase')
        if tb:
            ctx.time_base = tb.strip()
        dm = _get_attr(root, 'dropMode')
        if dm:
            ctx.drop_mode = dm.strip()
        self._time_ctx = ctx

    # ------------------------------------------------------------------ #
    # <head>
    # ------------------------------------------------------------------ #
    def _parse_head(self, head: ET.Element, doc: SubtitleDocument):
        styling = self._find(head, 'styling')
        if styling is not None:
            for node in styling:
                tag = _local(node.tag)
                if tag == 'initial':
                    st = self._style_from_attrs(node)
                    doc.initial = st.merged_over(doc.initial)
                    doc.initial.id = '__initial__'
                elif tag == 'style':
                    sid = _get_xml_attr(node, 'id') or _get_attr(node, 'id')
                    if not sid:
                        continue
                    st = self._style_from_attrs(node)
                    st.id = sid
                    refs = _get_attr(node, 'style')
                    if refs:
                        st.parent_ids = refs.split()
                    doc.styles[sid] = st

        layout = self._find(head, 'layout')
        if layout is not None:
            for node in layout:
                if _local(node.tag) == 'region':
                    self._parse_region(node, doc)

    # ------------------------------------------------------------------ #
    def _parse_region(self, node: ET.Element, doc: SubtitleDocument):
        rid = _get_xml_attr(node, 'id') or _get_attr(node, 'id') or f'r{len(doc.regions) + 1}'
        region = Region(id=rid)

        # style refs + inline attrs + nested <style> children (TTML1 §8.1.2)
        refs = _get_attr(node, 'style')
        if refs:
            region.style_refs = refs.split()
        st = self._style_from_attrs(node)
        for child in node:
            if _local(child.tag) == 'style':
                st = self._style_from_attrs(child).merged_over(st)
        region.style = st

        # Resolve geometry from origin/extent/position (specified directly
        # or through referenced styles).
        spec = doc.specified_style(region.style_refs, region.style)

        # defaults per TTML: origin auto (0,0), extent auto (full container)
        region.x, region.x_edge = Dim(0, '%'), 'left'
        region.y, region.y_edge = Dim(0, '%'), 'top'
        region.width, region.height = Dim(100, '%'), Dim(100, '%')

        if spec.extent is not None:
            region.width, region.height = spec.extent
        if spec.origin is not None:
            region.x, region.y = spec.origin
            region.x_edge = region.y_edge = 'point-origin'
            # origin is a plain offset of the top/left corner:
            region.x_edge, region.y_edge = 'left', 'top'
        if spec.position:
            self._apply_position(spec.position, region)

        doc.regions[rid] = region

    def _apply_position(self, pos: str, region: Region):
        """
        tts:position — CSS <position>-like. Sets anchoring on the region.
        Percentage-point semantics ('30% 80%') use the CSS formula
        offset = (container - region) * p, modeled as edge='point'.
        """
        tokens = pos.split()
        h_kw = {'left', 'right'}
        v_kw = {'top', 'bottom'}

        # split tokens into horizontal and vertical components
        h: Optional[Tuple[str, Optional[Dim]]] = None
        v: Optional[Tuple[str, Optional[Dim]]] = None
        i = 0
        pending_center = 0
        while i < len(tokens):
            t = tokens[i]
            tl = t.lower()
            if tl in h_kw:
                off = None
                if i + 1 < len(tokens) and Dim.parse(tokens[i + 1]) is not None \
                        and tokens[i + 1].lower() not in h_kw | v_kw | {'center'}:
                    off = Dim.parse(tokens[i + 1])
                    i += 1
                h = (tl, off)
            elif tl in v_kw:
                off = None
                if i + 1 < len(tokens) and Dim.parse(tokens[i + 1]) is not None \
                        and tokens[i + 1].lower() not in h_kw | v_kw | {'center'}:
                    off = Dim.parse(tokens[i + 1])
                    i += 1
                v = (tl, off)
            elif tl == 'center':
                pending_center += 1
            else:
                d = Dim.parse(t)
                if d is not None:
                    if h is None and pending_center == 0 and v is None:
                        h = ('point', d)
                    elif v is None:
                        v = ('point', d)
                i += 0
            i += 1

        # distribute leftover 'center' keywords
        if h is None and pending_center > 0:
            h = ('center', None)
            pending_center -= 1
        if v is None and pending_center > 0:
            v = ('center', None)
            pending_center -= 1

        if h is not None:
            kind, off = h
            if kind == 'left':
                region.x_edge, region.x = 'left', (off or Dim(0, '%'))
            elif kind == 'right':
                region.x_edge, region.x = 'right', (off or Dim(0, '%'))
            elif kind == 'center':
                region.x_edge, region.x = 'center', Dim(50, '%')
            elif kind == 'point':
                region.x_edge, region.x = 'point', off
        if v is not None:
            kind, off = v
            if kind == 'top':
                region.y_edge, region.y = 'top', (off or Dim(0, '%'))
            elif kind == 'bottom':
                region.y_edge, region.y = 'bottom', (off or Dim(0, '%'))
            elif kind == 'center':
                region.y_edge, region.y = 'center', Dim(50, '%')
            elif kind == 'point':
                region.y_edge, region.y = 'point', off

    # ------------------------------------------------------------------ #
    # attribute -> Style mapping
    # ------------------------------------------------------------------ #
    def _style_from_attrs(self, el: ET.Element) -> Style:
        st = Style()
        g = lambda n: _get_attr(el, n)

        v = g('color')
        if v:
            st.color = parse_color(v)
        v = g('backgroundColor')
        if v:
            st.background_color = parse_color(v)
        v = g('opacity')
        if v:
            try:
                st.opacity = max(0.0, min(1.0, float(v)))
            except ValueError:
                pass
        v = g('visibility')
        if v:
            st.visibility = v.strip()
        v = g('display')
        if v:
            st.display = v.strip()

        v = g('fontFamily')
        if v:
            st.font_family = _parse_font_family(v)
        v = g('fontSize')
        if v:
            parts = v.split()
            d = Dim.parse(parts[-1])   # "w h" → use height
            if d:
                st.font_size = d
        v = g('fontStyle')
        if v:
            st.font_style = v.strip()
        v = g('fontWeight')
        if v:
            st.font_weight = v.strip()
        v = g('letterSpacing')
        if v:
            st.letter_spacing = Dim.parse(v)

        v = g('lineHeight')
        if v and v.strip() != 'normal':
            st.line_height = Dim.parse(v, default_unit='')
        v = g('textAlign')
        if v:
            st.text_align = v.strip()
        v = g('multiRowAlign')
        if v and v.strip() != 'auto':
            st.multi_row_align = v.strip()
        v = g('displayAlign')
        if v:
            st.display_align = v.strip()
        v = g('wrapOption')
        if v:
            st.wrap = (v.strip() != 'noWrap')
        v = g('writingMode')
        if v:
            st.writing_mode = v.strip()
        v = g('textOrientation')
        if v:
            st.text_orientation = v.strip()
        v = g('padding')
        if v:
            st.padding = _parse_padding(v)
        v = g('showBackground')
        if v:
            st.show_background = v.strip()

        v = g('textOutline')
        if v:
            self._parse_text_outline(v, st)
        v = g('textShadow')
        if v:
            self._parse_text_shadow(v, st)
        v = g('textDecoration')
        if v:
            st.text_decoration = v.strip()

        v = g('shear') or g('fontShear')
        if v:
            m = re.match(r'\s*([+-]?[0-9.]+)\s*%?', v)
            if m:
                val = float(m.group(1))
                # tts:shear is a percentage of 100% = 90deg; fontShear ditto.
                if '%' in v:
                    val = val * 0.9
                st.shear = val

        v = g('ruby')
        if v:
            st.ruby = v.strip()
        v = g('rubyAlign')
        if v:
            st.ruby_align = v.strip()
        v = g('rubyPosition') or g('rubyOffset')
        if v and v.strip() in ('before', 'after', 'over', 'under', 'outside'):
            st.ruby_position = v.strip()
        v = g('textCombine')
        if v:
            st.text_combine = v.strip()

        v = g('textEmphasis')
        if v:
            self._parse_text_emphasis(v, st)

        v = g('origin')
        if v and v.strip() != 'auto':
            pair = parse_dim_pair(v)
            if pair:
                st.origin = pair
        v = g('extent')
        if v and v.strip() != 'auto':
            pair = parse_dim_pair(v)
            if pair:
                st.extent = pair
        v = g('position')
        if v:
            st.position = v.strip()

        return st

    def _parse_text_outline(self, value: str, st: Style):
        """tts:textOutline = none | [<color>]? <length> [<length>]?"""
        if value.strip() == 'none':
            st.outline_width = Dim(0, 'px')
            return
        color = None
        width = None
        for tok in value.split():
            c = parse_color(tok)
            d = Dim.parse(tok)
            if c is not None and not re.match(r'^[0-9.]', tok):
                color = c
            elif d is not None and width is None:
                width = d
            elif c is not None and color is None:
                color = c
        if width is not None:
            st.outline_width = width
        if color is not None:
            st.outline_color = color

    def _parse_text_shadow(self, value: str, st: Style):
        """tts:textShadow = none | <shadow># ; shadow = x y [blur]? [color]?"""
        if value.strip() == 'none':
            st.shadows = []
            return
        shadows: List[Shadow] = []
        for term in value.split(','):
            toks = term.split()
            dims: List[Dim] = []
            color = None
            for t in toks:
                d = Dim.parse(t)
                if d is not None and not t.lstrip('+-')[0:1].isalpha():
                    dims.append(d)
                else:
                    c = parse_color(t)
                    if c is not None:
                        color = c
            if len(dims) >= 2:
                sh = Shadow(offset_x=dims[0], offset_y=dims[1],
                            blur=dims[2] if len(dims) > 2 else Dim(0, 'px'),
                            color=color or (0, 0, 0, 255))
                shadows.append(sh)
        if shadows:
            st.shadows = shadows

    def _parse_text_emphasis(self, value: str, st: Style):
        """tts:textEmphasis = none|auto | style keywords + position + color"""
        toks = value.split()
        if 'none' in toks:
            st.text_emphasis_style = None
            return
        pos_kw = {'before', 'after', 'outside'}
        styles = []
        for t in toks:
            tl = t.lower()
            if tl in pos_kw:
                st.text_emphasis_position = 'before' if tl == 'outside' else tl
            elif parse_color(t) is not None and not tl[0].isdigit():
                st.text_emphasis_color = parse_color(t)
            elif tl in ('filled', 'open', 'dot', 'circle', 'sesame', 'auto'):
                styles.append('dot' if tl == 'auto' else tl)
        st.text_emphasis_style = ' '.join(styles) if styles else 'filled dot'

    # ------------------------------------------------------------------ #
    # <body>
    # ------------------------------------------------------------------ #
    def _parse_body(self, body: ET.Element, doc: SubtitleDocument):
        self._parse_container(body, doc,
                              inherited_refs=[],
                              region_id=None,
                              time_offset=0.0,
                              parent_end=None,
                              chain=[])

    def _timing_of(self, el: ET.Element, time_offset: float
                   ) -> Tuple[Optional[float], Optional[float]]:
        begin = _get_attr(el, 'begin')
        end = _get_attr(el, 'end')
        dur = _get_attr(el, 'dur')
        b = parse_ttml_time(begin, self._time_ctx) if begin else None
        e = parse_ttml_time(end, self._time_ctx) if end else None
        d = parse_ttml_time(dur, self._time_ctx) if dur else None
        if b is not None:
            b += time_offset
        if e is not None:
            e += time_offset
        if e is None and d is not None:
            e = (b if b is not None else time_offset) + d
        return b, e

    def _parse_container(self, el: ET.Element, doc: SubtitleDocument,
                         inherited_refs: List[Tuple[List[str], Optional[Style]]],
                         region_id: Optional[str],
                         time_offset: float,
                         parent_end: Optional[float],
                         chain: list):
        refs = (_get_attr(el, 'style') or '').split()
        inline = self._style_from_attrs(el)
        node_chain = chain + [(refs, None if inline.is_empty() else inline)]

        reg = _get_attr(el, 'region') or region_id

        b, e = self._timing_of(el, time_offset)
        offset = b if b is not None else time_offset
        end_limit = e if e is not None else parent_end

        for child in el:
            tag = _local(child.tag)
            if tag == 'div':
                self._parse_container(child, doc, inherited_refs, reg,
                                      offset, end_limit, node_chain)
            elif tag == 'p':
                self._parse_p(child, doc, reg, offset, end_limit, node_chain)

    def _parse_p(self, p: ET.Element, doc: SubtitleDocument,
                 region_id: Optional[str],
                 time_offset: float, parent_end: Optional[float],
                 chain: list):
        b, e = self._timing_of(p, time_offset)
        if b is None:
            return
        if e is None:
            e = parent_end if parent_end is not None else b + 5000.0

        cue = Cue(begin_ms=b, end_ms=e)
        cue.source_id = _get_xml_attr(p, 'id') or ''
        reg = _get_attr(p, 'region') or region_id
        cue.region_id = reg
        lang = _get_xml_attr(p, 'lang')
        if lang:
            cue.lang = lang

        # ancestor styling (body/div chain) is carried into the cue's
        # style context: flatten it into ordered refs + a merged inline.
        anc_refs: List[str] = []
        anc_inline = Style()
        for refs, inline in chain:
            anc_refs.extend(refs)
            if inline is not None:
                anc_inline = inline.merged_over(anc_inline)
        p_refs = (_get_attr(p, 'style') or '').split()
        p_inline = self._style_from_attrs(p)
        cue.style_refs = anc_refs + p_refs
        merged_inline = p_inline.merged_over(
            None if anc_inline.is_empty() else anc_inline)
        cue.inline_style = None if merged_inline.is_empty() else merged_inline

        cue.root = SpanNode(kind='root')
        self._parse_inline_content(p, cue.root)
        doc.cues.append(cue)

    def _parse_inline_content(self, el: ET.Element, target: SpanNode):
        if el.text:
            target.children.append(SpanNode.text_node(el.text))
        for child in el:
            tag = _local(child.tag)
            if tag == 'br':
                target.children.append(SpanNode.br())
            elif tag == 'span':
                sp = SpanNode(kind='span')
                sp.style_refs = (_get_attr(child, 'style') or '').split()
                inline = self._style_from_attrs(child)
                sp.inline_style = None if inline.is_empty() else inline
                lang = _get_xml_attr(child, 'lang')
                if lang:
                    sp.meta['lang'] = lang
                self._parse_inline_content(child, sp)
                target.children.append(sp)
            # unknown elements: recurse transparently (metadata is skipped)
            elif tag not in ('metadata',):
                self._parse_inline_content(child, target)
            if child.tail:
                target.children.append(SpanNode.text_node(child.tail))

    # ------------------------------------------------------------------ #
    # whitespace normalization (xml:space="default")
    # ------------------------------------------------------------------ #
    def _normalize_whitespace(self, doc: SubtitleDocument, preserve: bool):
        if preserve:
            return
        for cue in doc.cues:
            texts = [n for n in cue.root.iter_text_nodes()]
            for n in texts:
                n.text = re.sub(r'[ \t\r\n]+', ' ', n.text)
            # trim at paragraph edges and around <br>
            self._trim_edges(cue.root)
            # drop empty text nodes
            self._prune_empty(cue.root)

    def _linear_text_nodes(self, node: SpanNode, out: list):
        for c in node.children:
            if c.kind == 'text':
                out.append(c)
            elif c.kind == 'br':
                out.append(c)
            else:
                self._linear_text_nodes(c, out)

    def _trim_edges(self, root: SpanNode):
        seq: List[SpanNode] = []
        self._linear_text_nodes(root, seq)
        # leading
        for n in seq:
            if n.kind == 'br':
                break
            n.text = n.text.lstrip()
            if n.text:
                break
        # trailing
        for n in reversed(seq):
            if n.kind == 'br':
                break
            n.text = n.text.rstrip()
            if n.text:
                break
        # around brs
        for i, n in enumerate(seq):
            if n.kind == 'br':
                for prev in reversed(seq[:i]):
                    if prev.kind == 'br':
                        break
                    prev.text = prev.text.rstrip()
                    if prev.text:
                        break
                for nxt in seq[i + 1:]:
                    if nxt.kind == 'br':
                        break
                    nxt.text = nxt.text.lstrip()
                    if nxt.text:
                        break

    def _prune_empty(self, node: SpanNode) -> bool:
        node.children = [c for c in node.children if not self._is_empty(c)]
        for c in node.children:
            if c.kind not in ('text', 'br'):
                self._prune_empty(c)
        return True

    def _is_empty(self, node: SpanNode) -> bool:
        if node.kind == 'text':
            return node.text == ''
        if node.kind == 'br':
            return False
        self._prune_empty(node)
        return not node.children
