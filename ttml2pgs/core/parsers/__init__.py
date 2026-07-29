"""
Subtitle parsers: format detection + dispatch.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ..model import SubtitleDocument

#: filename language tokens -> BCP-47-ish codes (extend as needed)
LANG_TOKENS = {
    'ja': 'ja', 'jp': 'ja', 'jpn': 'ja', 'ja-jp': 'ja',
    'en': 'en', 'eng': 'en', 'en-us': 'en', 'en-gb': 'en',
    'fr': 'fr', 'fre': 'fr', 'fra': 'fr',
    'de': 'de', 'deu': 'de', 'ger': 'de',
    'es': 'es', 'spa': 'es', 'es-419': 'es', 'es-es': 'es',
    'it': 'it', 'ita': 'it',
    'pt': 'pt', 'por': 'pt', 'pt-br': 'pt-BR', 'pt-pt': 'pt',
    'zh': 'zh', 'chi': 'zh', 'zho': 'zh',
    'zh-hans': 'zh-Hans', 'zh-cn': 'zh-Hans', 'chs': 'zh-Hans',
    'zh-hant': 'zh-Hant', 'zh-tw': 'zh-Hant', 'zh-hk': 'zh-Hant', 'cht': 'zh-Hant',
    'ko': 'ko', 'kor': 'ko',
    'ru': 'ru', 'rus': 'ru',
    'ar': 'ar', 'ara': 'ar',
    'th': 'th', 'tha': 'th',
    'vi': 'vi', 'vie': 'vi',
    'id': 'id', 'ind': 'id',
    'nl': 'nl', 'dut': 'nl', 'nld': 'nl',
    'pl': 'pl', 'pol': 'pl',
    'sv': 'sv', 'swe': 'sv',
    'no': 'no', 'nor': 'no', 'da': 'da', 'dan': 'da',
    'fi': 'fi', 'fin': 'fi', 'tr': 'tr', 'tur': 'tr',
    'he': 'he', 'heb': 'he', 'hi': 'hi', 'hin': 'hi',
}

# compact region forms streaming services use in filenames: jajp, enus…
for _k in [k for k in LANG_TOKENS if '-' in k]:
    LANG_TOKENS.setdefault(_k.replace('-', ''), LANG_TOKENS[_k])

SUBTITLE_EXTENSIONS = ('.ttml', '.ttml2', '.dfxp', '.xml', '.vtt', '.webvtt',
                       '.srt', '.t2p')


def normalize_language(lang: str) -> str:
    """
    Canonicalize a language tag: 'jp' → 'ja', 'jpn' → 'ja',
    'en-US' → 'en', 'zh-TW' → 'zh-Hant' … Unknown tags pass through
    with just the region stripped when the base is recognized.
    """
    if not lang:
        return ''
    l = lang.strip().replace('_', '-')
    low = l.lower()
    if low in LANG_TOKENS:
        return LANG_TOKENS[low]
    base = low.split('-')[0]
    if base in LANG_TOKENS:
        return LANG_TOKENS[base]
    return l


def detect_language_from_filename(path: str) -> str:
    """
    Extract a language from ``name.<lang>[.forced|.sdh|...].ext``.
    Scans dot-separated tokens right-to-left, skipping known flag tokens.
    """
    name = os.path.basename(path)
    parts = name.lower().split('.')
    if len(parts) < 2:
        return ''
    flags = {'forced', 'sdh', 'cc', 'hi', 'full', 'default'}
    for tok in reversed(parts[:-1]):
        if tok in LANG_TOKENS:
            return LANG_TOKENS[tok]
        if tok in flags:
            continue
    return ''


def detect_format(path: str, head: Optional[str] = None) -> str:
    """Return 'ttml' | 'vtt' | 'srt' | 't2p' | '' (unknown)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.ttml', '.ttml2', '.dfxp'):
        return 'ttml'
    if ext in ('.vtt', '.webvtt'):
        return 'vtt'
    if ext == '.srt':
        return 'srt'
    if ext == '.t2p':
        return 't2p'
    if head is None:
        try:
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
                head = f.read(4096)
        except OSError:
            return ''
    h = head.lstrip('﻿ \t\r\n')
    if h.startswith('WEBVTT'):
        return 'vtt'
    if h.startswith('<') and ('<tt' in h[:2048] or 'ttml' in h[:2048]):
        return 'ttml'
    if re.match(r'^\d+\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->', h):
        return 'srt'
    return ''


def load_subtitle(path: str) -> SubtitleDocument:
    """Parse *path* into a SubtitleDocument (format auto-detected)."""
    fmt = detect_format(path)
    if fmt == 'ttml':
        from .ttml import TTMLParser
        doc = TTMLParser().parse_file(path)
    elif fmt == 'vtt':
        from .vtt import VTTParser
        doc = VTTParser().parse_file(path)
    elif fmt == 'srt':
        from .srt import SRTParser
        doc = SRTParser().parse_file(path)
    elif fmt == 't2p':
        from ..project import load_project_document
        doc = load_project_document(path)
    else:
        raise ValueError(f"Unrecognized subtitle format: {path}")
    doc.language = normalize_language(doc.language)
    if not doc.language:
        doc.language = detect_language_from_filename(path) or 'en'
    for cue in doc.cues:
        cue.lang = normalize_language(cue.lang) or doc.language
    if fmt in ('vtt', 'srt'):
        # Rebuild flattened 漢字(かんじ) ruby once the language is known.
        from .ruby import apply_auto_ruby
        apply_auto_ruby(doc)
    if fmt != 't2p':
        bake_structural_roles(doc)
        prune_noop_styles(doc)
    n = dedupe_overlapping_duplicates(doc)
    if n:
        doc.metadata['deduplicated_cues'] = str(n)
    return doc


