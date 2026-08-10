"""
Exporters: document model → TTML / WebVTT / SRT text.

TTML export is near-lossless (styles, regions, ruby, vertical, shear,
emphasis all round-trip). WebVTT keeps positioning (as cue settings),
classes, ruby and italics but loses TTML-only features like shear.
SRT keeps text, basic tags, font color and an ``{\\anX}`` position hint.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .colors import to_hex
from .model import Cue, Region, Shadow, SpanNode, Style, SubtitleDocument
from .timing import format_srt_timestamp, format_vtt_timestamp
from .units import Dim


# --------------------------------------------------------------------------- #
# TTML
# --------------------------------------------------------------------------- #

_TT = 'http://www.w3.org/ns/ttml'
_TTS = 'http://www.w3.org/ns/ttml#styling'
_TTP = 'http://www.w3.org/ns/ttml#parameter'


def _style_attrs(st: Style) -> Dict[str, str]:
    a: Dict[str, str] = {}
    if st.color is not None:
        a['tts:color'] = to_hex(st.color)
    if st.background_color is not None:
        a['tts:backgroundColor'] = to_hex(st.background_color)
    if st.font_family is not None:
        a['tts:fontFamily'] = ', '.join(st.font_family)
    if st.font_size is not None:
        a['tts:fontSize'] = str(st.font_size)
    if st.font_style is not None:
        a['tts:fontStyle'] = st.font_style
    if st.font_weight is not None:
        a['tts:fontWeight'] = st.font_weight
    if st.line_height is not None:
        a['tts:lineHeight'] = str(st.line_height)
    if st.text_align is not None:
        a['tts:textAlign'] = st.text_align
    if st.multi_row_align is not None:
        a['ebutts:multiRowAlign'] = st.multi_row_align
    if st.display_align is not None:
        a['tts:displayAlign'] = st.display_align
    if st.wrap is not None:
        a['tts:wrapOption'] = 'wrap' if st.wrap else 'noWrap'
    if st.writing_mode is not None:
        a['tts:writingMode'] = st.writing_mode
    if st.text_orientation is not None:
        a['tts:textOrientation'] = st.text_orientation
    if st.opacity is not None:
        a['tts:opacity'] = f"{st.opacity:g}"
    if st.visibility is not None:
        a['tts:visibility'] = st.visibility
    if st.padding is not None:
        a['tts:padding'] = ' '.join(str(p) for p in st.padding)
    if st.show_background is not None:
        a['tts:showBackground'] = st.show_background
    if st.outline_width is not None:
        if st.outline_width.value <= 0:
            a['tts:textOutline'] = 'none'
        else:
            col = to_hex(st.outline_color) if st.outline_color else ''
            a['tts:textOutline'] = f"{col} {st.outline_width}".strip()
    if st.shadows is not None:
        if not st.shadows:
            a['tts:textShadow'] = 'none'
        else:
            terms = []
            for sh in st.shadows:
                col = list(sh.color)
                col[3] = int(round(col[3] * sh.alpha))
                terms.append(f"{sh.offset_x} {sh.offset_y} {sh.blur} "
                             f"{to_hex(tuple(col))}")
            a['tts:textShadow'] = ', '.join(terms)
    if st.text_decoration is not None:
        a['tts:textDecoration'] = st.text_decoration
    if st.shear is not None:
        a['tts:shear'] = f"{st.shear / 0.9:g}%"
    if st.ruby is not None:
        a['tts:ruby'] = st.ruby
    if st.ruby_align is not None:
        a['tts:rubyAlign'] = st.ruby_align
    if st.ruby_position is not None:
        a['tts:rubyPosition'] = st.ruby_position
    if st.text_combine is not None:
        a['tts:textCombine'] = st.text_combine
    if st.text_emphasis_style is not None:
        parts = [st.text_emphasis_style]
        if st.text_emphasis_color:
            parts.append(to_hex(st.text_emphasis_color))
        if st.text_emphasis_position:
            parts.append(st.text_emphasis_position)
        a['tts:textEmphasis'] = ' '.join(parts)
    if st.letter_spacing is not None:
        a['tts:letterSpacing'] = str(st.letter_spacing)
    if st.origin is not None:
        a['tts:origin'] = f"{st.origin[0]} {st.origin[1]}"
    if st.extent is not None:
        a['tts:extent'] = f"{st.extent[0]} {st.extent[1]}"
    if st.position is not None:
        a['tts:position'] = st.position
    return a


def _ms_to_ttml(ms: float) -> str:
    return format_vtt_timestamp(ms)


def _position_value(region: Region) -> str:
    """
    CSS-valid tts:position for a region's anchoring.

    CSS's %-offset-from-edge semantics equal our 'point' anchoring
    (offset scales with container minus region), so every axis maps to
    a keyword form: point p% → 'left p%' / 'top p%', edges keep their
    keyword, 50% points become 'center'. The result parses back to the
    identical anchoring and is valid for other TTML consumers (the old
    emission could produce invalid mixes like '50% bottom 3.5vh').
    """
    def axis(edge: str, val, kw_lo: str, kw_hi: str) -> str:
        if edge == 'center':
            return 'center'
        if edge == 'point':
            if val is not None and val.unit == '%':
                if abs(val.value - 50.0) < 1e-6:
                    return 'center'
                return f'{kw_lo} {val}'
            return f'{kw_lo} {val}' if val is not None else 'center'
        if edge == kw_hi:
            return f'{kw_hi} {val}' if val is not None else kw_hi
        return f'{kw_lo} {val}' if val is not None else kw_lo

    hor = axis(region.x_edge, region.x, 'left', 'right')
    ver = axis(region.y_edge, region.y, 'top', 'bottom')
    return f'{hor} {ver}'


def _override_style(overrides, lang: str, is_hdr: bool) -> Optional[Style]:
    """The effective per-language override styling as a Style, or None
    when nothing is forced (auto-color counts — it IS the rendered
    color)."""
    if overrides is None:
        return None
    so = overrides.for_language(lang)
    st = so.to_style(is_hdr=is_hdr)
    st.opacity_mult = None                # not expressible in exports
    return st if st.set_props() else None


def export_ttml(doc: SubtitleDocument, overrides=None,
                is_hdr: bool = False) -> str:
    ET.register_namespace('', _TT)
    root = ET.Element('tt', {
        'xmlns': _TT, 'xmlns:tts': _TTS, 'xmlns:ttp': _TTP,
        'xmlns:ebutts': 'urn:ebu:tt:style',
        'xml:lang': doc.language or '',
        'tts:extent': f"{doc.px_width}px {doc.px_height}px",
        'ttp:cellResolution': f"{doc.cell_cols} {doc.cell_rows}",
    })
    if doc.fps is not None:
        root.set('ttp:frameRate', str(int(round(float(doc.fps)))))
        if doc.fps.denominator != 1:
            root.set('ttp:frameRateMultiplier',
                     f"{1000} {int(round(1000 * int(round(float(doc.fps)))/float(doc.fps)))}")

    head = ET.SubElement(root, 'head')
    styling = ET.SubElement(head, 'styling')
    if not doc.initial.is_empty():
        ET.SubElement(styling, 'initial', _style_attrs(doc.initial))
    for sid, st in doc.styles.items():
        attrs = _style_attrs(st)
        attrs['xml:id'] = sid
        if st.parent_ids:
            attrs['style'] = ' '.join(st.parent_ids)
        ET.SubElement(styling, 'style', attrs)
    # bake the app's per-language overrides so the export looks like
    # the render: one style per language, referenced LAST on each cue
    ov_style_ids: Dict[str, str] = {}
    if overrides is not None:
        for lang in sorted({(c.lang or doc.language or '')
                            for c in doc.cues}):
            st = _override_style(overrides, lang, is_hdr)
            if st is None:
                continue
            sid = f'ov.{lang or "default"}'
            n = 2
            while sid in doc.styles:
                sid = f'ov.{lang or "default"}_{n}'
                n += 1
            attrs = _style_attrs(st)
            attrs['xml:id'] = sid
            ET.SubElement(styling, 'style', attrs)
            ov_style_ids[lang] = sid
    layout = ET.SubElement(head, 'layout')
    for rid, region in doc.regions.items():
        if rid == '__default__':
            continue
        attrs = _style_attrs(region.style)
        attrs['xml:id'] = rid
        if region.style_refs:
            attrs['style'] = ' '.join(region.style_refs)
        # geometry
        x, y = region.x, region.y
        if region.x_edge == 'left' and region.y_edge == 'top':
            attrs['tts:origin'] = f"{x} {y}"
        else:
            attrs['tts:position'] = _position_value(region)
        if region.width is not None and region.height is not None:
            attrs['tts:extent'] = f"{region.width} {region.height}"
        elif region.width is not None:
            attrs['tts:extent'] = f"{region.width} auto"
        ET.SubElement(layout, 'region', attrs)

    body = ET.SubElement(root, 'body')
    div = ET.SubElement(body, 'div')
    for cue in doc.sorted_cues():
        attrs = {'begin': _ms_to_ttml(cue.begin_ms),
                 'end': _ms_to_ttml(cue.end_ms)}
        if cue.region_id and cue.region_id != '__default__':
            attrs['region'] = cue.region_id
        refs = list(cue.style_refs)
        ov_sid = ov_style_ids.get(cue.lang or doc.language or '')
        if ov_sid:
            refs.append(ov_sid)          # last ref wins per TTML
        if refs:
            attrs['style'] = ' '.join(refs)
        if cue.inline_style is not None:
            attrs.update(_style_attrs(cue.inline_style))
        if cue.source_id:
            attrs['xml:id'] = cue.source_id
        p = ET.SubElement(div, 'p', attrs)
        _emit_ttml_children(p, cue.root)

    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + _serialize_ttml(root, 0))


def _xml_escape(text: str, attr: bool = False) -> str:
    out = (text.replace('&', '&amp;').replace('<', '&lt;')
           .replace('>', '&gt;'))
    if attr:
        out = out.replace('"', '&quot;')
    return out


def _serialize_ttml(el: ET.Element, depth: int) -> str:
    """
    Pretty printer that keeps <p> subtrees compact — indentation inside a
    paragraph would become visible whitespace when reparsed (TTML default
    whitespace handling collapses but does not remove it).
    """
    ind = '  ' * depth
    attrs = ''.join(f' {k}="{_xml_escape(str(v), True)}"'
                    for k, v in el.attrib.items())
    tag = el.tag
    if tag == 'p' or tag.endswith('}p'):
        return f"{ind}{_serialize_inline(el)}\n"
    children = list(el)
    if not children and not (el.text and el.text.strip()):
        return f"{ind}<{tag}{attrs}/>\n"
    out = f"{ind}<{tag}{attrs}>\n"
    for c in children:
        out += _serialize_ttml(c, depth + 1)
    out += f"{ind}</{tag}>\n"
    return out


def _serialize_inline(el: ET.Element) -> str:
    attrs = ''.join(f' {k}="{_xml_escape(str(v), True)}"'
                    for k, v in el.attrib.items())
    inner = _xml_escape(el.text or '')
    for c in el:
        inner += _serialize_inline(c)
        inner += _xml_escape(c.tail or '')
    if not inner and not list(el):
        return f"<{el.tag}{attrs}/>"
    return f"<{el.tag}{attrs}>{inner}</{el.tag}>"


def _emit_ttml_children(parent: ET.Element, node: SpanNode):
    last: Optional[ET.Element] = None
    for child in node.children:
        if child.kind == 'text':
            if last is None:
                parent.text = (parent.text or '') + child.text
            else:
                last.tail = (last.tail or '') + child.text
        elif child.kind == 'br':
            last = ET.SubElement(parent, 'br')
        else:
            attrs = {}
            if child.style_refs:
                attrs['style'] = ' '.join(child.style_refs)
            if child.inline_style is not None:
                attrs.update(_style_attrs(child.inline_style))
            el = ET.SubElement(parent, 'span', attrs)
            _emit_ttml_children(el, child)
            last = el


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #

def _axis_pct(d: Optional[Dim], horizontal: bool,
              default: float) -> float:
    if d is None:
        return default
    if d.unit in ('%', 'vh', 'rh', 'vw', 'rw'):
        return d.value
    if d.unit == 'px':
        return d.value / (1920.0 if horizontal else 1080.0) * 100.0
    return default


def _axis_span(edge: str, val: Optional[Dim], size: Optional[Dim],
               horizontal: bool, size_default: float
               ) -> Tuple[float, float]:
    """Region span [start, start+size] along one axis, in % of canvas.
    Handles edge/center/point anchoring (point = CSS percentage-point:
    offset scales with canvas minus region)."""
    s = _axis_pct(size, horizontal, size_default)
    v = _axis_pct(val, horizontal, 0.0)
    if edge in ('left', 'top'):
        a = v
    elif edge in ('right', 'bottom'):
        a = 100.0 - v - s
    elif edge == 'center':
        a = v - s / 2.0 if val is not None else 50.0 - s / 2.0
    else:                                # point
        a = (100.0 - s) * (v / 100.0)
    return a, s


def _region_to_cue_settings(doc: SubtitleDocument, cue: Cue) -> str:
    """
    Map a region + its alignment to VTT cue settings so the text lands
    where the renderer puts it: `line` follows the DISPLAY-ALIGNED edge
    of the region box, `position` follows the TEXT-ALIGN point within
    the region's horizontal span (a centered full-width band emits no
    position at all — VTT's default centering is exactly right).
    """
    region = doc.get_region(cue)
    spec = doc.specified_style(region.style_refs, region.style)
    parts: List[str] = []
    vertical = region.is_vertical()
    ta = spec.text_align or 'center'
    da = spec.display_align or ('before' if vertical else 'after')

    if vertical:
        parts.append('vertical:rl' if (spec.writing_mode or 'tbrl') != 'tblr'
                     else 'vertical:lr')
        # line = column position measured from the writing start edge
        x0, w = _axis_span(region.x_edge, region.x, region.width,
                           True, 12.0)
        rl = (spec.writing_mode or 'tbrl') != 'tblr'
        if da == 'before':
            edge = (x0 + w) if rl else x0
            la = ''
        elif da == 'center':
            edge, la = x0 + w / 2.0, ',center'
        else:
            edge = x0 if rl else (x0 + w)
            la = ',end'
        line = (100.0 - edge) if rl else edge
        parts.append(f"line:{max(0.0, min(100.0, line)):g}%{la}")
        y0, h = _axis_span(region.y_edge, region.y, region.height,
                           False, 100.0)
        if ta in ('center',):
            pos = y0 + h / 2.0
            if abs(pos - 50.0) > 0.01:
                parts.append(f"position:{pos:g}%,center")
        elif ta in ('end', 'right', 'bottom'):
            parts.append(f"position:{y0 + h:g}%,line-right")
        else:
            if abs(y0) > 0.01:
                parts.append(f"position:{y0:g}%,line-left")
        if region.height is not None:
            parts.append(f"size:{h:g}%")
    else:
        y0, h = _axis_span(region.y_edge, region.y, region.height,
                           False, 12.0)
        if da == 'after':
            parts.append(f"line:{max(0.0, min(100.0, y0 + h)):g}%,end")
        elif da == 'center':
            parts.append(f"line:{y0 + h / 2.0:g}%,center")
        else:
            parts.append(f"line:{max(0.0, min(100.0, y0)):g}%")
        x0, w = _axis_span(region.x_edge, region.x, region.width,
                           True, 90.0)
        if ta == 'center':
            pos = x0 + w / 2.0
            if abs(pos - 50.0) > 0.01:
                parts.append(f"position:{pos:g}%")
        elif ta in ('end', 'right'):
            parts.append(f"position:{x0 + w:g}%,line-right")
        else:                            # start / left
            parts.append(f"position:{x0:g}%,line-left")
        if region.width is not None and abs(w - 100.0) > 0.01:
            parts.append(f"size:{w:g}%")
    if ta in ('start', 'end', 'left', 'right'):
        parts.append(f"align:{ta}")
    return ' '.join(parts)


def _vtt_escape(text: str) -> str:
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def _emit_vtt_node(node: SpanNode, doc: SubtitleDocument, out: List[str]):
    for child in node.children:
        if child.kind == 'text':
            out.append(_vtt_escape(child.text))
        elif child.kind == 'br':
            out.append('\n')
        else:
            spec = doc.specified_style(child.style_refs, child.inline_style)
            ruby = spec.ruby
            if ruby == 'container':
                out.append('<ruby>')
                _emit_vtt_ruby(child, doc, out)
                out.append('</ruby>')
                continue
            tags: List[str] = []
            if spec.font_style in ('italic', 'oblique'):
                tags.append('i')
            if spec.font_weight == 'bold':
                tags.append('b')
            if spec.text_decoration and 'underline' in spec.text_decoration:
                tags.append('u')
            classes = [r for r in child.style_refs
                       if not r.startswith('__')]
            open_tag = ''
            if classes:
                # 's1.en' → classes [s1, en] → <c.s1.en>, matching the
                # exported ::cue(.s1.en) selectors
                cls = [p for c in classes for p in c.split('.') if p]
                open_tag = 'c.' + '.'.join(cls)
            for t in tags:
                out.append(f'<{t}>')
            if open_tag:
                out.append(f'<{open_tag}>')
            _emit_vtt_node(child, doc, out)
            if open_tag:
                out.append('</c>')
            for t in reversed(tags):
                out.append(f'</{t}>')


def _emit_vtt_ruby(container: SpanNode, doc: SubtitleDocument,
                   out: List[str]):
    for child in container.children:
        if child.kind == 'text':
            out.append(_vtt_escape(child.text))
            continue
        if child.kind == 'br':
            continue
        spec = doc.specified_style(child.style_refs, child.inline_style)
        role = spec.ruby or ''
        if role in ('text', 'textContainer'):
            out.append('<rt>')
            out.append(_vtt_escape(child.plain_text()))
            out.append('</rt>')
        elif role == 'delimiter':
            continue
        else:
            out.append(_vtt_escape(child.plain_text()))


def _style_decls(st: Style, sizes: bool = True) -> List[str]:
    decls = []
    if st.color is not None:
        decls.append(f"color: {to_hex(st.color)};")
    if st.background_color is not None:
        decls.append(f"background-color: {to_hex(st.background_color)};")
    if st.font_family:
        decls.append(f"font-family: {', '.join(st.font_family)};")
    if st.font_style:
        decls.append(f"font-style: {st.font_style};")
    if st.font_weight:
        decls.append(f"font-weight: {st.font_weight};")
    if sizes and st.font_size is not None:
        decls.append(f"font-size: {st.font_size};")
    return decls


def export_vtt(doc: SubtitleDocument, overrides=None,
               is_hdr: bool = False) -> str:
    lines = ['WEBVTT', '']

    def style_block(selector: str, decls: List[str]):
        if not decls:
            return
        lines.append('STYLE')
        lines.append(f"::cue({selector}) {{")
        lines.extend(f"  {d}" for d in decls)
        lines.append('}')
        lines.append('')

    # STYLE blocks for exportable named styles
    for sid, st in doc.styles.items():
        if sid.startswith('__'):
            continue
        style_block('.' + '.'.join(p for p in sid.split('.') if p),
                    _style_decls(st, sizes=False))
    # per-language override blocks LAST (later rules win) — matched by
    # the lang-<code> class every cue payload is wrapped in
    langs = sorted({(c.lang or doc.language or '') for c in doc.cues})
    ov_langs = set()
    if overrides is not None:
        for lang in langs:
            st = _override_style(overrides, lang, is_hdr)
            if st is None:
                continue
            style_block(f'.lang-{lang or "und"}', _style_decls(st))
            ov_langs.add(lang)

    for i, cue in enumerate(doc.sorted_cues(), 1):
        settings = _region_to_cue_settings(doc, cue)
        lines.append(str(cue.source_id or i))
        ts = (f"{format_vtt_timestamp(cue.begin_ms)} --> "
              f"{format_vtt_timestamp(cue.end_ms)}")
        if settings:
            ts += ' ' + settings
        lines.append(ts)
        buf: List[str] = []
        # cue-level classes: named refs + the language class, so the
        # ::cue selectors above actually match
        cls: List[str] = []
        lang = cue.lang or doc.language or ''
        if lang in ov_langs:
            cls.append(f'lang-{lang or "und"}')
        for r in cue.style_refs:
            if not r.startswith('__'):
                cls += [p for p in r.split('.') if p]
        if cls:
            buf.append('<c.' + '.'.join(dict.fromkeys(cls)) + '>')
        _emit_vtt_node(cue.root, doc, buf)
        if cls:
            buf.append('</c>')
        lines.append(''.join(buf).strip('\n'))
        lines.append('')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #

_AN_FROM_ALIGN = {
    ('before', 'start'): 7, ('before', 'center'): 8, ('before', 'end'): 9,
    ('center', 'start'): 4, ('center', 'center'): 5, ('center', 'end'): 6,
    ('after', 'start'): 1, ('after', 'center'): 2, ('after', 'end'): 3,
}


def _emit_srt_node(node: SpanNode, doc: SubtitleDocument, out: List[str]):
    for child in node.children:
        if child.kind == 'text':
            out.append(child.text)
        elif child.kind == 'br':
            out.append('\n')
        else:
            spec = doc.specified_style(child.style_refs, child.inline_style)
            if spec.ruby == 'container':
                # flatten ruby to 基(よみ)
                base, ann = [], []
                for c in child.children:
                    cs = doc.specified_style(c.style_refs, c.inline_style) \
                        if c.kind == 'span' else Style()
                    role = cs.ruby or ''
                    if role in ('text', 'textContainer'):
                        ann.append(c.plain_text())
                    elif c.kind != 'br':
                        base.append(c.plain_text())
                out.append(''.join(base))
                if ann:
                    out.append(f"({''.join(ann)})")
                continue
            pre, post = '', ''
            if spec.font_style in ('italic', 'oblique'):
                pre, post = pre + '<i>', '</i>' + post
            if spec.font_weight == 'bold':
                pre, post = pre + '<b>', '</b>' + post
            if spec.text_decoration and 'underline' in spec.text_decoration:
                pre, post = pre + '<u>', '</u>' + post
            if spec.color is not None:
                pre = pre + f'<font color="{to_hex(spec.color, False)}">'
                post = '</font>' + post
            out.append(pre)
            _emit_srt_node(child, doc, out)
            out.append(post)


def export_srt(doc: SubtitleDocument, overrides=None,
               is_hdr: bool = False) -> str:
    out: List[str] = []
    # per-language override color (SRT can express color, not size)
    ov_color: Dict[str, str] = {}
    if overrides is not None:
        for lang in {(c.lang or doc.language or '') for c in doc.cues}:
            st = _override_style(overrides, lang, is_hdr)
            if st is not None and st.color is not None:
                ov_color[lang] = to_hex(st.color, False)
    for i, cue in enumerate(doc.sorted_cues(), 1):
        out.append(str(i))
        out.append(f"{format_srt_timestamp(cue.begin_ms)} --> "
                   f"{format_srt_timestamp(cue.end_ms)}")
        buf: List[str] = []
        # position hint
        region = doc.get_region(cue)
        spec = doc.specified_style(region.style_refs, region.style)
        da = spec.display_align or ('after' if region.y_edge == 'bottom'
                                    else 'before')
        ta = spec.text_align or 'center'
        ta = {'left': 'start', 'right': 'end'}.get(ta, ta)
        an = _AN_FROM_ALIGN.get((da if da in ('before', 'center', 'after')
                                 else 'after',
                                 ta if ta in ('start', 'center', 'end')
                                 else 'center'), 2)
        if an != 2:
            buf.append(f"{{\\an{an}}}")
        col = ov_color.get(cue.lang or doc.language or '')
        if col:
            buf.append(f'<font color="{col}">')
        _emit_srt_node(cue.root, doc, buf)
        if col:
            buf.append('</font>')
        out.append(''.join(buf).strip('\n'))
        out.append('')
    return '\n'.join(out)
