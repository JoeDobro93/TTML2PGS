"""
Native project format (.t2p) — lossless JSON persistence of a document
plus its editing state (video binding, per-language overrides, retime,
canvas policy). Lets a user save a tweaked subtitle and reload it later
exactly as configured.
"""

from __future__ import annotations

import json
from dataclasses import fields
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

from .colors import parse_color, to_hex
from .model import (Cue, Region, Shadow, SpanNode, Style, SubtitleDocument)
from .overrides import OverrideSet
from .units import Dim

FORMAT_VERSION = 2


# --------------------------------------------------------------------------- #
# Style / value serialization
# --------------------------------------------------------------------------- #

def _val_to_json(v: Any) -> Any:
    if isinstance(v, Dim):
        return {'_dim': str(v), 'u': v.unit}
    if isinstance(v, tuple) and len(v) == 4 and all(isinstance(c, int) for c in v):
        return {'_rgba': to_hex(v)}
    if isinstance(v, tuple):
        return {'_tuple': [_val_to_json(x) for x in v]}
    if isinstance(v, Shadow):
        return {'_shadow': {
            'x': str(v.offset_x), 'y': str(v.offset_y), 'b': str(v.blur),
            'c': to_hex(v.color), 'a': v.alpha}}
    if isinstance(v, list):
        return [_val_to_json(x) for x in v]
    return v


def _val_from_json(v: Any) -> Any:
    if isinstance(v, dict):
        if '_dim' in v:
            return Dim.parse(v['_dim'], default_unit=v.get('u', 'px'))
        if '_rgba' in v:
            return parse_color(v['_rgba'])
        if '_tuple' in v:
            return tuple(_val_from_json(x) for x in v['_tuple'])
        if '_shadow' in v:
            s = v['_shadow']
            return Shadow(Dim.parse(s['x']) or Dim(2, 'px'),
                          Dim.parse(s['y']) or Dim(2, 'px'),
                          Dim.parse(s['b']) or Dim(0, 'px'),
                          parse_color(s['c']) or (0, 0, 0, 255),
                          float(s.get('a', 1.0)))
    if isinstance(v, list):
        return [_val_from_json(x) for x in v]
    return v


def style_to_json(st: Style) -> dict:
    out = {}
    for f in fields(Style):
        v = getattr(st, f.name)
        if v is None or f.name == 'id' and not v:
            continue
        if f.name == 'parent_ids' and not v:
            continue
        out[f.name] = _val_to_json(v)
    return out


def style_from_json(d: dict) -> Style:
    st = Style()
    valid = {f.name for f in fields(Style)}
    for k, v in (d or {}).items():
        if k in valid:
            setattr(st, k, _val_from_json(v))
    return st


def region_to_json(r: Region) -> dict:
    return {
        'id': r.id, 'x': str(r.x), 'x_edge': r.x_edge,
        'y': str(r.y), 'y_edge': r.y_edge,
        'width': str(r.width) if r.width else None,
        'height': str(r.height) if r.height else None,
        'style': style_to_json(r.style),
        'style_refs': r.style_refs,
        'derived': r.derived,
    }


def region_from_json(d: dict) -> Region:
    r = Region(id=d.get('id', ''))
    r.x = Dim.parse(d.get('x', '50%'), '%') or Dim(50, '%')
    r.y = Dim.parse(d.get('y', '90%'), '%') or Dim(90, '%')
    r.x_edge = d.get('x_edge', 'center')
    r.y_edge = d.get('y_edge', 'center')
    r.width = Dim.parse(d['width'], '%') if d.get('width') else None
    r.height = Dim.parse(d['height'], '%') if d.get('height') else None
    r.style = style_from_json(d.get('style', {}))
    r.style_refs = list(d.get('style_refs', []))
    r.derived = bool(d.get('derived', False))
    return r


def node_to_json(n: SpanNode) -> dict:
    out: Dict[str, Any] = {'k': n.kind}
    if n.kind == 'text':
        out['t'] = n.text
        return out
    if n.kind == 'br':
        return out
    if n.style_refs:
        out['refs'] = n.style_refs
    if n.inline_style is not None:
        out['style'] = style_to_json(n.inline_style)
    if n.meta:
        out['meta'] = n.meta
    if n.children:
        out['c'] = [node_to_json(c) for c in n.children]
    return out