def bake_structural_roles(doc: SubtitleDocument) -> None:
    """
    Normalize ruby / tate-chū-yoko structure: resolve each span's ruby
    role (and textCombine) from its style refs and bake it into the
    span's *inline* style. Afterwards the structure is self-contained —
    named styles no longer carry any structural meaning, identically
    for TTML, WebVTT and auto-ruby SRT.
    """
    from ..model import Style

    def bake(span) -> None:
        spec = doc.specified_style(span.style_refs, span.inline_style)
        updates = {}
        if spec.ruby and (span.inline_style is None or
                          span.inline_style.ruby is None):
            updates['ruby'] = spec.ruby
        for attr in ('ruby_position', 'ruby_align', 'ruby_scale'):
            v = getattr(spec, attr)
            if v is not None and (span.inline_style is None or
                                  getattr(span.inline_style, attr) is None):
                updates[attr] = v
        tc = spec.text_combine
        if tc and tc != 'none' and (span.inline_style is None or
                                    span.inline_style.text_combine is None):
            updates['text_combine'] = tc
        if updates:
            if span.inline_style is None:
                span.inline_style = Style()
            for k, v in updates.items():
                setattr(span.inline_style, k, v)

    def walk(node) -> None:
        for ch in node.children:
            if ch.kind == 'span':
                bake(ch)
                walk(ch)

    for cue in doc.cues:
        walk(cue.root)

    # roles now live on the spans — clear the vestigial fields from named
    # styles (styles with other content survive minus the role fields;
    # role-only styles become empty and are pruned right after)
    for st in doc.styles.values():
        st.ruby = None
        st.ruby_align = None
        st.ruby_position = None
        st.ruby_scale = None


#: font sizes that are pure no-op multipliers of the parent size
_NOOP_SIZES = {(100.0, '%'), (1.0, 'em')}


def prune_noop_styles(doc: SubtitleDocument) -> int:
    """
    Drop named styles that no longer do anything once structure is baked
    inline: styles whose every set property is a ruby-role field, a
    100 % / 1em font size, or an explicit 'normal' weight/style.

    Edge case guarded: an explicit ``normal`` CAN matter — it cancels a
    bold/italic inherited from another style or the initials. It only
    counts as a no-op when nothing in the document sets bold/italic at
    all. Returns the number of styles removed.
    """
    import dataclasses as _dc

    any_bold = any(s.font_weight not in (None, 'normal')
                   for s in list(doc.styles.values()) + [doc.initial]) or \
        any(r.style.font_weight not in (None, 'normal')
            for r in doc.regions.values())
    any_italic = any(s.font_style not in (None, 'normal')
                     for s in list(doc.styles.values()) + [doc.initial]) or \
        any(r.style.font_style not in (None, 'normal')
            for r in doc.regions.values())

    role_fields = {'ruby', 'ruby_align', 'ruby_position', 'ruby_scale'}

    def is_noop(st) -> bool:
        if st.parent_ids:
            return False
        for f in _dc.fields(st):
            if f.name in ('id', 'parent_ids'):
                continue
            v = getattr(st, f.name)
            if v is None:
                continue
            if f.name in role_fields:
                continue
            if f.name == 'font_size' and (v.value, v.unit) in _NOOP_SIZES:
                continue
            if f.name == 'font_weight' and v == 'normal' and not any_bold:
                continue
            if f.name == 'font_style' and v == 'normal' and not any_italic:
                continue
            return False
        # nothing meaningful set (fully-empty styles included — role-only
        # styles become empty once roles are baked inline)
        return True

    victims = [sid for sid, st in doc.styles.items() if is_noop(st)]
    if not victims:
        return 0
    vs = set(victims)
    for sid in victims:
        doc.styles.pop(sid, None)

    def strip(refs):
        return [r for r in refs if r not in vs]

    for s in doc.styles.values():
        s.parent_ids = strip(s.parent_ids)
    for r in doc.regions.values():
        r.style_refs = strip(r.style_refs)

    def walk(node):
        node.style_refs = strip(node.style_refs)
        for ch in node.children:
            walk(ch)

    for cue in doc.cues:
        cue.style_refs = strip(cue.style_refs)
        walk(cue.root)
    doc.metadata['pruned_styles'] = ' '.join(victims)
    return len(victims)


def dedupe_overlapping_duplicates(doc: SubtitleDocument) -> int:
    """
    Condense identical cues whose time ranges overlap.

    Segmented VTT streams (HLS chunks) repeat the trailing cue of one
    chunk at the head of the next; when both survive stitching, the cue
    is rendered twice on screen — invisible when fully opaque, but with
    alpha the stacked copies double up and look less transparent. Cues
    with identical content, region and styling whose intervals *overlap*
    are merged into one cue spanning their union. Identical adjacent
    (touching, non-overlapping) cues are left alone — that's a
    legitimate re-display.

    Returns the number of cues removed.
    """
    from ..project import cue_to_json
    import json

    def signature(cue):
        d = cue_to_json(cue)
        d.pop('begin', None)
        d.pop('end', None)
        d.pop('sid', None)
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    by_sig = {}
    for cue in doc.cues:
        by_sig.setdefault(signature(cue), []).append(cue)

    removed = set()
    for group in by_sig.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda c: (c.begin_ms, c.end_ms))
        keeper = group[0]
        for cue in group[1:]:
            if cue.begin_ms < keeper.end_ms - 0.001:     # strict overlap
                keeper.end_ms = max(keeper.end_ms, cue.end_ms)
                removed.add(id(cue))
            else:
                keeper = cue
    if removed:
        doc.cues = [c for c in doc.cues if id(c) not in removed]
    return len(removed)
