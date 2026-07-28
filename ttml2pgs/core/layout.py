"""
Text layout engine.

Turns flattened inline items (text runs with fully-resolved numeric
styles, ruby groups, breaks) into positioned glyphs:

* line breaking — Latin word breaking + CJK per-character breaking with
  kinsoku shori (forbidden line-start/line-end characters),
* horizontal and vertical (``tbrl``/``tblr``) flows,
* ruby annotations (over/under, 1-2-1 justified when narrower than the
  base, centered with atom widening when wider),
* tate-chu-yoko (horizontal-in-vertical digit/word groups),
* per-character orientation in vertical text (CJK upright, Latin rotated,
  optional full-width digit conversion),
* text alignment incl. ebutts multiRowAlign block alignment,
* text emphasis marks (bouten), underline, per-span background boxes.

Output coordinates are block-local, x→right / y→down, origin at the
block's top-left. The cue renderer positions the block inside its region.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .colors import RGBA
from .fonts import FaceRecord, FontManager
from .shaping import ShapedGlyph, face_metrics, shape_run

# --------------------------------------------------------------------------- #
# Resolved (numeric) styles — everything already in pixels
# --------------------------------------------------------------------------- #


@dataclass
class ResolvedShadow:
    dx: float
    dy: float
    blur: float
    color: RGBA


@dataclass
class RunStyle:
    font_px: float = 48.0
    color: RGBA = (255, 255, 255, 255)
    outline_px: float = 0.0
    outline_color: RGBA = (0, 0, 0, 255)
    shadows: List[ResolvedShadow] = field(default_factory=list)
    background: RGBA = (0, 0, 0, 0)
    shear_deg: float = 0.0
    opacity: float = 1.0
    letter_spacing_px: float = 0.0
    text_decoration: str = 'none'
    emphasis_style: Optional[str] = None
    emphasis_color: Optional[RGBA] = None
    emphasis_position: Optional[str] = None
    faces: List[FaceRecord] = field(default_factory=list)
    bold: bool = False
    italic: bool = False
    lang: str = ''
    ruby_scale: float = 0.5
    shear_axis: str = 'x'     # 'y' for upright glyphs in vertical flow

    def scaled(self, factor: float) -> 'RunStyle':
        import copy
        s = copy.copy(self)
        s.font_px = self.font_px * factor
        s.letter_spacing_px = self.letter_spacing_px * factor
        return s


# --------------------------------------------------------------------------- #
# Inline items (input)
# --------------------------------------------------------------------------- #

@dataclass
class TextItem:
    text: str
    style: RunStyle


@dataclass
class BreakItem:
    pass


@dataclass
class RubyItem:
    base: List[TextItem]
    annotation: List[TextItem]
    style: RunStyle                  # container style
    position: str = 'before'         # before (over/right) | after
    align: str = 'center'


@dataclass
class TCYItem:                       # tate-chu-yoko
    text: str
    style: RunStyle


InlineItem = object  # union of the above


# --------------------------------------------------------------------------- #
# Output structures
# --------------------------------------------------------------------------- #

@dataclass
class PlacedGlyph:
    face: FaceRecord
    gid: int
    x: float                  # baseline origin x (block-local)
    y: float                  # baseline origin y
    font_px: float
    style: RunStyle
    rot90: bool = False       # drawn rotated 90° cw (vertical sideways)
    synth_bold: bool = False
    synth_italic: bool = False
    scale_x: float = 1.0      # horizontal squash (TCY overflow)


@dataclass
class Mark:
    """Text-emphasis mark."""
    cx: float
    cy: float
    radius: float
    color: RGBA
    filled: bool = True
    sesame: bool = False
    style: Optional[RunStyle] = None


@dataclass
class DecoRect:
    x: float
    y: float
    w: float
    h: float
    color: RGBA
    kind: str = 'background'   # background | underline | strikethrough


@dataclass
class LineBox:
    x: float
    y: float
    w: float
    h: float
    baseline: float


@dataclass
class LayoutResult:
    glyphs: List[PlacedGlyph] = field(default_factory=list)
    marks: List[Mark] = field(default_factory=list)
    rects: List[DecoRect] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    lines: List[LineBox] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Character classification
# --------------------------------------------------------------------------- #

_NO_START = set('。、．，）」』】〉》〕｝〙〗›»！？；：‼⁇⁈⁉・'
                'ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶ'
                'ーゝゞ々〻…‥.,)]}!?;:%‰′″℃»')
_NO_END = set('（「『【〈《〔｛〘〖‹«([{£$¥＄￥')

_CJK_RANGES = (
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x2E80, 0x2FDF),   # radicals
    (0x3000, 0x303F),   # CJK punct
    (0x3040, 0x30FF),   # kana
    (0x3130, 0x318F),   # Hangul compat
    (0x31C0, 0x9FFF),   # strokes..unified
    (0xA960, 0xA97F),
    (0xAC00, 0xD7FF),   # Hangul syllables
    (0xF900, 0xFAFF),   # compat ideographs
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFF60),   # fullwidth forms
    (0xFFE0, 0xFFE6),
    (0x20000, 0x3FFFF),
)


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for a, b in _CJK_RANGES)


def _is_upright_in_vertical(ch: str) -> bool:
    """True if the char stays upright in vertical flow (mixed orientation)."""
    if is_cjk(ch):
        return True
    cp = ord(ch)
    # arrows/geometric/enclosed forms stay upright too
    if 0x2460 <= cp <= 0x24FF or 0x25A0 <= cp <= 0x27BF:
        return True
    return False


_FW_DIGITS = str.maketrans('0123456789', '０１２３４５６７８９')

#: last-chance substitutions for characters most fonts lack, tried before
#: falling back to a pan-unicode bitmap font (keeps rare punctuation from
#: rendering in an ugly fallback face).
_CHAR_SUBSTITUTIONS = {
    '⸺': '——',      # two-em dash -> 2x em dash
    '⸻': '———',
    '〝': '“', '〞': '”', '〟': '„',
    '⹀': '＝', '﹘': '－',
    '¬': '-', '⁇': '??', '⁈': '?!', '⁉': '!?', '‼': '!!',
    '［': '[', '］': ']', '｟': '（', '｠': '）',
    '`': "'",
}


# --------------------------------------------------------------------------- #
# Atoms
# --------------------------------------------------------------------------- #

@dataclass
class _Run:
    face: FaceRecord
    glyphs: List[ShapedGlyph]
    style: RunStyle
    text: str
    advance: float
    rot90: bool = False
    font_px: float = 0.0


@dataclass
class _RubyPart:
    runs: List[_Run]
    advance: float


@dataclass
class _Atom:
    kind: str                    # word | char | space | ruby | tcy
    runs: List[_Run] = field(default_factory=list)
    advance: float = 0.0
    text: str = ''
    breakable_before: bool = True
    ruby_base: Optional[_RubyPart] = None
    ruby_ann: Optional[_RubyPart] = None
    ruby_position: str = 'before'
    ruby_style: Optional[RunStyle] = None
    tcy_scale: float = 1.0


class LayoutEngine:
    """Stateless layout engine (uses the FontManager singleton)."""

    def __init__(self, vertical_digits: str = 'fullwidth'):
        self.fm = FontManager.instance()
        self.vertical_digits = vertical_digits  # fullwidth | rotate

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def layout(self, items: Sequence[InlineItem],
               measure: Optional[float],
               vertical: bool = False,
               text_align: str = 'center',
               multi_row_align: Optional[str] = None,
               line_height_px: Optional[float] = None,
               line_height_factor: float = 1.25,
               wrap: bool = True,
               rtl_columns: bool = True) -> LayoutResult:
        """
        measure – wrapping extent in px along the *inline* axis
                  (width for horizontal, height for vertical);
                  None disables wrapping (explicit breaks only).
        rtl_columns – vertical only: True = tbrl (columns right→left).
        """
        atoms_lines = self._items_to_atom_lines(items, vertical)
        lines: List[List[_Atom]] = []
        for atoms in atoms_lines:
            if wrap and measure is not None and measure > 0:
                lines.extend(self._break_atoms(atoms, measure))
            else:
                lines.append(atoms)
        # drop leading/trailing space atoms per line
        for ln in lines:
            while ln and ln[0].kind == 'space':
                ln.pop(0)
            while ln and ln[-1].kind == 'space':
                ln.pop()
        lines = [ln for ln in lines if ln] or [[]]

        if vertical:
            return self._place_vertical(lines, measure, text_align,
                                        multi_row_align, line_height_px,
                                        line_height_factor, rtl_columns)
        return self._place_horizontal(lines, measure, text_align,
                                      multi_row_align, line_height_px,
                                      line_height_factor)

    # ------------------------------------------------------------------ #
    # items -> atoms
    # ------------------------------------------------------------------ #
    def _items_to_atom_lines(self, items: Sequence[InlineItem],
                             vertical: bool) -> List[List[_Atom]]:
        lines: List[List[_Atom]] = [[]]
        for item in items:
            if isinstance(item, BreakItem):
                lines.append([])
            elif isinstance(item, TextItem):
                lines[-1].extend(self._atomize_text(item, vertical))
            elif isinstance(item, TCYItem):
                lines[-1].append(self._make_tcy_atom(item))
            elif isinstance(item, RubyItem):
                lines[-1].append(self._make_ruby_atom(item, vertical))
        return lines

    # -- text ----------------------------------------------------------- #
    def _shape_text(self, text: str, style: RunStyle, vertical_upright: bool,
                    rot90: bool = False, font_px: Optional[float] = None
                    ) -> List[_Run]:
        """Split *text* by required face (coverage) and shape each piece."""
        if text == '':
            return []
        fpx = font_px if font_px is not None else style.font_px
        runs: List[_Run] = []
        seg = ''
        seg_face: Optional[FaceRecord] = None
        for ch in text:
            face = self.fm.face_covering(style.faces, ch)
            piece = ch
            if face is None or self.fm.is_low_quality(face):
                # try a typographic substitution before accepting a
                # missing glyph or a pan-unicode bitmap fallback
                sub = _CHAR_SUBSTITUTIONS.get(ch)
                if sub:
                    sub_faces = [self.fm.face_covering(style.faces, c)
                                 for c in sub]
                    if all(f is not None and not self.fm.is_low_quality(f)
                           for f in sub_faces):
                        piece = sub
                        face = sub_faces[0]
                if face is None:
                    face = self.fm.pick_face(style.faces, ch)
            if face is None:
                continue
            if seg_face is None or face.key() == seg_face.key():
                seg_face = face
                seg += piece
            else:
                runs.append(self._shape_seg(seg, seg_face, style,
                                            vertical_upright, rot90, fpx))
                seg, seg_face = piece, face
        if seg and seg_face is not None:
            runs.append(self._shape_seg(seg, seg_face, style,
                                        vertical_upright, rot90, fpx))
        return runs

    def _shape_seg(self, text: str, face: FaceRecord, style: RunStyle,
                   vertical_upright: bool, rot90: bool, font_px: float
                   ) -> _Run:
        glyphs, adv = shape_run(text, face, font_px,
                                vertical=vertical_upright,
                                language=style.lang,
                                letter_spacing_px=style.letter_spacing_px)
        return _Run(face=face, glyphs=glyphs, style=style, text=text,
                    advance=adv, rot90=rot90, font_px=font_px)

    def _atomize_text(self, item: TextItem, vertical: bool) -> List[_Atom]:
        atoms: List[_Atom] = []
        text = item.text
        if vertical and self.vertical_digits == 'fullwidth':
            text = text.translate(_FW_DIGITS)

        # segment into (kind, chunk) units
        units: List[Tuple[str, str]] = []
        buf = ''
        for ch in text:
            if ch.isspace():
                if buf:
                    units.append(('word', buf))
                    buf = ''
                units.append(('space', ch))
            elif is_cjk(ch):
                if buf:
                    units.append(('word', buf))
                    buf = ''
                units.append(('char', ch))
            else:
                buf += ch
        if buf:
            units.append(('word', buf))

        for kind, chunk in units:
            if vertical:
                upright = kind == 'char' or all(
                    _is_upright_in_vertical(c) or c.isspace() for c in chunk)
                runs = self._shape_text(chunk, item.style,
                                        vertical_upright=upright,
                                        rot90=not upright)
            else:
                runs = self._shape_text(chunk, item.style,
                                        vertical_upright=False)
            adv = sum(r.advance for r in runs)
            atom = _Atom(kind=kind, runs=runs, advance=adv, text=chunk)
            atoms.append(atom)
        return atoms

    # -- tate-chu-yoko --------------------------------------------------- #
    def _make_tcy_atom(self, item: TCYItem) -> _Atom:
        runs = self._shape_text(item.text, item.style, vertical_upright=False)
        natural = sum(r.advance for r in runs)
        em = item.style.font_px
        scale = min(1.0, em / natural) if natural > 0 else 1.0
        return _Atom(kind='tcy', runs=runs, advance=em, text=item.text,
                     tcy_scale=scale)

    # -- ruby ------------------------------------------------------------ #
    def _make_ruby_atom(self, item: RubyItem, vertical: bool) -> _Atom:
        base_runs: List[_Run] = []
        for ti in item.base:
            t = ti.text
            if vertical and self.vertical_digits == 'fullwidth':
                t = t.translate(_FW_DIGITS)
            base_runs.extend(self._shape_text(t, ti.style,
                                              vertical_upright=vertical))
        ann_runs: List[_Run] = []
        for ti in item.annotation:
            ann_style = ti.style.scaled(ti.style.ruby_scale)
            ann_runs.extend(self._shape_text(ti.text, ann_style,
                                             vertical_upright=vertical))
        base_adv = sum(r.advance for r in base_runs)
        ann_adv = sum(r.advance for r in ann_runs)
        atom = _Atom(kind='ruby',
                     advance=max(base_adv, ann_adv),
                     text=''.join(t.text for t in item.base),
                     ruby_base=_RubyPart(base_runs, base_adv),
                     ruby_ann=_RubyPart(ann_runs, ann_adv),
                     ruby_position=item.position,
                     ruby_style=item.style)
        return atom

    # ------------------------------------------------------------------ #
    # line breaking
    # ------------------------------------------------------------------ #
    def _break_atoms(self, atoms: List[_Atom], measure: float
                     ) -> List[List[_Atom]]:
        lines: List[List[_Atom]] = []
        cur: List[_Atom] = []
        cur_adv = 0.0

        def cur_trim_adv() -> float:
            """current advance ignoring trailing spaces"""
            a = cur_adv
            for at in reversed(cur):
                if at.kind == 'space':
                    a -= at.advance
                else:
                    break
            return a

        i = 0
        while i < len(atoms):
            atom = atoms[i]
            candidate = cur_trim_adv() + (0 if atom.kind == 'space' else atom.advance)
            if cur and atom.kind != 'space' and candidate > measure + 0.01:
                # need a break before `atom` — apply kinsoku
                brk = len(cur)
                # forbidden start: pull preceding atom(s) down with it
                while brk > 1 and atom.text[:1] in _NO_START and \
                        cur[brk - 1].kind in ('char', 'word'):
                    # move last atom of line down (oidashi), then re-check
                    atom_prev = cur[brk - 1]
                    if atom_prev.text[:1] in _NO_START:
                        brk -= 1
                        continue
                    brk -= 1
                    break
                # forbidden end: line must not end with opening bracket
                while brk > 1 and cur[brk - 1].text[-1:] in _NO_END:
                    brk -= 1
                moved = cur[brk:]
                del cur[brk:]
                while cur and cur[-1].kind == 'space':
                    cur.pop()
                if cur:
                    lines.append(cur)
                cur = moved
                cur_adv = sum(a.advance for a in cur)
                # re-evaluate same atom on the fresh line
                continue
            cur.append(atom)
            cur_adv += atom.advance
            i += 1
        if cur:
            lines.append(cur)
        return lines or [[]]

    # ------------------------------------------------------------------ #
    # metrics helpers
    # ------------------------------------------------------------------ #
    def _atom_metrics(self, atom: _Atom) -> Tuple[float, float, float, float]:
        """(ascent, descent, ruby_before_h, ruby_after_h) for an atom."""
        asc = desc = 0.0
        rb = ra = 0.0
        runs = atom.runs
        if atom.kind == 'ruby':
            runs = atom.ruby_base.runs
            ann_runs = atom.ruby_ann.runs
            ann_h = 0.0
            for r in ann_runs:
                m = face_metrics(r.face, r.font_px)
                ann_h = max(ann_h, m.ascent + m.descent)
            gap = 0.05 * (runs[0].font_px if runs else 24.0)
            if atom.ruby_position == 'after':
                ra = ann_h + gap
            else:
                rb = ann_h + gap
        for r in runs:
            m = face_metrics(r.face, r.font_px)
            asc = max(asc, m.ascent)
            desc = max(desc, m.descent)
        if atom.kind == 'tcy':
            fs = runs[0].font_px if runs else 24.0
            asc = max(asc, fs * 0.88)
            desc = max(desc, fs * 0.12)
        return asc, desc, rb, ra

    def _line_metrics(self, atoms: List[_Atom]
                      ) -> Tuple[float, float, float, float, float]:
        """(ascent, descent, ruby_before, ruby_after, max_font_px)"""
        asc = desc = rb = ra = 0.0
        maxf = 0.0
        for a in atoms:
            aa, dd, b, r = self._atom_metrics(a)
            asc, desc = max(asc, aa), max(desc, dd)
            rb, ra = max(rb, b), max(ra, r)
            for run in (a.runs or
                        (a.ruby_base.runs if a.ruby_base else [])):
                maxf = max(maxf, run.font_px)
        if maxf == 0:
            maxf = 24.0
        if asc == 0 and desc == 0:
            asc, desc = maxf * 0.8, maxf * 0.2
        return asc, desc, rb, ra, maxf

    # ------------------------------------------------------------------ #
    # placement — horizontal
    # ------------------------------------------------------------------ #
    def _place_horizontal(self, lines, measure, text_align, multi_row_align,
                          line_height_px, lh_factor) -> LayoutResult:
        result = LayoutResult()
        line_widths = [sum(a.advance for a in ln) for ln in lines]
        block_w = measure if measure else (max(line_widths) if line_widths else 0)
        block_w = max(block_w, max(line_widths) if line_widths else 0)

        # multiRowAlign: lines align to each other inside the block; the
        # block itself is aligned by text_align.
        y = 0.0
        for ln, lw in zip(lines, line_widths):
            asc, desc, rb, ra, maxf = self._line_metrics(ln)
            lh = line_height_px if line_height_px else maxf * lh_factor
            leading = max(0.0, (lh - (asc + desc)) / 2.0)
            y_top = y
            baseline = y_top + leading + rb + asc

            if multi_row_align:
                inner = self._align_offset(multi_row_align, block_w, lw)
            else:
                inner = self._align_offset(text_align, block_w, lw)
            x = inner
            for atom in ln:
                self._emit_atom(result, atom, x, baseline,
                                asc, desc, vertical=False)
                x += atom.advance
            line_h = leading * 2 + rb + ra + asc + desc
            result.lines.append(LineBox(inner, y_top, lw, line_h, baseline))
            y = y_top + line_h

        result.width = block_w
        result.height = y
        # block-level multiRowAlign shift
        if multi_row_align:
            content_w = max(line_widths) if line_widths else 0
            shift = self._align_offset(text_align, block_w, content_w)
            inner_shift = self._align_offset(multi_row_align, block_w, content_w)
            delta = shift - inner_shift
            if abs(delta) > 0.01:
                for g in result.glyphs:
                    g.x += delta
                for mk in result.marks:
                    mk.cx += delta
                for rc in result.rects:
                    rc.x += delta
                for lb in result.lines:
                    lb.x += delta
        return result

    # ------------------------------------------------------------------ #
    # placement — vertical
    # ------------------------------------------------------------------ #
    def _place_vertical(self, lines, measure, text_align, multi_row_align,
                        line_height_px, lh_factor, rtl_columns) -> LayoutResult:
        result = LayoutResult()
        line_extents = [sum(a.advance for a in ln) for ln in lines]
        block_h = measure if measure else (max(line_extents) if line_extents else 0)
        block_h = max(block_h, max(line_extents) if line_extents else 0)

        # column pitches
        pitches = []
        col_metrics = []
        for ln in lines:
            asc, desc, rb, ra, maxf = self._line_metrics(ln)
            pitch = line_height_px if line_height_px else maxf * lh_factor
            pitch = max(pitch, asc + desc)
            # ruby occupies extra pitch on the 'before' side (right in tbrl)
            pitches.append(pitch + rb + ra)
            col_metrics.append((asc, desc, rb, ra, maxf))
        block_w = sum(pitches)

        x_edge = block_w if rtl_columns else 0.0
        for idx, (ln, ext) in enumerate(zip(lines, line_extents)):
            asc, desc, rb, ra, maxf = col_metrics[idx]
            pitch = pitches[idx]
            if rtl_columns:
                # column occupies [x_edge - pitch, x_edge]; ruby-before on right
                center = x_edge - ra - (pitch - rb - ra) / 2.0
                x_edge -= pitch
            else:
                center = x_edge + rb + (pitch - rb - ra) / 2.0
                x_edge += pitch

            y = self._align_offset(text_align, block_h, ext,
                                   vertical=True)
            top = y
            for atom in ln:
                self._emit_atom_vertical(result, atom, center, y,
                                         asc, desc, rb, ra, rtl_columns)
                y += atom.advance
            result.lines.append(LineBox(
                center - (asc + desc) / 2 - (ra if rtl_columns else rb),
                top, pitch, ext, center))
        result.width = block_w
        result.height = block_h
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _align_offset(align: str, box: float, content: float,
                      vertical: bool = False) -> float:
        a = (align or 'center').lower()
        if a in ('start', 'left', 'top', 'before'):
            return 0.0
        if a in ('end', 'right', 'bottom', 'after'):
            return max(0.0, box - content)
        return max(0.0, (box - content) / 2.0)

    # ------------------------------------------------------------------ #
    # atom emission — horizontal
    # ------------------------------------------------------------------ #
    def _emit_atom(self, result: LayoutResult, atom: _Atom, x: float,
                   baseline: float, line_asc: float, line_desc: float,
                   vertical: bool):
        if atom.kind == 'ruby':
            self._emit_ruby_h(result, atom, x, baseline, line_asc)
            return
        if atom.kind == 'tcy':
            # in horizontal flow TCY is just inline text
            pass
        pen = x
        for run in atom.runs:
            self._emit_run_h(result, run, pen, baseline)
            pen += run.advance

    def _emit_run_h(self, result: LayoutResult, run: _Run, x: float,
                    baseline: float, scale_x: float = 1.0):
        st = run.style
        m = face_metrics(run.face, run.font_px)
        # background box for the run
        if st.background[3] > 0:
            result.rects.append(DecoRect(
                x, baseline - m.ascent, run.advance * scale_x,
                m.ascent + m.descent, st.background, 'background'))
        pen = x
        synth_bold = st.bold and run.face.weight < 600
        synth_italic = st.italic and not run.face.italic and st.shear_deg == 0
        for i, g in enumerate(run.glyphs):
            result.glyphs.append(PlacedGlyph(
                face=run.face, gid=g.gid,
                x=pen + g.x_off * scale_x, y=baseline - g.y_off,
                font_px=run.font_px, style=st,
                synth_bold=synth_bold, synth_italic=synth_italic,
                scale_x=scale_x))
            pen += g.x_adv * scale_x
        # decorations
        if st.text_decoration and 'underline' in st.text_decoration.lower():
            th = max(1.0, run.font_px * 0.05)
            result.rects.append(DecoRect(
                x, baseline + m.descent * 0.45, run.advance * scale_x, th,
                st.color, 'underline'))
        if st.text_decoration and 'linethrough' in st.text_decoration.lower().replace('-', ''):
            th = max(1.0, run.font_px * 0.05)
            result.rects.append(DecoRect(
                x, baseline - m.ascent * 0.35, run.advance * scale_x, th,
                st.color, 'strikethrough'))
        # emphasis marks (bouten) per cluster
        if st.emphasis_style:
            self._emit_emphasis_h(result, run, x, baseline, m)

    def _emit_emphasis_h(self, result, run: _Run, x: float, baseline: float,
                         m) -> None:
        st = run.style
        color = st.emphasis_color or st.color
        r = run.font_px * 0.09
        under = (st.emphasis_position == 'after')
        cy = baseline + m.descent + r * 1.8 if under \
            else baseline - m.ascent - r * 1.8
        filled = 'open' not in (st.emphasis_style or '')
        sesame = 'sesame' in (st.emphasis_style or '')
        pen = x
        for g in run.glyphs:
            if run.text and not run.text.strip():
                pen += g.x_adv
                continue
            cx = pen + g.x_adv / 2.0
            result.marks.append(Mark(cx, cy, r, color, filled, sesame, st))
            pen += g.x_adv

    def _emit_ruby_h(self, result: LayoutResult, atom: _Atom, x: float,
                     baseline: float, line_asc: float):
        base, ann = atom.ruby_base, atom.ruby_ann
        # base centered in atom advance
        bx = x + (atom.advance - base.advance) / 2.0
        pen = bx
        base_asc = 0.0
        for run in base.runs:
            m = face_metrics(run.face, run.font_px)
            base_asc = max(base_asc, m.ascent)
        for run in base.runs:
            self._emit_run_h(result, run, pen, baseline)
            pen += run.advance

        if not ann.runs:
            return
        fs = base.runs[0].font_px if base.runs else 24.0
        gap = 0.05 * fs
        ann_m = [face_metrics(r.face, r.font_px) for r in ann.runs]
        ann_desc = max(mm.descent for mm in ann_m)
        ann_asc = max(mm.ascent for mm in ann_m)
        under = (atom.ruby_position == 'after')
        if under:
            base_desc = 0.0
            for run in base.runs:
                m = face_metrics(run.face, run.font_px)
                base_desc = max(base_desc, m.descent)
            ann_baseline = baseline + base_desc + gap + ann_asc
        else:
            ann_baseline = baseline - base_asc - gap - ann_desc

        n_glyphs = sum(len(r.glyphs) for r in ann.runs)
        if ann.advance < atom.advance - 0.5 and n_glyphs > 0:
            # 1-2-1 justification: edge = free/(2n), inner gaps = free/n
            free = atom.advance - ann.advance
            edge = free / (2 * n_glyphs)
            inner = free / n_glyphs
            pen = x + edge
            gi = 0
            for run in ann.runs:
                st = run.style
                synth_bold = st.bold and run.face.weight < 600
                for g in run.glyphs:
                    result.glyphs.append(PlacedGlyph(
                        face=run.face, gid=g.gid,
                        x=pen + g.x_off, y=ann_baseline - g.y_off,
                        font_px=run.font_px, style=st,
                        synth_bold=synth_bold))
                    pen += g.x_adv
                    gi += 1
                    if gi < n_glyphs:
                        pen += inner
        else:
            # annotation centered over the atom (may overhang)
            pen = x + (atom.advance - ann.advance) / 2.0
            for run in ann.runs:
                self._emit_run_h(result, run, pen, ann_baseline)
                pen += run.advance

    # ------------------------------------------------------------------ #
    # atom emission — vertical
    # ------------------------------------------------------------------ #
    def _emit_atom_vertical(self, result: LayoutResult, atom: _Atom,
                            center: float, y: float, col_asc: float,
                            col_desc: float, ruby_before: float,
                            ruby_after: float, rtl_columns: bool):
        if atom.kind == 'ruby':
            self._emit_ruby_v(result, atom, center, y, rtl_columns)
            return
        if atom.kind == 'tcy':
            self._emit_tcy_v(result, atom, center, y)
            return
        pen_y = y
        for run in atom.runs:
            if run.rot90:
                self._emit_run_v_rotated(result, run, center, pen_y)
            else:
                self._emit_run_v_upright(result, run, center, pen_y)
            pen_y += run.advance

    def _emit_run_v_upright(self, result: LayoutResult, run: _Run,
                            center: float, y: float):
        st = run.style
        if st.background[3] > 0:
            m = face_metrics(run.face, run.font_px)
            half = (m.ascent + m.descent) / 2.0
            result.rects.append(DecoRect(
                center - half, y, half * 2, run.advance,
                st.background, 'background'))
        pen_y = y
        synth_bold = st.bold and run.face.weight < 600
        for g in run.glyphs:
            # HB ttb: pen at vertical origin on the column axis; glyph's
            # horizontal-baseline origin sits at pen + (x_off, -y_off).
            result.glyphs.append(PlacedGlyph(
                face=run.face, gid=g.gid,
                x=center + g.x_off, y=pen_y - g.y_off,
                font_px=run.font_px, style=st,
                synth_bold=synth_bold))
            pen_y += g.y_adv
        if st.emphasis_style:
            color = st.emphasis_color or st.color
            r = run.font_px * 0.09
            side = 1.0 if st.emphasis_position != 'after' else -1.0
            cx = center + side * (run.font_px / 2.0 + r * 1.8)
            filled = 'open' not in (st.emphasis_style or '')
            sesame = 'sesame' in (st.emphasis_style or '')
            pen_y = y
            for g in run.glyphs:
                result.marks.append(Mark(cx, pen_y + g.y_adv / 2.0, r,
                                         color, filled, sesame, st))
                pen_y += g.y_adv

    def _emit_run_v_rotated(self, result: LayoutResult, run: _Run,
                            center: float, y: float):
        """Sideways (rotated 90° cw) latin run in a vertical column."""
        st = run.style
        m = face_metrics(run.face, run.font_px)
        # After a 90° cw rotation the ascender extends +x and the descender
        # -x from the baseline; center the em box on the column axis.
        baseline_x = center - (m.ascent - m.descent) / 2.0
        if st.background[3] > 0:
            half = (m.ascent + m.descent) / 2.0
            result.rects.append(DecoRect(
                center - half, y, half * 2, run.advance,
                st.background, 'background'))
        pen_y = y
        synth_bold = st.bold and run.face.weight < 600
        synth_italic = st.italic and not run.face.italic and st.shear_deg == 0
        for g in run.glyphs:
            result.glyphs.append(PlacedGlyph(
                face=run.face, gid=g.gid,
                x=baseline_x - g.y_off, y=pen_y + g.x_off,
                font_px=run.font_px, style=st, rot90=True,
                synth_bold=synth_bold, synth_italic=synth_italic))
            pen_y += g.x_adv

    def _emit_tcy_v(self, result: LayoutResult, atom: _Atom,
                    center: float, y: float):
        """Tate-chu-yoko: horizontal mini-run centered in the column cell."""
        scale = atom.tcy_scale
        total_w = sum(r.advance for r in atom.runs) * scale
        fs = atom.runs[0].font_px if atom.runs else 24.0
        asc = max((face_metrics(r.face, r.font_px).ascent for r in atom.runs),
                  default=fs * 0.8)
        desc = max((face_metrics(r.face, r.font_px).descent for r in atom.runs),
                   default=fs * 0.2)
        cell_h = atom.advance
        baseline = y + (cell_h - (asc + desc)) / 2.0 + asc
        pen = center - total_w / 2.0
        for run in atom.runs:
            self._emit_run_h(result, run, pen, baseline, scale_x=scale)
            pen += run.advance * scale

    def _emit_ruby_v(self, result: LayoutResult, atom: _Atom,
                     center: float, y: float, rtl_columns: bool):
        base, ann = atom.ruby_base, atom.ruby_ann
        by = y + (atom.advance - base.advance) / 2.0
        pen_y = by
        for run in base.runs:
            self._emit_run_v_upright(result, run, center, pen_y)
            pen_y += run.advance
        if not ann.runs:
            return
        fs = base.runs[0].font_px if base.runs else 24.0
        gap = 0.05 * fs
        ann_fs = ann.runs[0].font_px if ann.runs else fs * 0.5
        # annotation column sits on the 'before' side: right for tbrl
        side = 1.0 if (atom.ruby_position != 'after') == rtl_columns else -1.0
        ann_center = center + side * (fs / 2.0 + gap + ann_fs / 2.0)

        n_glyphs = sum(len(r.glyphs) for r in ann.runs)
        if ann.advance < atom.advance - 0.5 and n_glyphs > 0:
            free = atom.advance - ann.advance
            edge = free / (2 * n_glyphs)
            inner = free / n_glyphs
            pen_y = y + edge
            gi = 0
            for run in ann.runs:
                st = run.style
                for g in run.glyphs:
                    result.glyphs.append(PlacedGlyph(
                        face=run.face, gid=g.gid,
                        x=ann_center + g.x_off, y=pen_y - g.y_off,
                        font_px=run.font_px, style=st))
                    pen_y += g.y_adv
                    gi += 1
                    if gi < n_glyphs:
                        pen_y += inner
        else:
            pen_y = y + (atom.advance - ann.advance) / 2.0
            for run in ann.runs:
                self._emit_run_v_upright(result, run, ann_center, pen_y)
                pen_y += run.advance
