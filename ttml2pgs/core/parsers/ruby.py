"""
Auto-ruby detection for flattened Japanese subtitles (VTT/SRT).

Streaming exports often lose real ruby markup and flatten it to
``漢字(かんじ)``. For documents flagged Japanese:

* only the ASCII parenthesis ``(`` marks a potential annotation
  (full-width ``（）`` is real text — stage directions like ``（笑）``
  stay untouched),
* the annotation must be pure kana (readings) — otherwise it is left
  as normal parenthesized text,
* the base is everything back from the ``(`` to the nearest preceding
  ASCII space or the start of the line; that delimiter space is a marker
  only and is removed from the rendered output (full-width layout spaces
  are kept and simply bound the base).
"""

from __future__ import annotations

import re
from typing import List

from ..model import SpanNode, Style, SubtitleDocument

_KANA_RE = re.compile(r'^[぀-ヿーゝゞヽヾ・]+$')


def detect_doc_lang_hint(doc: SubtitleDocument) -> str:
    """Heuristic: sniff Japanese from cue text (hiragana presence)."""
    sample = ''.join(c.plain_text() for c in doc.cues[:80])
    if re.search(r'[぀-ゟ]', sample):
        return 'ja'
    return ''


def apply_auto_ruby(doc: SubtitleDocument):
    """Rewrite flattened ruby into ruby span structures (Japanese only)."""
    lang = (doc.language or detect_doc_lang_hint(doc)) or ''
    if not lang.lower().startswith('ja') and lang.lower() != 'jp':
        return
    for cue in doc.cues:
        _walk(cue.root)


def _walk(node: SpanNode):
    new_children: List[SpanNode] = []
    for child in node.children:
        if child.kind == 'text':
            new_children.extend(_split_text(child))
        else:
            if child.kind == 'span' and (child.inline_style is None or
                                         child.inline_style.ruby is None):
                _walk(child)
            new_children.append(child)
    node.children = new_children


def _make_ruby(base: str, reading: str) -> SpanNode:
    container = SpanNode(kind='span', inline_style=Style(ruby='container'))
    bnode = SpanNode(kind='span', inline_style=Style(ruby='base'))
    bnode.children.append(SpanNode.text_node(base))
    tnode = SpanNode(kind='span', inline_style=Style(ruby='text'))
    tnode.children.append(SpanNode.text_node(reading))
    container.children = [bnode, tnode]
    container.meta['auto_ruby'] = '1'
    return container


def _split_text(textnode: SpanNode) -> List[SpanNode]:
    text = textnode.text
    out: List[SpanNode] = []
    buf = ''
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '(':                                   # ASCII only
            close = text.find(')', i + 1)
            if close > i + 1:
                reading = text[i + 1:close]
                if _KANA_RE.match(reading):
                    # base: back to the nearest ASCII space marker, else a
                    # full-width space boundary (kept), else line start.
                    marker = buf.rfind(' ')
                    fw = buf.rfind('　')
                    if marker >= fw:
                        pre, base = ((buf[:marker], buf[marker + 1:])
                                     if marker != -1 else ('', buf))
                    else:
                        pre, base = buf[:fw + 1], buf[fw + 1:]
                    if base:
                        if pre:
                            out.append(SpanNode.text_node(pre))
                        out.append(_make_ruby(base, reading))
                        buf = ''
                        i = close + 1
                        continue
        buf += ch
        i += 1
    if buf:
        out.append(SpanNode.text_node(buf))
    return out
