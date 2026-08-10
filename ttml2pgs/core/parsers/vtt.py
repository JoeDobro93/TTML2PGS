"""
WebVTT parser — https://www.w3.org/TR/webvtt1/

Coverage
--------
* Header + header metadata (X-TIMESTAMP-MAP offset applied).
* ``REGION`` blocks (id, width, lines, regionanchor, viewportanchor, scroll).
* ``STYLE`` blocks: ``::cue``, ``::cue(.class…)``, ``::cue(i)``,
  ``::cue(v[voice="…"])`` and ``::cue(lang)`` selectors, mapped to named
  styles referenced by span class nodes.
* ``NOTE`` blocks skipped.
* Cue timing lines with settings: region / vertical / line (percent or
  line-number, with line-alignment) / position (with position-alignment) /
  size / align.
* Cue payload tags: <b> <i> <u> <c.classes> <v speaker> <lang tag>
  <ruby><rt> and cue timestamps (karaoke timestamps recorded, not styled).
* HTML entity decoding.
* Region derivation: cues that carry only positional settings are grouped
  by their positional signature; each distinct signature becomes a Region
  in the document (so a file whose cues all share one position produces
  exactly one region, vertical cues a second, etc.).
"""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

from ..colors import parse_color
from ..model import Cue, Region, Shadow, SpanNode, Style, SubtitleDocument, default_region
from ..timing import parse_vtt_timestamp
from ..units import Dim

_TAG_RE = re.compile(r'(<[^>]*>)')
_TS_TAG_RE = re.compile(r'^\d{1,}:?\d{2}:\d{2}\.\d{3}$|^\d{2}:\d{2}\.\d{3}$')


