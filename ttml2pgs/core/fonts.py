"""
Font discovery and language-aware selection.

The MUST-HAVE this module exists for: **CJK correctness**. A Japanese
subtitle must never be drawn with a Chinese font's glyph variants for
unified Han codepoints (直/骨/学…), and vice versa. Selection therefore
works in two dimensions:

1. The author's requested family list (from the subtitle file), matched
   against every name-table family name (including localized names like
   ``游ゴシック``), with TTML generic families mapped per language.
2. A per-language fallback chain appended after the requested families,
   ordered so that fonts *designed for that language* rank first. Fonts
   are classified by name markers (``JP``, ``SC``, ``TC``, ``KR``,
   ``Meiryo``, ``YaHei``…) so e.g. ``Noto Sans CJK JP`` outranks
   ``WenQuanYi`` for ``lang=ja`` even though both cover the codepoints.

Per-run resolution then walks the candidate list and picks the first face
covering each character, splitting runs when the face changes.

The disk index is cached (path+mtime+size keyed) so startup after the
first scan is instant.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import freetype
import uharfbuzz as hb

_FONT_EXTS = ('.ttf', '.otf', '.ttc', '.otc')


def _norm_family(name: str) -> str:
    """Normalize a family name for matching: casefold, strip spaces/dashes."""
    return ''.join(ch for ch in unicodedata.normalize('NFKC', name).casefold()
                   if ch not in ' -_')


@dataclass
class FaceRecord:
    path: str
    index: int
    families: List[str]
    weight: int = 400
    italic: bool = False
    monospace: bool = False

    def key(self) -> Tuple[str, int]:
        return (self.path, self.index)


# --------------------------------------------------------------------------- #
# Language classification markers
# --------------------------------------------------------------------------- #

_LANG_MARKERS: Dict[str, List[str]] = {
    'ja': ['cjkjp', 'sansjp', 'serifjp', 'notosansjp', 'notoserifjp',
           'sourcehansansjp', 'sourcehanserifjp', 'hiragino', 'yugothic',
           'yumincho', 'meiryo', 'msgothic', 'mspgothic', 'msmincho',
           'mspmincho', 'osaka', 'kaku', 'mincho', 'gothicjp', 'ipagothic',
           'ipamincho', 'takao', 'unifontjp', 'genshin', 'biz udgothic',
           'bizud', 'udev', 'klee', 'zenkaku', 'harenosora'],
    'zh-hans': ['cjksc', 'sanssc', 'serifsc', 'notosanssc', 'notoserifsc',
                'sourcehansanssc', 'sourcehansanscn', 'yahei', 'simsun',
                'simhei', 'simkai', 'simfang', 'dengxian', 'fangsong',
                'wenquanyi', 'wqy', 'pingfangsc', 'songti', 'heiti'],
    'zh-hant': ['cjktc', 'sanstc', 'seriftc', 'notosanstc', 'notoseriftc',
                'sourcehansanstw', 'jhenghei', 'mingliu', 'pmingliu',
                'dfkai', 'kaiu', 'pingfangtc', 'pingfanghk', 'cjkhk',
                'sanshk', 'notosanshk'],
    'ko': ['cjkkr', 'sanskr', 'serifkr', 'notosanskr', 'notoserifkr',
           'sourcehansanskr', 'malgun', 'gulim', 'batang', 'dotum',
           'nanum', 'applesdgothic'],
}

#: default family preference per language, used for generic families and
#: appended as fallback after author-requested families.
_LANG_DEFAULT_STACKS: Dict[str, List[str]] = {
    # CJK stacks list MEDIUM-named families FIRST: 400-weight CJK faces
    # are print designs that render anemic as subtitles, and the medium
    # preference must win across families (a machine with only Noto
    # Regular + Yu Gothic Medium installed should use the latter — this
    # matches Chromium's Yu Gothic Medium default on Windows). For bold
    # requests the mediums are moved behind the base families so true
    # Bold faces win (see _stack_for).
    'ja': ['Noto Sans CJK JP Medium', 'Noto Sans JP Medium',
           'Source Han Sans JP Medium', 'Yu Gothic Medium',
           'Noto Sans CJK JP', 'Noto Sans JP', 'Hiragino Sans',
           'Hiragino Kaku Gothic ProN', 'Yu Gothic',
           'Meiryo', 'Source Han Sans JP', 'MS PGothic', 'IPAPGothic',
           'Unifont-JP', 'Unifont JP'],
    'zh-hans': ['Noto Sans CJK SC Medium', 'Noto Sans SC Medium',
                'Source Han Sans SC Medium',
                'PingFang SC', 'Noto Sans CJK SC', 'Noto Sans SC',
                'Source Han Sans SC', 'Microsoft YaHei', 'SimHei',
                'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei'],
    'zh-hant': ['Noto Sans CJK TC Medium', 'Noto Sans TC Medium',
                'PingFang TC', 'Noto Sans CJK TC', 'Noto Sans TC',
                'Source Han Sans TW', 'Microsoft JhengHei', 'PMingLiU',
                'WenQuanYi Zen Hei'],
    'ko': ['Noto Sans CJK KR Medium', 'Noto Sans KR Medium',
           'Apple SD Gothic Neo', 'Noto Sans CJK KR', 'Noto Sans KR',
           'Source Han Sans KR', 'Malgun Gothic', 'NanumGothic'],
    '': ['Arial', 'Helvetica', 'Liberation Sans', 'DejaVu Sans',
         'Noto Sans', 'Roboto', 'Segoe UI'],
}


def _stack_for(lk: str, w: int) -> List[str]:
    """Language stack ordered for the requested weight: medium-named
    families lead for normal text, trail for bold (true Bolds win)."""
    names = _LANG_DEFAULT_STACKS.get(lk, [])
    if w >= 600:
        med = [n for n in names if n.lower().endswith('medium')]
        rest = [n for n in names if not n.lower().endswith('medium')]
        return rest + med
    return names

#: last-resort pan-unicode fallbacks
_UNIVERSAL_FALLBACKS = ['DejaVu Sans', 'Arial Unicode MS', 'Unifont',
                        'Unifont-JP', 'Noto Sans', 'FreeSans',
                        'WenQuanYi Zen Hei']

_GENERIC = {'sans-serif', 'serif', 'monospace', 'cursive', 'fantasy',
            'system-ui', 'default'}


def _lang_key(lang: str) -> str:
    l = (lang or '').lower().replace('_', '-')
    if l.startswith('ja'):
        return 'ja'
    if l.startswith('zh'):
        if any(t in l for t in ('hant', '-tw', '-hk', '-mo')):
            return 'zh-hant'
        return 'zh-hans'
    if l.startswith('ko'):
        return 'ko'
    return ''


def default_font_dirs() -> List[str]:
    dirs: List[str] = []
    home = os.path.expanduser('~')
    if sys.platform.startswith('win'):
        windir = os.environ.get('WINDIR', r'C:\Windows')
        dirs += [os.path.join(windir, 'Fonts'),
                 os.path.join(os.environ.get('LOCALAPPDATA', ''),
                              'Microsoft', 'Windows', 'Fonts')]
    elif sys.platform == 'darwin':
        dirs += ['/System/Library/Fonts', '/System/Library/Fonts/Supplemental',
                 '/Library/Fonts', os.path.join(home, 'Library', 'Fonts')]
    else:
        dirs += ['/usr/share/fonts', '/usr/local/share/fonts',
                 os.path.join(home, '.fonts'),
                 os.path.join(home, '.local', 'share', 'fonts')]
    # bundled fonts shipped with the app
    bundled = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'resources', 'fonts')
    dirs.append(bundled)
    return [d for d in dirs if d and os.path.isdir(d)]


def _cache_path() -> str:
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
        os.path.expanduser('~'), '.cache')
    d = os.path.join(base, 'ttml2pgs')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'fontindex.json')


# --------------------------------------------------------------------------- #

class FontManager:
    """Singleton-ish registry of system fonts + face/hb caches."""

    _instance: Optional['FontManager'] = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> 'FontManager':
        with cls._lock:
            if cls._instance is None:
                cls._instance = FontManager()
                cls._instance.scan()
            return cls._instance

    def __init__(self, extra_dirs: Sequence[str] = ()):  # noqa: D401
        self.extra_dirs = list(extra_dirs)
        self.records: List[FaceRecord] = []
        self.by_family: Dict[str, List[FaceRecord]] = {}
        self._ft_faces: Dict[Tuple[str, int, int], freetype.Face] = {}
        self._hb_fonts: Dict[Tuple[str, int], hb.Font] = {}
        self._blob_bytes: Dict[str, bytes] = {}
        self._coverage_cache: Dict[Tuple[str, int, int], bool] = {}
        self._resolve_cache: Dict[tuple, List[FaceRecord]] = {}

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def scan(self, dirs: Optional[Sequence[str]] = None,
             use_cache: bool = True) -> None:
        dirs = list(dirs) if dirs else (default_font_dirs() + self.extra_dirs)
        cache: Dict[str, dict] = {}
        cpath = _cache_path()
        if use_cache and os.path.exists(cpath):
            try:
                with open(cpath, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
            except (OSError, ValueError):
                cache = {}

        records: List[FaceRecord] = []
        new_cache: Dict[str, dict] = {}
        for d in dirs:
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if not fn.lower().endswith(_FONT_EXTS):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    sig = f"{st.st_mtime_ns}:{st.st_size}"
                    ent = cache.get(path)
                    if ent and ent.get('sig') == sig:
                        new_cache[path] = ent
                        for face in ent['faces']:
                            records.append(FaceRecord(
                                path=path, index=face['index'],
                                families=face['families'],
                                weight=face.get('weight', 400),
                                italic=face.get('italic', False),
                                monospace=face.get('monospace', False)))
                        continue
                    faces = self._read_font_names(path)
                    if faces:
                        new_cache[path] = {'sig': sig, 'faces': faces}
                        for face in faces:
                            records.append(FaceRecord(
                                path=path, index=face['index'],
                                families=face['families'],
                                weight=face.get('weight', 400),
                                italic=face.get('italic', False),
                                monospace=face.get('monospace', False)))

        self.records = records
        self.by_family = {}
        for rec in records:
            for fam in rec.families:
                self.by_family.setdefault(_norm_family(fam), []).append(rec)
        self._resolve_cache.clear()

        try:
            with open(cpath, 'w', encoding='utf-8') as f:
                json.dump(new_cache, f)
        except OSError:
            pass

    @staticmethod
    def _read_font_names(path: str) -> List[dict]:
        """Read family names/weight/slant for every face in a font file."""
        from fontTools.ttLib import TTFont, TTLibError
        from fontTools.ttLib.ttCollection import TTCollection
        out: List[dict] = []

        def face_info(tt, index: int) -> Optional[dict]:
            try:
                name = tt['name']
            except KeyError:
                return None
            fams: List[str] = []
            for nid in (16, 1):
                for rec in name.names:
                    if rec.nameID == nid:
                        try:
                            val = rec.toUnicode().strip()
                        except UnicodeDecodeError:
                            continue
                        if val and val not in fams:
                            fams.append(val)
            if not fams:
                return None
            weight, italic, mono = 400, False, False
            if 'OS/2' in tt:
                os2 = tt['OS/2']
                weight = int(getattr(os2, 'usWeightClass', 400) or 400)
                italic = bool(getattr(os2, 'fsSelection', 0) & 1)
            if not italic and 'head' in tt:
                italic = bool(tt['head'].macStyle & 2)
                if weight == 400 and (tt['head'].macStyle & 1):
                    weight = 700
            if 'post' in tt:
                mono = bool(getattr(tt['post'], 'isFixedPitch', 0))
            # subfamily hints for files with bad OS/2
            for rec in name.names:
                if rec.nameID in (2, 17):
                    try:
                        sub = rec.toUnicode().lower()
                    except UnicodeDecodeError:
                        continue
                    if 'italic' in sub or 'oblique' in sub:
                        italic = True
                    if 'bold' in sub and weight < 600:
                        weight = 700
            return {'index': index, 'families': fams, 'weight': weight,
                    'italic': italic, 'monospace': mono}

        try:
            if path.lower().endswith(('.ttc', '.otc')):
                coll = TTCollection(path, lazy=True)
                for i, tt in enumerate(coll.fonts):
                    info = face_info(tt, i)
                    if info:
                        out.append(info)
                coll.close()
            else:
                tt = TTFont(path, lazy=True, fontNumber=-1)
                info = face_info(tt, 0)
                if info:
                    out.append(info)
                tt.close()
        except (TTLibError, OSError, Exception):
            return out
        return out

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def families_available(self) -> List[str]:
        seen = {}
        for rec in self.records:
            if rec.families:
                seen.setdefault(rec.families[0], True)
        return sorted(seen)

    def _score(self, rec: FaceRecord, weight: int, italic: bool) -> int:
        # Prefer the heavier face when two weights are equally distant
        # (Medium over Light for a 'normal' request): subtitle text over
        # video should err toward solidity, and it matches Chrome's
        # Japanese default (Yu Gothic Medium).
        s = abs(rec.weight - weight) * 2
        if rec.weight < weight:
            s += 1
        if rec.italic != italic:
            s += 1000
        return s

    def _lookup_family(self, family: str, weight: int, italic: bool
                       ) -> List[FaceRecord]:
        recs = self.by_family.get(_norm_family(family), [])
        return sorted(recs, key=lambda r: self._score(r, weight, italic))

    def _lang_classified(self, lang_key: str, weight: int, italic: bool
                         ) -> List[FaceRecord]:
        """All faces whose names carry the language's markers, best first."""
        markers = _LANG_MARKERS.get(lang_key, [])
        found: List[FaceRecord] = []
        seen = set()
        for rec in self.records:
            n = _norm_family(' '.join(rec.families))
            if any(mk.replace(' ', '') in n for mk in markers):
                if rec.key() not in seen:
                    seen.add(rec.key())
                    found.append(rec)
        return sorted(found, key=lambda r: self._score(r, weight, italic))

    def resolve_stack(self, families: Sequence[str], lang: str = '',
                      weight: str = 'normal', italic: bool = False,
                      preferred: str = '') -> List[FaceRecord]:
        """
        Turn a requested family list + language into an ordered candidate
        face list (deduplicated). This is the core selection routine.

        ``preferred`` — the user's per-language default font. It heads
        the resolution of generic families (sans-serif etc.) and the
        fallback chain, but never displaces a specific family the
        subtitle author asked for.
        """
        lk = _lang_key(lang)
        if str(weight) in ('bold', '700', '800', '900'):
            w = 700
        else:
            # CJK 'normal' targets Medium (500): 400-weight CJK faces are
            # designed for print/body text and look anemic as subtitles —
            # Chromium ships the same choice (Yu Gothic Medium) on
            # Windows. Latin keeps 400.
            w = 500 if lk else 400
        preferred = (preferred or '').strip()
        key = (tuple(families), lk, w, italic, preferred)
        cached = self._resolve_cache.get(key)
        if cached is not None:
            return cached

        ordered: List[FaceRecord] = []
        seen = set()

        def add(recs: Iterable[FaceRecord]):
            for r in recs:
                if r.key() not in seen:
                    seen.add(r.key())
                    ordered.append(r)

        def add_preferred():
            if preferred:
                add(self._lookup_family(preferred, w, italic))

        for fam in families:
            f = fam.strip()
            if not f:
                continue
            fl = f.lower()
            if fl in _GENERIC or fl == 'japanese':
                # generic → user default, then language stack (CJK aware)
                add_preferred()
                for name in _stack_for(lk, w) + _LANG_DEFAULT_STACKS['']:
                    add(self._lookup_family(name, w, italic))
                if lk:
                    add(self._lang_classified(lk, w, italic))
            else:
                # author-named CJK families: try the Medium sibling
                # family first at normal weight (files say 'Noto Sans
                # JP'; the installed Medium lives under '… Medium')
                if w == 500 and not fl.endswith('medium'):
                    add(self._lookup_family(f + ' Medium', w, italic))
                add(self._lookup_family(f, w, italic))

        # language fallback after the explicit list
        add_preferred()
        if lk:
            for name in _stack_for(lk, w):
                add(self._lookup_family(name, w, italic))
            add(self._lang_classified(lk, w, italic))
        for name in _LANG_DEFAULT_STACKS['']:
            add(self._lookup_family(name, w, italic))
        for name in _UNIVERSAL_FALLBACKS:
            add(self._lookup_family(name, w, italic))
        # absolute last resort: anything
        if not ordered and self.records:
            add(sorted(self.records, key=lambda r: self._score(r, w, italic)))

        self._resolve_cache[key] = ordered
        return ordered

    # ------------------------------------------------------------------ #
    # Coverage + face objects
    # ------------------------------------------------------------------ #
    def covers(self, rec: FaceRecord, codepoint: int) -> bool:
        ck = (rec.path, rec.index, codepoint)
        hit = self._coverage_cache.get(ck)
        if hit is not None:
            return hit
        face = self.ft_face(rec.path, rec.index)
        ok = face is not None and face.get_char_index(codepoint) != 0
        self._coverage_cache[ck] = ok
        return ok

    def pick_face(self, candidates: Sequence[FaceRecord], ch: str
                  ) -> Optional[FaceRecord]:
        cp = ord(ch)
        # whitespace/control: render with the first candidate
        if ch.isspace():
            return candidates[0] if candidates else None
        for rec in candidates:
            if self.covers(rec, cp):
                return rec
        return candidates[0] if candidates else None

    def face_covering(self, candidates: Sequence[FaceRecord], ch: str
                      ) -> Optional[FaceRecord]:
        """Like pick_face but returns None when nothing covers the char."""
        if ch.isspace():
            return candidates[0] if candidates else None
        cp = ord(ch)
        for rec in candidates:
            if self.covers(rec, cp):
                return rec
        return None

    #: pan-unicode bitmap fallbacks + bitmap-era CJK system fonts whose
    #: outlines look wiry at subtitle sizes
    _LOW_QUALITY_MARKERS = ('unifont', 'lastresort', 'msgothic', 'mspgothic',
                            'msmincho', 'mspmincho', 'simsun', 'nsimsun',
                            'mingliu', 'pmingliu', 'gulim', 'batang')

    @classmethod
    def is_low_quality(cls, rec: FaceRecord) -> bool:
        """Fonts that cover a lot but render poorly (bitmap heritage).
        Typographic substitutions are preferred over glyphs from these."""
        n = _norm_family(' '.join(rec.families))
        return any(m in n for m in cls._LOW_QUALITY_MARKERS)

    def ft_face(self, path: str, index: int, size_px: float = 0
                ) -> Optional[freetype.Face]:
        # size-less shared face for coverage; rasterizer sets sizes itself
        key = (path, index, 0)
        face = self._ft_faces.get(key)
        if face is None:
            try:
                face = freetype.Face(path, index)
            except freetype.FT_Exception:
                return None
            self._ft_faces[key] = face
        return face

    def font_bytes(self, path: str) -> bytes:
        b = self._blob_bytes.get(path)
        if b is None:
            with open(path, 'rb') as f:
                b = f.read()
            self._blob_bytes[path] = b
        return b

    def hb_font(self, path: str, index: int) -> Optional[hb.Font]:
        key = (path, index)
        font = self._hb_fonts.get(key)
        if font is None:
            try:
                blob = hb.Blob(self.font_bytes(path))
                face = hb.Face(blob, index)
                font = hb.Font(face)
                font.scale = (face.upem, face.upem)
            except Exception:
                return None
            self._hb_fonts[key] = font
        return font
