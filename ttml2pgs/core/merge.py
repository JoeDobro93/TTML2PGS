"""
Merge mode: combine two subtitle documents (e.g. Japanese dialogue +
English forced signs) into one document that renders both.

Design notes
------------
* Cues keep their SOURCE language (``cue.lang``) so the per-language
  override sets (fonts, size, padding…) still apply per line; the
  merged document's language — and thus the mux track tag and the
  Default-profile pick — is the *primary* file's.
* The secondary document's styles and regions are renamed with a
  ``.<lang>`` suffix before merging so nothing collides and the
  Styles/Regions tabs show which language each belongs to.
* Document initials come from the primary file. If the secondary file
  carried initials of its own, they are preserved as a named style
  (``__init.<lang>``) prepended to each secondary cue so its rendering
  doesn't change.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Dict, List, Optional, Tuple

from .model import SubtitleDocument
from .video import is_forced_name, subtitle_stem


# --------------------------------------------------------------------------- #
# Language variants ("en" vs "en (forced)")
# --------------------------------------------------------------------------- #

def lang_variant(sub_path: str, language: str) -> str:
    """Key identifying a subtitle's language option, forced-distinct:
    'ja', 'en', 'en+forced'…"""
    lang = (language or 'und').strip() or 'und'
    return f'{lang}+forced' if is_forced_name(sub_path) else lang


def variant_label(variant: str) -> str:
    return variant.replace('+forced', ' (forced)')


def episode_stem(sub_path: str) -> str:
    """Grouping key for 'the same episode': the filename with language
    and flag tokens stripped, case-normalized."""
    return subtitle_stem(sub_path).lower()


def plan_merge(all_paths_langs: List[Tuple[str, str]],
               selected_paths: List[str]) -> Dict[str, Dict[str, str]]:
    """
    Resolve which files take part in a merge.

    all_paths_langs — (sub_path, language) for EVERY open file.
    selected_paths  — the files the user highlighted.

    Returns {episode_stem: {variant: sub_path}} for every episode the
    selection touches, considering ALL open files of those episodes
    (the user only needs to highlight one file per episode).
    """
    wanted = {episode_stem(p) for p in selected_paths}
    groups: Dict[str, Dict[str, str]] = {}
    for path, lang in all_paths_langs:
        stem = episode_stem(path)
        if stem in wanted:
            groups.setdefault(stem, {})[lang_variant(path, lang)] = path
    return groups


def common_variants(groups: Dict[str, Dict[str, str]]) -> List[str]:
    """Variants present in EVERY episode group (mergeable choices)."""
    if not groups:
        return []
    sets = [set(v.keys()) for v in groups.values()]
    common = set.intersection(*sets)
    order: List[str] = []
    for v in groups[next(iter(groups))]:
        if v in common:
            order.append(v)
    return order


def all_variants(groups: Dict[str, Dict[str, str]]) -> List[str]:
    seen: List[str] = []
    for g in groups.values():
        for v in g:
            if v not in seen:
                seen.append(v)
    return seen


# --------------------------------------------------------------------------- #
# Document merge
# --------------------------------------------------------------------------- #

def _suffix_ids(doc: SubtitleDocument, suffix: str,
                taken_styles, taken_regions):
    """Rename every style/region in `doc` to '<id>.<suffix>' (made
    unique against the taken sets), cascading through references."""
    for sid in list(doc.styles.keys()):
        new = f'{sid}.{suffix}'
        n = 2
        while new in taken_styles or new in doc.styles:
            new = f'{sid}.{suffix}_{n}'
            n += 1
        doc.rename_style(sid, new)
    for rid in list(doc.regions.keys()):
        new = f'{rid}.{suffix}'
        n = 2
        while new in taken_regions or new in doc.regions:
            new = f'{rid}.{suffix}_{n}'
            n += 1
        doc.rename_region(rid, new)


def merge_documents(primary: SubtitleDocument,
                    secondary: SubtitleDocument,
                    primary_lang: str = '',
                    secondary_lang: str = '') -> SubtitleDocument:
    """
    Return a new document containing both files' cues.

    The merged document speaks the PRIMARY language (initials, language
    tag); secondary cues keep their own ``cue.lang`` so per-language
    overrides apply line by line.
    """
    p_lang = (primary_lang or primary.language or 'und').split('+')[0]
    s_lang = (secondary_lang or secondary.language or 'und').split('+')[0]

    doc = copy.deepcopy(primary)
    sec = copy.deepcopy(secondary)
    doc.language = p_lang
    for cue in doc.cues:
        cue.lang = cue.lang or p_lang

    # secondary ids get a language suffix (unique against the primary)
    suffix = re.sub(r'[^A-Za-z0-9_-]', '', s_lang) or 'sec'
    if s_lang == p_lang:
        suffix += '2'
    _suffix_ids(sec, suffix, set(doc.styles), set(doc.regions))

    # secondary initials survive as a style on its cues (doc-level
    # initials are the primary's)
    init_props = sec.initial.set_props()
    if init_props:
        init_id = f'__init.{suffix}'
        st = copy.deepcopy(sec.initial)
        st.id = init_id
        sec.styles[init_id] = st
        for cue in sec.cues:
            cue.style_refs = [init_id] + list(cue.style_refs)

    doc.styles.update(sec.styles)
    doc.regions.update(sec.regions)
    for cue in sec.cues:
        cue.lang = cue.lang or s_lang
        doc.cues.append(cue)
    return doc


def merged_display_name(primary_path: str, secondary_path: str) -> str:
    return (f'{os.path.basename(primary_path)} | '
            f'{os.path.basename(secondary_path)}')


def merged_out_path(primary_path: str, video_path: Optional[str],
                    primary_variant: str, secondary_variant: str) -> str:
    """
    Default .sup name for a merged pair: the episode stem plus BOTH
    language tags, so 'Episode01.ja+en.forced.sup' says exactly what's
    inside (and can't collide with the single-language outputs).
    """
    base_dir = os.path.dirname(video_path or primary_path)
    stem = subtitle_stem(primary_path)
    ptag = primary_variant.replace('+forced', '.forced')
    stag = secondary_variant.replace('+forced', '.forced')
    return os.path.join(base_dir, f'{stem}.{ptag}+{stag}.sup')


# --------------------------------------------------------------------------- #
# Timestamp snapping
# --------------------------------------------------------------------------- #

def snap_secondary_timestamps(doc: SubtitleDocument,
                              primary_lang: str,
                              threshold_ms: float = 500.0) -> int:
    """
    Align secondary-language cue edges to the primary language's cue
    boundaries.

    For every cue NOT in the primary language that overlaps (or nearly
    overlaps) primary cues: each endpoint moves to the nearest primary
    start/end within `threshold_ms`; endpoints with no boundary in
    range stay put. Snaps that would invert or zero the cue are
    skipped. Returns the number of cues changed.
    """
    p_lang = (primary_lang or doc.language or '').split('-')[0]

    def is_primary(c):
        return ((c.lang or doc.language or '').split('-')[0]) == p_lang

    prim = [c for c in doc.cues if is_primary(c)]
    if not prim:
        return 0
    bounds = sorted({t for c in prim for t in (c.begin_ms, c.end_ms)})

    def nearest(t: float) -> Optional[float]:
        best, bd = None, threshold_ms
        # bounds is sorted; a linear scan is fine at subtitle scales
        for b in bounds:
            d = abs(b - t)
            if d <= bd:
                best, bd = b, d
            elif b - t > threshold_ms:
                break
        return best

    changed = 0
    for cue in doc.cues:
        if is_primary(cue):
            continue
        # near a primary cue at all? (overlap, or an edge within range)
        touches = any(cue.begin_ms < p.end_ms + threshold_ms and
                      p.begin_ms - threshold_ms < cue.end_ms
                      for p in prim)
        if not touches:
            continue
        nb, ne = nearest(cue.begin_ms), nearest(cue.end_ms)
        new_b = nb if nb is not None else cue.begin_ms
        new_e = ne if ne is not None else cue.end_ms
        if new_e <= new_b:               # would invert/zero — best effort:
            if nb is not None and cue.end_ms > new_b:
                new_e = cue.end_ms       # keep original end
            elif ne is not None and new_e > cue.begin_ms:
                new_b = cue.begin_ms     # keep original start
            else:
                continue
        if (new_b, new_e) != (cue.begin_ms, cue.end_ms):
            cue.begin_ms, cue.end_ms = new_b, new_e
            changed += 1
    return changed