class VTTParser:
    def parse_file(self, path: str) -> SubtitleDocument:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            content = f.read()
        doc = self.parse_string(content)
        doc.source_path = path
        return doc

    # ------------------------------------------------------------------ #
    def parse_string(self, content: str) -> SubtitleDocument:
        doc = SubtitleDocument(source_format='vtt')
        content = content.replace('\x00', '\ufffd')  # WebVTT: NULL -> U+FFFD
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        # Strip zero-width spaces that break ruby grouping.
        content = content.replace('\u200b', '')

        blocks = re.split(r'\n{2,}', content)
        self._ts_offset = 0.0
        self._sig_regions: Dict[tuple, str] = {}
        cue_blocks: List[List[str]] = []

        first = True
        for block in blocks:
            lines = [l for l in block.split('\n') if l.strip() != '']
            if not lines:
                continue
            head = lines[0].strip()
            if first and head.startswith('WEBVTT'):
                first = False
                self._parse_header_metadata(lines[1:], doc)
                continue
            first = False
            if head.startswith('NOTE'):
                continue
            if head.startswith('STYLE'):
                self._parse_style_block('\n'.join(lines[1:]), doc)
                continue
            if head.startswith('REGION'):
                self._parse_region_block(lines[1:] if head == 'REGION' else lines, doc)
                continue
            cue_blocks.append(lines)

        for lines in cue_blocks:
            self._parse_cue_block(lines, doc)
        return doc

    # ------------------------------------------------------------------ #
    def _parse_header_metadata(self, lines: List[str], doc: SubtitleDocument):
        for line in lines:
            if ':' not in line:
                continue
            key, val = line.split(':', 1)
            key = key.strip()
            doc.metadata[key] = val.strip()
            if key.upper() == 'X-TIMESTAMP-MAP':
                # e.g. X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0
                m = re.search(r'LOCAL:([\d:.]+)', val)
                if m:
                    local = parse_vtt_timestamp(m.group(1))
                    if local:
                        self._ts_offset = -local
            elif key.lower() == 'language':
                doc.language = val.strip()

    # ------------------------------------------------------------------ #
    # REGION blocks
    # ------------------------------------------------------------------ #
    def _parse_region_block(self, lines: List[str], doc: SubtitleDocument):
        settings: Dict[str, str] = {}
        for line in lines:
            for part in line.replace('REGION', '').split():
                if ':' in part:
                    k, v = part.split(':', 1)
                    settings[k.strip().lower()] = v.strip()
        rid = settings.get('id')
        if not rid:
            return

        width = 100.0
        m = re.match(r'([\d.]+)%', settings.get('width', ''))
        if m:
            width = float(m.group(1))
        lines_n = 3
        if settings.get('lines', '').isdigit():
            lines_n = int(settings['lines'])

        def anchor(text: str, default=(0.0, 100.0)) -> Tuple[float, float]:
            mm = re.match(r'([\d.]+)%\s*,\s*([\d.]+)%', text or '')
            if mm:
                return float(mm.group(1)), float(mm.group(2))
            return default

        rax, ray = anchor(settings.get('regionanchor'))
        vax, vay = anchor(settings.get('viewportanchor'))

        # Region anchor point (rax% into the region) is pinned at the
        # viewport anchor point (vax% of the viewport).
        region = Region(id=rid, derived=False)
        region.width = Dim(width, '%')
        # Model as 'point' anchoring when anchors match CSS point semantics;
        # otherwise compute left offset: left% = vax - rax * width/100.
        left = vax - rax * width / 100.0
        region.x, region.x_edge = Dim(left, '%'), 'left'
        # vertical: lines_n lines tall, anchored at vay.
        region.height = None  # shrink to content; lines count is a max
        top = vay - ray  # approximation: ray% of an auto box ~ use offset
        region.y, region.y_edge = Dim(vay, '%'), \
            ('bottom' if ray >= 66 else 'center' if ray >= 34 else 'top')
        if region.y_edge == 'bottom':
            region.y = Dim(100.0 - vay, '%')
        region.style.display_align = 'after' if ray >= 66 else (
            'center' if ray >= 34 else 'before')
        region.style.text_align = 'center'
        region.meta_lines = lines_n  # informational
        doc.regions[rid] = region

    # ------------------------------------------------------------------ #
    # STYLE blocks (::cue selectors)
    # ------------------------------------------------------------------ #
    _CUE_SEL_RE = re.compile(
        r'::cue(?:\(\s*([^)]*)\s*\))?\s*\{([^}]*)\}', re.DOTALL)

    def _parse_style_block(self, css: str, doc: SubtitleDocument):
        # strip CSS comments
        css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
        for m in self._CUE_SEL_RE.finditer(css):
            selector = (m.group(1) or '').strip()
            body = m.group(2)
            st = self._parse_css_declarations(body)
            if st is None:
                continue
            sid, meta = self._selector_to_style_id(selector)
            if sid is None:
                continue
            st.id = sid
            if sid in doc.styles:
                st = st.merged_over(doc.styles[sid])
                st.id = sid
            doc.styles[sid] = st

    def _selector_to_style_id(self, selector: str):
        """
        Map a ::cue selector to an internal style id:
          ''                -> '__cue__'      (cue-wide default)
          '.classA.classB'  -> 'classA.classB' (applied to <c> spans)
          'i' / 'b' / 'u'   -> '__tag_i__' etc.
          'v[voice="X"]'    -> '__voice_X__'
          'lang(ja)' or ':lang(ja)' -> '__lang_ja__'
        """
        if selector == '':
            return '__cue__', None
        if re.fullmatch(r'[ibuc]', selector):
            return f'__tag_{selector}__', None
        m = re.fullmatch(r'v\[voice="?([^"\]]+)"?\]', selector)
        if m:
            return f'__voice_{m.group(1)}__', None
        m = re.fullmatch(r':?lang\(([^)]+)\)', selector)
        if m:
            return f'__lang_{m.group(1)}__', None
        if selector.startswith('.') or re.fullmatch(r'[.\w-]+', selector):
            cls = '.'.join(p for p in selector.split('.') if p)
            return cls, None
        return None, None

    def _parse_css_declarations(self, body: str) -> Optional[Style]:
        st = Style()
        for decl in body.split(';'):
            if ':' not in decl:
                continue
            prop, val = decl.split(':', 1)
            prop = prop.strip().lower()
            val = val.strip().rstrip(';').strip()
            if not val:
                continue
            if prop == 'color':
                st.color = parse_color(val)
            elif prop in ('background', 'background-color'):
                st.background_color = parse_color(val)
            elif prop == 'font-family':
                st.font_family = [f.strip().strip('"\'') for f in val.split(',')]
            elif prop == 'font-size':
                st.font_size = Dim.parse(val)
            elif prop == 'font-style':
                st.font_style = val
            elif prop == 'font-weight':
                st.font_weight = 'bold' if val in ('bold', 'bolder') or \
                    (val.isdigit() and int(val) >= 600) else 'normal'
            elif prop == 'font':
                for tok in val.split():
                    if tok in ('italic', 'oblique'):
                        st.font_style = tok
                    elif tok == 'bold':
                        st.font_weight = 'bold'
            elif prop == 'text-shadow':
                self._parse_css_text_shadow(val, st)
            elif prop == 'text-decoration':
                st.text_decoration = ('underline' if 'underline' in val
                                      else 'lineThrough' if 'line-through' in val
                                      else 'none')
            elif prop in ('text-emphasis', 'text-emphasis-style'):
                st.text_emphasis_style = val
            elif prop == 'text-emphasis-position':
                st.text_emphasis_position = 'after' if 'under' in val else 'before'
            elif prop == 'ruby-position':
                st.ruby_position = 'after' if 'under' in val else 'before'
            elif prop == 'text-combine-upright':
                st.text_combine = 'all' if val != 'none' else 'none'
            elif prop == 'opacity':
                try:
                    st.opacity = float(val)
                except ValueError:
                    pass
            elif prop == 'x-ttml-shear':
                try:
                    st.shear = float(val.rstrip('%'))
                except ValueError:
                    pass
            elif prop == '-webkit-text-stroke' or prop == 'text-stroke':
                for tok in val.split():
                    d = Dim.parse(tok)
                    c = parse_color(tok)
                    if d is not None and not tok.lstrip('+-')[0:1].isalpha():
                        st.outline_width = d
                    elif c is not None:
                        st.outline_color = c
        return None if st.is_empty() else st

    def _parse_css_text_shadow(self, val: str, st: Style):
        shadows = []
        for term in val.split(','):
            dims, color = [], None
            for tok in term.split():
                d = Dim.parse(tok)
                if d is not None and not tok.lstrip('+-')[0:1].isalpha():
                    dims.append(d)
                else:
                    c = parse_color(tok)
                    if c is not None:
                        color = c
            if len(dims) >= 2:
                shadows.append(Shadow(
                    offset_x=dims[0], offset_y=dims[1],
                    blur=dims[2] if len(dims) > 2 else Dim(0, 'px'),
                    color=color or (0, 0, 0, 255)))
        if shadows:
            st.shadows = shadows

    # ------------------------------------------------------------------ #
    # Cue blocks
    # ------------------------------------------------------------------ #
    def _parse_cue_block(self, lines: List[str], doc: SubtitleDocument):
        idx = next((i for i, l in enumerate(lines) if '-->' in l), -1)
        if idx < 0:
            return
        cue_id = ' '.join(lines[:idx]).strip()
        timing = lines[idx]
        payload = '\n'.join(lines[idx + 1:])

        m = re.match(r'\s*(\S+)[ \t]+-->[ \t]+(\S+)[ \t]*(.*)$', timing)
        if not m:
            return
        start = parse_vtt_timestamp(m.group(1))
        end = parse_vtt_timestamp(m.group(2))
        if start is None or end is None:
            return
        settings = self._parse_cue_settings(m.group(3))

        cue = Cue(begin_ms=start + self._ts_offset,
                  end_ms=end + self._ts_offset)
        cue.source_id = cue_id
        cue.region_id = self._resolve_region(settings, doc)
        if '__cue__' in doc.styles:
            cue.style_refs.append('__cue__')

        # cue-level text alignment (applies inside the box)
        align = settings.get('align')
        if align:
            cue.inline_style = cue.inline_style or Style()
            cue.inline_style.text_align = align

        self._parse_payload(payload, cue.root, doc)
        if not cue.root.children:
            return
        self._promote_cue_style(cue)
        doc.cues.append(cue)

    @staticmethod
    def _promote_cue_style(cue: Cue):
        """A class tag wrapping the ENTIRE payload becomes the cue's
        style (like TTML's <p style="…">): the outermost span's refs
        hoist to cue level and its children splice up. Inner spans stay
        span-level. Only named classes promote — b/i/u pseudo styles
        keep their inline placement."""
        kids = cue.root.children
        core = [c for c in kids
                if not (c.kind == 'text' and not c.text.strip())]
        if len(core) != 1 or core[0].kind != 'span':
            return
        span = core[0]
        if not span.style_refs or \
                not any(not r.startswith('__') for r in span.style_refs):
            return
        cue.style_refs = list(dict.fromkeys(
            cue.style_refs + span.style_refs))
        if span.inline_style is not None:
            cue.inline_style = (
                span.inline_style.merged_over(cue.inline_style)
                if cue.inline_style is not None else span.inline_style)
        new_kids = []
        for c in kids:
            if c is span:
                new_kids.extend(span.children)
            else:
                new_kids.append(c)
        cue.root.children = new_kids

    def _parse_cue_settings(self, text: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for part in text.split():
            if ':' in part:
                k, v = part.split(':', 1)
                out[k.strip().lower()] = v.strip()
        return out

    # ------------------------------------------------------------------ #
    def _resolve_region(self, settings: Dict[str, str],
                        doc: SubtitleDocument) -> Optional[str]:
        """
        Map cue settings to a region id — using a declared REGION when
        referenced, else deriving/reusing a Region from the positional
        signature (vertical, line, position, size, align).
        """
        rid = settings.get('region')
        if rid and rid in doc.regions:
            return rid

        vertical = settings.get('vertical', '')          # '', 'rl', 'lr'
        line_raw = settings.get('line')
        pos_raw = settings.get('position')
        size_raw = settings.get('size')
        align = settings.get('align', 'center')

        sig = (vertical, line_raw, pos_raw, size_raw, align)
        if sig in self._sig_regions:
            return self._sig_regions[sig]

        region = self._derive_region(vertical, line_raw, pos_raw, size_raw, align)
        region.derived = True
        region.id = f'vtt_{len(self._sig_regions) + 1}'
        doc.regions[region.id] = region
        self._sig_regions[sig] = region.id
        return region.id

    @staticmethod
    def _split_setting(raw: Optional[str]) -> Tuple[Optional[float], Optional[str], bool]:
        """'80%,center' -> (80.0, 'center', True); '-2' -> (-2.0, None, False)."""
        if raw is None:
            return None, None, False
        parts = raw.split(',')
        alignment = parts[1].strip() if len(parts) > 1 else None
        v = parts[0].strip()
        is_pct = v.endswith('%')
        try:
            return float(v.rstrip('%')), alignment, is_pct
        except ValueError:
            return None, alignment, is_pct

    def _derive_region(self, vertical: str, line_raw, pos_raw, size_raw,
                       align: str) -> Region:
        region = Region()
        line_v, line_align, line_pct = self._split_setting(line_raw)
        pos_v, pos_align, _ = self._split_setting(pos_raw)
        size_v, _, _ = self._split_setting(size_raw)

        if vertical in ('rl', 'lr'):
            region.style.writing_mode = 'tbrl' if vertical == 'rl' else 'tblr'
            region.style.text_align = align
            # size = block extent along the vertical axis (height)
            region.height = Dim(size_v, '%') if size_v is not None else None
            region.width = None
            # 'line' positions the column: for rl, 0% = right edge.
            if line_v is not None:
                if not line_pct:
                    # integer line number: count columns from the edge; approx
                    # each column ~ 6% of width.
                    off = abs(line_v) * 6.0
                    if (line_v < 0) ^ (vertical == 'lr'):
                        region.x, region.x_edge = Dim(off, '%'), 'right'
                    else:
                        region.x, region.x_edge = Dim(off, '%'), 'left'
                else:
                    anchor = line_align or 'start'
                    if vertical == 'rl':
                        # line% measured from right
                        if anchor == 'end':
                            region.x, region.x_edge = Dim(100 - line_v, '%'), 'left'
                        else:
                            region.x, region.x_edge = Dim(line_v, '%'), 'right'
                    else:
                        if anchor == 'end':
                            region.x, region.x_edge = Dim(line_v, '%'), 'right'
                        else:
                            region.x, region.x_edge = Dim(line_v, '%'), 'left'
            else:
                region.x, region.x_edge = Dim(5, '%'), \
                    ('right' if vertical == 'rl' else 'left')
            # 'position' positions along the vertical axis
            if pos_v is not None:
                if pos_align == 'center':
                    region.y, region.y_edge = Dim(pos_v, '%'), 'center'
                elif pos_align in ('line-right', 'end'):
                    region.y, region.y_edge = Dim(100 - pos_v, '%'), 'bottom'
                else:
                    region.y, region.y_edge = Dim(pos_v, '%'), 'top'
            else:
                region.y, region.y_edge = Dim(0, '%'), 'top'
                region.style.display_align = {'start': 'before',
                                              'center': 'center',
                                              'end': 'after'}.get(align, 'before')
            return region

        # ----- horizontal cues ----------------------------------------- #
        region.style.text_align = align
        region.width = Dim(size_v, '%') if size_v is not None else None

        # line: vertical placement
        if line_v is not None:
            if not line_pct:
                # signed line number (counted in lines of ~6% height)
                off = (abs(line_v) - (1 if line_v < 0 else 0)) * 6.0
                if line_v < 0:
                    region.y, region.y_edge = Dim(off, '%'), 'bottom'
                else:
                    region.y, region.y_edge = Dim(off, '%'), 'top'
            else:
                anchor = line_align
                if anchor == 'center':
                    region.y, region.y_edge = Dim(line_v, '%'), 'center'
                elif anchor == 'end':
                    region.y, region.y_edge = Dim(100.0 - line_v, '%'), 'bottom'
                elif anchor == 'start':
                    region.y, region.y_edge = Dim(line_v, '%'), 'top'
                else:
                    # unspecified: players effectively bottom-anchor high
                    # percentages; mirror that so text sits where authored.
                    if line_v > 50:
                        region.y, region.y_edge = Dim(100.0 - line_v, '%'), 'bottom'
                    else:
                        region.y, region.y_edge = Dim(line_v, '%'), 'top'
        else:
            region.y, region.y_edge = Dim(5, '%'), 'bottom'
        region.style.display_align = 'after' if region.y_edge == 'bottom' else (
            'center' if region.y_edge == 'center' else 'before')

        # position: horizontal placement
        if pos_v is not None:
            anchor = pos_align
            if not anchor:
                anchor = {'left': 'line-left', 'start': 'line-left',
                          'right': 'line-right', 'end': 'line-right'}.get(align, 'center')
            if anchor in ('line-left', 'start', 'left'):
                region.x, region.x_edge = Dim(pos_v, '%'), 'left'
            elif anchor in ('line-right', 'end', 'right'):
                region.x, region.x_edge = Dim(100.0 - pos_v, '%'), 'right'
            else:
                region.x, region.x_edge = Dim(pos_v, '%'), 'center'
        else:
            region.x, region.x_edge = Dim(50, '%'), 'center'
        return region

    # ------------------------------------------------------------------ #
    # Payload
    # ------------------------------------------------------------------ #
    def _parse_payload(self, text: str, root: SpanNode, doc: SubtitleDocument):
        text = html.unescape(text)
        stack: List[SpanNode] = [root]

        def top() -> SpanNode:
            return stack[-1]

        for token in _TAG_RE.split(text):
            if token == '':
                continue
            if token.startswith('<') and token.endswith('>'):
                inner = token[1:-1].strip()
                if not inner:
                    continue
                if inner.startswith('/'):
                    name = inner[1:].split('.')[0].split()[0].lower()
                    # pop to matching open tag (tolerate mis-nesting)
                    for i in range(len(stack) - 1, 0, -1):
                        if stack[i].meta.get('vtt_tag') == name:
                            del stack[i:]
                            break
                    continue
                # timestamps <00:01.000>
                if _TS_TAG_RE.match(inner):
                    n = SpanNode(kind='span')
                    n.meta['timestamp'] = inner
                    top().children.append(n)
                    continue
                parts = inner.split('.')
                head = parts[0].split()
                name = head[0].lower()
                annot = inner[len(parts[0]):]  # ".class" chain (no annotation)
                classes = [c for c in parts[1:] if c]
                if name == 'br':
                    top().children.append(SpanNode.br())
                    continue
                node = SpanNode(kind='span')
                node.meta['vtt_tag'] = name
                if name == 'b':
                    node.inline_style = Style(font_weight='bold')
                elif name == 'i':
                    node.inline_style = Style(font_style='italic')
                elif name == 'u':
                    node.inline_style = Style(text_decoration='underline')
                elif name == 'c':
                    pass
                elif name == 'v':
                    voice = inner[len('v'):].split('.')[0].strip()
                    node.meta['voice'] = voice
                    vid = f'__voice_{voice}__'
                    if vid in doc.styles:
                        node.style_refs.append(vid)
                elif name == 'lang':
                    langv = head[1] if len(head) > 1 else ''
                    node.meta['lang'] = langv
                    lid = f'__lang_{langv}__'
                    if lid in doc.styles:
                        node.style_refs.append(lid)
                elif name == 'ruby':
                    node.inline_style = Style(ruby='container')
                elif name == 'rt':
                    node.inline_style = Style(ruby='text')
                else:
                    # unknown tag: transparent container
                    pass
                # tag styles from STYLE blocks (::cue(i) etc.)
                tag_style_id = f'__tag_{name}__'
                if tag_style_id in doc.styles:
                    node.style_refs.append(tag_style_id)
                # class styles
                if classes:
                    joined = '.'.join(classes)
                    if joined in doc.styles:
                        node.style_refs.append(joined)
                    else:
                        for cls in classes:
                            if cls in doc.styles:
                                node.style_refs.append(cls)
                    node.meta['classes'] = joined
                top().children.append(node)
                stack.append(node)
            else:
                for i, seg in enumerate(token.split('\n')):
                    if i > 0:
                        top().children.append(SpanNode.br())
                    if seg != '':
                        top().children.append(SpanNode.text_node(seg))