def node_from_json(d: dict) -> SpanNode:
    n = SpanNode(kind=d.get('k', 'span'))
    if n.kind == 'text':
        n.text = d.get('t', '')
        return n
    n.style_refs = list(d.get('refs', []))
    if 'style' in d:
        n.inline_style = style_from_json(d['style'])
    n.meta = dict(d.get('meta', {}))
    n.children = [node_from_json(c) for c in d.get('c', [])]
    return n


def cue_to_json(c: Cue) -> dict:
    out = {
        'begin': c.begin_ms, 'end': c.end_ms,
        'root': node_to_json(c.root),
    }
    if c.region_id:
        out['region'] = c.region_id
    if c.style_refs:
        out['refs'] = c.style_refs
    if c.inline_style is not None:
        out['style'] = style_to_json(c.inline_style)
    if not c.enabled:
        out['enabled'] = False
    if c.source_id:
        out['sid'] = c.source_id
    if c.lang:
        out['lang'] = c.lang
    return out


def cue_from_json(d: dict) -> Cue:
    c = Cue(begin_ms=float(d.get('begin', 0)), end_ms=float(d.get('end', 0)))
    c.region_id = d.get('region')
    c.style_refs = list(d.get('refs', []))
    if 'style' in d:
        c.inline_style = style_from_json(d['style'])
    c.root = node_from_json(d.get('root', {'k': 'root'}))
    c.root.kind = 'root'
    c.enabled = bool(d.get('enabled', True))
    c.source_id = d.get('sid', '')
    c.lang = d.get('lang', '')
    return c


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #

def document_to_json(doc: SubtitleDocument) -> dict:
    return {
        'source_path': doc.source_path,
        'source_format': doc.source_format,
        'language': doc.language,
        'px_width': doc.px_width, 'px_height': doc.px_height,
        'cell_rows': doc.cell_rows, 'cell_cols': doc.cell_cols,
        'fps': [doc.fps.numerator, doc.fps.denominator] if doc.fps else None,
        'styles': {k: style_to_json(v) for k, v in doc.styles.items()},
        'regions': {k: region_to_json(v) for k, v in doc.regions.items()},
        'initial': style_to_json(doc.initial),
        'cues': [cue_to_json(c) for c in doc.cues],
        'metadata': doc.metadata,
    }


def document_from_json(d: dict) -> SubtitleDocument:
    doc = SubtitleDocument()
    doc.source_path = d.get('source_path', '')
    doc.source_format = d.get('source_format', '')
    doc.language = d.get('language', '')
    doc.px_width = int(d.get('px_width', 1920))
    doc.px_height = int(d.get('px_height', 1080))
    doc.cell_rows = int(d.get('cell_rows', 15))
    doc.cell_cols = int(d.get('cell_cols', 32))
    fps = d.get('fps')
    doc.fps = Fraction(fps[0], fps[1]) if fps else None
    doc.styles = {k: style_from_json(v)
                  for k, v in d.get('styles', {}).items()}
    for k, v in doc.styles.items():
        v.id = k
    doc.regions = {k: region_from_json(v)
                   for k, v in d.get('regions', {}).items()}
    doc.initial = style_from_json(d.get('initial', {}))
    doc.initial.id = '__initial__'
    doc.cues = [cue_from_json(c) for c in d.get('cues', [])]
    doc.metadata = dict(d.get('metadata', {}))
    return doc


# --------------------------------------------------------------------------- #
# Project files
# --------------------------------------------------------------------------- #

def save_project(path: str, doc: SubtitleDocument,
                 overrides: Optional[OverrideSet] = None,
                 extras: Optional[dict] = None):
    """extras: video_path, target_fps [num,den], retime info, ui state…"""
    payload = {
        'format': 'ttml2pgs-project',
        'version': FORMAT_VERSION,
        'document': document_to_json(doc),
        'overrides': overrides.to_dict() if overrides else None,
        'extras': extras or {},
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def load_project(path: str) -> Tuple[SubtitleDocument, Optional[OverrideSet], dict]:
    with open(path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    if payload.get('format') != 'ttml2pgs-project':
        raise ValueError('Not a ttml2pgs project file')
    doc = document_from_json(payload.get('document', {}))
    doc.source_format = doc.source_format or 't2p'
    ov = OverrideSet.from_dict(payload['overrides']) \
        if payload.get('overrides') else None
    return doc, ov, payload.get('extras', {})


def load_project_document(path: str) -> SubtitleDocument:
    doc, _, _ = load_project(path)
    if not doc.source_path:
        doc.source_path = path
    return doc
