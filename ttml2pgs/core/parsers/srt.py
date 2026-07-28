"""
SubRip (.srt) parser.

SRT has no formal spec; this follows the de-facto rules:
* numbered blocks, ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` (dot tolerated),
* inline tags: <i> <b> <u> <font color="..." face="..." size="...">,
* SSA-style position tags ``{\\an1}``..``{\\an9}`` at line start map to
  the nine screen anchor positions (numpad layout),
* ``{\\pos(x,y)}`` (rare) is honored assuming a 384x288 SSA play field.

Positional info becomes derived Regions so SRT behaves exactly like the
other formats downstream.
"""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional

from ..colors import parse_color
from ..model import Cue, Region, SpanNode, Style, SubtitleDocument
from ..units import Dim

_TIME_RE = re.compile(
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*'
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})')
_TAG_RE = re.compile(r'(<[^>]+>|\{\\[^}]*\})')
_AN_RE = re.compile(r'\{\\an?(\d)\}')
_POS_RE = re.compile(r'\{\\pos\((\d+(?:\.\d+)?),(\d+(?:\.\d+)?)\)\}')


def _ts(h, m, s, ms) -> float:
    return ((int(h) * 3600 + int(m) * 60 + int(s)) * 1000
            + int(str(ms).ljust(3, '0')))


class SRTParser:
    def parse_file(self, path: str) -> SubtitleDocument:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            content = f.read()
        doc = self.parse_string(content)
        doc.source_path = path
        return doc

    def parse_string(self, content: str) -> SubtitleDocument:
        doc = SubtitleDocument(source_format='srt')
        self._an_regions: Dict[str, str] = {}
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        for block in re.split(r'\n{2,}', content):
            lines = [l for l in block.split('\n') if l.strip() != '']
            if not lines:
                continue
            ti = next((i for i, l in enumerate(lines) if _TIME_RE.search(l)), -1)
            if ti < 0:
                continue
            m = _TIME_RE.search(lines[ti])
            begin = _ts(*m.groups()[:4])
            end = _ts(*m.groups()[4:])
            payload = '\n'.join(lines[ti + 1:])
            if not payload.strip():
                continue
            cue = Cue(begin_ms=begin, end_ms=end)
            if ti > 0 and lines[0].strip().isdigit():
                cue.source_id = lines[0].strip()
            payload = self._extract_position(payload, cue, doc)
            self._parse_payload(payload, cue.root)
            if cue.root.children:
                doc.cues.append(cue)
        return doc

    # ------------------------------------------------------------------ #
    # Position handling
    # ------------------------------------------------------------------ #
    #: numpad anchor -> (x%, x_edge, y%, y_edge, text_align, display_align)
    _AN_MAP = {
        '1': (5, 'left', 5, 'bottom', 'left', 'after'),
        '2': (50, 'center', 5, 'bottom', 'center', 'after'),
        '3': (5, 'right', 5, 'bottom', 'right', 'after'),
        '4': (5, 'left', 50, 'center', 'left', 'center'),
        '5': (50, 'center', 50, 'center', 'center', 'center'),
        '6': (5, 'right', 50, 'center', 'right', 'center'),
        '7': (5, 'left', 5, 'top', 'left', 'before'),
        '8': (50, 'center', 5, 'top', 'center', 'before'),
        '9': (5, 'right', 5, 'top', 'right', 'before'),
    }

    def _extract_position(self, payload: str, cue: Cue,
                          doc: SubtitleDocument) -> str:
        man = _AN_RE.search(payload)
        key: Optional[str] = None
        region: Optional[Region] = None
        if man:
            an = man.group(1)
            key = f'an{an}'
            if key not in self._an_regions:
                x, xe, y, ye, ta, da = self._AN_MAP.get(an, self._AN_MAP['2'])
                region = Region(id=f'srt_{key}', derived=True,
                                x=Dim(x, '%'), x_edge=xe,
                                y=Dim(y, '%'), y_edge=ye,
                                width=Dim(90, '%'), height=None)
                region.style.text_align = ta
                region.style.display_align = da
            payload = _AN_RE.sub('', payload)
        else:
            mpos = _POS_RE.search(payload)
            if mpos:
                x = float(mpos.group(1)) / 384.0 * 100.0
                y = float(mpos.group(2)) / 288.0 * 100.0
                key = f'pos{round(x)}x{round(y)}'
                if key not in self._an_regions:
                    region = Region(id=f'srt_{key}', derived=True,
                                    x=Dim(x, '%'), x_edge='center',
                                    y=Dim(y, '%'), y_edge='center',
                                    width=None, height=None)
                    region.style.text_align = 'center'
                    region.style.display_align = 'center'
                payload = _POS_RE.sub('', payload)

        if key:
            if region is not None:
                doc.regions[region.id] = region
                self._an_regions[key] = region.id
            cue.region_id = self._an_regions[key]
        # strip any other unsupported override blocks
        payload = re.sub(r'\{\\[^}]*\}', '', payload)
        return payload

    # ------------------------------------------------------------------ #
    def _parse_payload(self, text: str, root: SpanNode):
        text = html.unescape(text)
        stack: List[SpanNode] = [root]

        for token in _TAG_RE.split(text):
            if token == '':
                continue
            if token.startswith('{'):
                continue  # leftover override blocks already stripped
            if token.startswith('<') and token.endswith('>'):
                inner = token[1:-1].strip()
                closing = inner.startswith('/')
                name = inner.lstrip('/').split()[0].lower() if inner.lstrip('/') else ''
                if closing:
                    for i in range(len(stack) - 1, 0, -1):
                        if stack[i].meta.get('srt_tag') == name:
                            del stack[i:]
                            break
                    continue
                if name == 'br':
                    stack[-1].children.append(SpanNode.br())
                    continue
                node = SpanNode(kind='span')
                node.meta['srt_tag'] = name
                if name == 'i':
                    node.inline_style = Style(font_style='italic')
                elif name == 'b':
                    node.inline_style = Style(font_weight='bold')
                elif name == 'u':
                    node.inline_style = Style(text_decoration='underline')
                elif name == 'font':
                    st = Style()
                    mc = re.search(r'color\s*=\s*"?([^"\s>]+)"?', inner, re.I)
                    if mc:
                        st.color = parse_color(mc.group(1))
                    mf = re.search(r'face\s*=\s*"?([^">]+)"?', inner, re.I)
                    if mf:
                        st.font_family = [f.strip() for f in mf.group(1).split(',')]
                    node.inline_style = st
                else:
                    pass  # unknown tag: transparent
                stack[-1].children.append(node)
                stack.append(node)
            else:
                for i, seg in enumerate(token.split('\n')):
                    if i > 0:
                        stack[-1].children.append(SpanNode.br())
                    if seg:
                        stack[-1].children.append(SpanNode.text_node(seg))
