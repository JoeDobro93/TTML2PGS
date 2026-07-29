"""
Selected-cue pane: token-based styled-text editor for the current cue.

The cue's span tree is shown as plain text with **style tokens** —
``⟦Style1 … Style1⟧`` — marking where each named style applies, like
visible TTML ``<span>`` tags. Tokens are atomic: they can be dragged
around (native text drag&drop) but not edited — any change that corrupts
one, ends a span before it starts, nests a style inside itself, or names
an unknown style is automatically reverted. Deleting a token removes its
partner too (keeps the text, drops the styling).

Overlapping ranges are legal and normalized into nested spans (HTML
semantics), so dragging boundaries around freely always yields a valid
tree. ``b``/``i`` are built-in pseudo-styles (bold/italic independent of
named styles). Ruby / tate-chū-yoko blocks appear as one solid
``⟦≡1 漢字(かんじ)⟧`` chip (draggable, not internally editable) so
complex structure survives round-trips; spans carrying other inline
styling from the source file surface as ``✎N`` chips whose contents stay
editable.
"""

from __future__ import annotations

import colorsys
import copy
import dataclasses
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QTextCharFormat,
                         QTextCursor, QTextFormat, QTextObjectInterface)
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QPushButton, QTextEdit, QToolButton,
                             QVBoxLayout, QWidget)

from ...core.model import Cue, SpanNode, Style, SubtitleDocument
from ...core.timing import format_display_time
from .cue_table import parse_style_refs
from .settings_panel import CollapsibleSection

OPEN, CLOSE = '⟦', '⟧'
#: lookalikes shown for literal ⟦⟧ occurring in subtitle TEXT (so they
#: can't be confused with tokens); converted back on rebuild
_ESC = {OPEN: '⟬', CLOSE: '⟭'}
_UNESC = {v: k for k, v in _ESC.items()}
_BI_IDS = ('b', 'i')


def _esc_text(s: str) -> str:
    return s.replace(OPEN, _ESC[OPEN]).replace(CLOSE, _ESC[CLOSE])


def _unesc_text(s: str) -> str:
    return s.replace(_ESC[OPEN], OPEN).replace(_ESC[CLOSE], CLOSE)


def _is_pure_bi(st: Optional[Style]) -> Optional[Tuple[bool, bool]]:
    """(bold, italic) if the style sets nothing else; None otherwise."""
    if st is None:
        return (False, False)
    bold = italic = False
    for f in dataclasses.fields(st):
        if f.name in ('id', 'parent_ids'):
            continue
        v = getattr(st, f.name)
        if v is None:
            continue
        if f.name == 'font_weight' and v == 'bold':
            bold = True
        elif f.name == 'font_style' and v == 'italic':
            italic = True
        else:
            return None
    return (bold, italic)


def _atom_preview(node: SpanNode, doc: SubtitleDocument) -> str:
    """Flattened text of a structural span, ruby readings in parens."""
    out: List[str] = []

    def walk(n: SpanNode, chain):
        for ch in n.children:
            if ch.kind == 'text':
                out.append(ch.text)
            elif ch.kind == 'br':
                out.append('↵')
            elif ch.kind == 'span':
                sub = chain + [(ch.style_refs, ch.inline_style)]
                try:
                    role = doc.resolve_style(sub).ruby or ''
                except Exception:
                    role = ''
                if role in ('text', 'textContainer'):
                    out.append('(')
                    walk(ch, sub)
                    out.append(')')
                elif role == 'delimiter':
                    continue
                else:
                    walk(ch, sub)

    walk(node, [(node.style_refs, node.inline_style)])
    return ''.join(out)


@dataclass
class CueMarkup:
    """The editable representation of one cue's content."""
    doc: SubtitleDocument
    text: str = ''
    #: ✎N shells: (style_refs, inline_style, meta) preserved verbatim
    shells: List[tuple] = field(default_factory=list)
    #: ≡N atoms: whole subtrees preserved verbatim (ruby/TCY/lang spans)
    atoms: List[SpanNode] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @staticmethod
    def from_cue(doc: SubtitleDocument, cue: Cue) -> 'CueMarkup':
        mk = CueMarkup(doc=doc)
        parts: List[str] = []

        def is_structural(sp: SpanNode) -> bool:
            if sp.meta:
                return True
            try:
                spec = doc.specified_style(sp.style_refs, sp.inline_style)
            except Exception:
                return True
            return bool(spec.ruby) or bool(
                spec.text_combine and spec.text_combine != 'none')

        def walk(node: SpanNode):
            for ch in node.children:
                if ch.kind == 'text':
                    parts.append(_esc_text(ch.text))
                elif ch.kind == 'br':
                    parts.append('\n')
                elif ch.kind == 'span':
                    if is_structural(ch):
                        mk.atoms.append(ch)
                        n = len(mk.atoms)
                        parts.append(
                            f'{OPEN}≡{n} {_atom_preview(ch, doc)}{CLOSE}')
                        continue
                    bi = _is_pure_bi(ch.inline_style)
                    if bi is not None and \
                            all(r in doc.styles for r in ch.style_refs):
                        opens = list(ch.style_refs)
                        if bi[0]:
                            opens.append('b')
                        if bi[1]:
                            opens.append('i')
                        if not opens:            # transparent span
                            walk(ch)
                            continue
                        for sid in opens:
                            parts.append(OPEN + sid)
                        walk(ch)
                        for sid in reversed(opens):
                            parts.append(sid + CLOSE)
                    else:
                        mk.shells.append((list(ch.style_refs),
                                          ch.inline_style, dict(ch.meta)))
                        n = len(mk.shells)
                        parts.append(f'{OPEN}✎{n}')
                        walk(ch)
                        parts.append(f'✎{n}{CLOSE}')

        walk(cue.root)
        mk.text = ''.join(parts)
        return mk

    # ------------------------------------------------------------------ #
    def _token_ids(self) -> List[str]:
        ids = list(self.doc.styles.keys()) + list(_BI_IDS)
        ids += [f'✎{i + 1}' for i in range(len(self.shells))]
        return sorted(ids, key=len, reverse=True)

    def parse(self, text: str):
        """
        Tokenize + validate *text*.

        Returns (items, tokens) or (None, reason).
        items:  ('ch', c, active) | ('br', active) | ('atom', idx, active)
                with `active` = tuple of open ids at that point.
        tokens: (start, end, id, kind) with kind 'open'|'close'|'atom'
                for chip highlighting.
        """
        ids = self._token_ids()
        atom_lits = [f'{OPEN}≡{i + 1} {_atom_preview(a, self.doc)}{CLOSE}'
                     for i, a in enumerate(self.atoms)]
        items: List[tuple] = []
        tokens: List[tuple] = []
        active: List[str] = []
        pos, n = 0, len(text)
        while pos < n:
            ch = text[pos]
            matched = False
            if ch == OPEN:
                for i, lit in enumerate(atom_lits):
                    if text.startswith(lit, pos):
                        items.append(('atom', i, tuple(active)))
                        tokens.append((pos, pos + len(lit), f'≡{i + 1}',
                                       'atom'))
                        pos += len(lit)
                        matched = True
                        break
                if matched:
                    continue
                for sid in ids:
                    if text.startswith(OPEN + sid, pos):
                        if sid in active:
                            return None, (f'style "{sid}" can\'t be '
                                          f'nested inside itself')
                        active.append(sid)
                        tokens.append((pos, pos + 1 + len(sid), sid, 'open'))
                        pos += 1 + len(sid)
                        matched = True
                        break
                if not matched:
                    return None, 'unknown style name after ⟦'
                continue
            # close token?
            for sid in ids:
                if text.startswith(sid + CLOSE, pos):
                    if sid not in active:
                        return None, (f'"{sid}⟧" appears before its '
                                      f'matching ⟦ start')
                    active.remove(sid)
                    tokens.append((pos, pos + len(sid) + 1, sid, 'close'))
                    pos += len(sid) + 1
                    matched = True
                    break
            if matched:
                continue
            if ch == CLOSE:
                return None, 'stray ⟧ (broken token)'
            if ch == '\n':
                items.append(('br', tuple(active)))
            else:
                items.append(('ch', ch, tuple(active)))
            pos += 1
        if active:
            return None, f'style "{active[-1]}" is never closed'
        return (items, tokens), ''

    # ------------------------------------------------------------------ #
    def to_tree(self, text: str) -> Tuple[Optional[SpanNode], str]:
        parsed, reason = self.parse(text)
        if parsed is None:
            return None, reason
        items, _tokens = parsed

        def make_span(sid: str) -> SpanNode:
            sp = SpanNode(kind='span')
            if sid == 'b':
                sp.inline_style = Style(font_weight='bold')
            elif sid == 'i':
                sp.inline_style = Style(font_style='italic')
            elif sid.startswith('✎'):
                refs, inline, meta = self.shells[int(sid[1:]) - 1]
                sp.style_refs = list(refs)
                sp.inline_style = copy.deepcopy(inline)
                sp.meta = dict(meta)
            else:
                sp.style_refs = [sid]
            return sp

        def build(seg: List[tuple], depth: int) -> List[SpanNode]:
            out: List[SpanNode] = []
            i = 0
            while i < len(seg):
                it = seg[i]
                act = it[-1]
                if len(act) == depth:
                    if it[0] == 'ch':
                        # coalesce runs of plain chars
                        j = i
                        buf = []
                        while j < len(seg) and seg[j][0] == 'ch' and \
                                len(seg[j][-1]) == depth and \
                                seg[j][-1] == act:
                            buf.append(seg[j][1])
                            j += 1
                        out.append(SpanNode.text_node(
                            _unesc_text(''.join(buf))))
                        i = j
                    elif it[0] == 'br':
                        out.append(SpanNode.br())
                        i += 1
                    else:                       # atom
                        out.append(copy.deepcopy(self.atoms[it[1]]))
                        i += 1
                    continue
                head = act[depth]
                j = i
                while j < len(seg) and len(seg[j][-1]) > depth and \
                        seg[j][-1][depth] == head:
                    j += 1
                span = make_span(head)
                span.children = build(seg[i:j], depth + 1)
                out.append(span)
                i = j
            return out

        root = SpanNode(kind='root')
        root.children = build(items, 0)
        return root, ''


def _chip_color(sid: str) -> QColor:
    if sid in _BI_IDS:
        return QColor(110, 110, 118)
    if sid.startswith('≡'):
        return QColor(96, 82, 128)
    if sid.startswith('✎'):
        return QColor(128, 96, 64)
    h = (hash(sid) & 0xFFFF) / 0xFFFF
    r, g, b = colorsys.hls_to_rgb(h, 0.32, 0.55)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


#: custom text-object type hosting one style chip per character
TOKEN_OBJECT_TYPE = int(QTextFormat.ObjectTypes.UserObject) + 1
PROP_LITERAL = int(QTextFormat.Property.UserProperty) + 1
_OBJ = '￼'


def _literal_meta(lit: str) -> Tuple[str, str, str]:
    """literal -> (kind 'open'|'close'|'atom', sid, chip label)."""
    if lit.startswith(OPEN + '≡'):
        body = lit[1:-1]                       # ≡N preview
        sid = body.split(' ', 1)[0]
        prev = body.split(' ', 1)[1] if ' ' in body else ''
        return 'atom', sid, prev or sid
    if lit.startswith(OPEN):
        sid = lit[1:]
        return 'open', sid, f'{sid} ▸'
    sid = lit[:-1]
    return 'close', sid, f'◂ {sid}'


class _ChipHandler(QObject, QTextObjectInterface):
    """Paints ⟦style⟧ tokens as rounded, labeled boxes."""

    def __init__(self, edit: 'TokenStyleEdit'):
        super().__init__(edit)
        self.edit = edit

    def _font(self) -> QFont:
        f = QFont(self.edit.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        f.setBold(True)
        return f

    def intrinsicSize(self, doc, pos, fmt):
        from PyQt6.QtCore import QSizeF
        lit = fmt.property(PROP_LITERAL) or ''
        _k, _sid, label = _literal_meta(lit)
        fm = QFontMetricsF(self._font())
        return QSizeF(fm.horizontalAdvance(label) + 12, fm.height() + 4)

    def drawObject(self, painter, rect, doc, pos, fmt):
        lit = fmt.property(PROP_LITERAL) or ''
        kind, sid, label = _literal_meta(lit)
        color = _chip_color(sid)
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        r = rect.adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(r, 5, 5)
        painter.setPen(QColor(245, 245, 245))
        painter.setFont(self._font())
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()


class TokenStyleEdit(QTextEdit):
    """Cue text editor where style spans appear as draggable chip boxes.

    Each chip is ONE embedded character (a custom text object), so it's
    atomic by construction: the caret can't enter it, selections take it
    whole, and Qt's undo restores it intact. Click a chip to select it,
    drag it to move where the styling starts/ends. Deleting a chip also
    removes its partner (keeping the text); edits producing an invalid
    structure (end before start, style nested in itself) revert with the
    reason reported.
    """

    committed = pyqtSignal(object)          # rebuilt SpanNode root
    rejected = pyqtSignal(str)              # reason an edit was reverted

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFixedHeight(96)
        self.setToolTip(
            'The cue text with its style spans shown as chips '
            '(name ▸ … ◂ name). Click a chip to select it, drag to move '
            'it; overlaps auto-normalize. Deleting a chip removes its '
            'partner too (text is kept). Invalid arrangements revert.')
        self.markup: Optional[CueMarkup] = None
        self._doc_tokens: List[tuple] = []   # (pos, literal, kind, sid)
        self._loading = False
        self._reverting = False
        self._suspend = False
        self._press_chip: Optional[int] = None
        self._press_pos = None
        self._handler = _ChipHandler(self)
        self.document().documentLayout().registerHandler(
            TOKEN_OBJECT_TYPE, self._handler)
        self.textChanged.connect(self._changed)

    # ------------------------------------------------------------------ #
    # document <-> markup text
    # ------------------------------------------------------------------ #
    def _chip_format(self, literal: str) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setObjectType(TOKEN_OBJECT_TYPE)
        f.setProperty(PROP_LITERAL, literal)
        return f

    def _insert_markup(self, cursor: QTextCursor, text: str):
        """Insert markup text, materializing token literals as chips."""
        if self.markup is None:
            cursor.insertText(_esc_text(text))
            return
        parsed, _ = self.markup.parse(text)
        if parsed is None:
            # not balanced here — still materialize literal-by-literal
            pos = 0
            tokens = self._scan_literals(text)
            for s, e, lit in tokens:
                if s > pos:
                    cursor.insertText(_esc_text(text[pos:s]))
                cursor.insertText(_OBJ, self._chip_format(lit))
                cursor.setCharFormat(QTextCharFormat())
                pos = e
            cursor.insertText(_esc_text(text[pos:]))
            return
        _items, tokens = parsed
        pos = 0
        for s, e, sid, kind in tokens:
            if s > pos:
                cursor.insertText(text[pos:s])
            cursor.insertText(_OBJ, self._chip_format(text[s:e]))
            cursor.setCharFormat(QTextCharFormat())
            pos = e
        cursor.insertText(text[pos:])

    def _scan_literals(self, text: str) -> List[tuple]:
        """Best-effort token literal scan (no balance check) for pastes."""
        if self.markup is None:
            return []
        out = []
        ids = self.markup._token_ids()
        atom_lits = [f'{OPEN}≡{i + 1} {_atom_preview(a, self.markup.doc)}'
                     f'{CLOSE}' for i, a in enumerate(self.markup.atoms)]
        pos, n = 0, len(text)
        while pos < n:
            hit = None
            if text[pos] == OPEN:
                for lit in atom_lits:
                    if text.startswith(lit, pos):
                        hit = lit
                        break
                if hit is None:
                    for sid in ids:
                        if text.startswith(OPEN + sid, pos):
                            hit = OPEN + sid
                            break
            else:
                for sid in ids:
                    if text.startswith(sid + CLOSE, pos):
                        hit = sid + CLOSE
                        break
            if hit:
                out.append((pos, pos + len(hit), hit))
                pos += len(hit)
            else:
                pos += 1
        return out

    def _serialize(self) -> str:
        """Document → markup text; also rebuilds self._doc_tokens."""
        doc = self.document()
        parts: List[str] = []
        self._doc_tokens = []
        pos = 0
        block = doc.begin()
        first = True
        while block.isValid():
            if not first:
                parts.append('\n')
                pos += 1
            first = False
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                text = frag.text()
                if fmt.objectType() == TOKEN_OBJECT_TYPE:
                    lit = fmt.property(PROP_LITERAL) or ''
                    kind, sid, _lab = _literal_meta(lit)
                    for _ in range(len(text)):     # normally 1 char
                        parts.append(lit)
                        self._doc_tokens.append((pos, lit, kind, sid))
                        pos += 1
                else:
                    clean = text.replace(_OBJ, '')
                    parts.append(clean)
                    pos += len(clean)
                it += 1
            block = block.next()
        return ''.join(parts)

    # ------------------------------------------------------------------ #
    def load(self, markup: Optional[CueMarkup]):
        self._loading = True
        self.markup = markup
        self.clear()
        if markup is not None:
            cur = self.textCursor()
            self._insert_markup(cur, markup.text)
        self.document().clearUndoRedoStacks()
        self.setEnabled(markup is not None)
        self._loading = False
        self._revalidate(commit=False)

    # ------------------------------------------------------------------ #
    # chips: hit test, partner, smart delete
    # ------------------------------------------------------------------ #
    def _chip_at(self, doc_pos: int) -> Optional[tuple]:
        for t in self._doc_tokens:
            if t[0] == doc_pos:
                return t
        return None

    def _partner_pos(self, tok: tuple) -> Optional[int]:
        pos, lit, kind, sid = tok
        if kind == 'atom':
            return None
        if kind == 'open':
            for t in self._doc_tokens:
                if t[0] > pos and t[3] == sid and t[2] == 'close':
                    return t[0]
        else:
            best = None
            for t in self._doc_tokens:
                if t[0] < pos and t[3] == sid and t[2] == 'open':
                    best = t[0]
            return best
        return None

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if self._delete_smart(forward=ev.key() == Qt.Key.Key_Delete):
                return
        elif ev.text() and ev.text().isprintable() and \
                self.textCursor().hasSelection():
            if self._delete_smart(forward=True, insert=ev.text()):
                return
        super().keyPressEvent(ev)

    def _delete_smart(self, forward: bool, insert: str = '') -> bool:
        c = self.textCursor()
        if c.hasSelection():
            a, b = c.selectionStart(), c.selectionEnd()
        else:
            pos = c.position()
            a, b = (pos, pos + 1) if forward else (pos - 1, pos)
            if a < 0 or b > self.document().characterCount() - 1:
                return True
        # partners of chips inside [a,b) that live outside it
        extra = []
        for t in self._doc_tokens:
            if a <= t[0] < b:
                p = self._partner_pos(t)
                if p is not None and not (a <= p < b):
                    extra.append((p, p + 1))
        ranges = sorted(set(extra) | {(a, b)}, reverse=True)
        self._suspend = True
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            for x, y in ranges:
                cur.setPosition(x)
                cur.setPosition(y, QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
            if insert:
                cur.insertText(insert)
        finally:
            cur.endEditBlock()
            self._suspend = False
        self._revalidate(commit=True)
        return True

    # ------------------------------------------------------------------ #
    # mouse: click selects a chip; dragging it moves it
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            pt = ev.position().toPoint()
            cur = self.cursorForPosition(pt)
            pos = cur.position()
            # cursorForPosition gives the nearest boundary; the clicked
            # character is at pos when the click is right of it, pos-1
            # when left of it
            target = pos if pt.x() >= self.cursorRect(cur).x() else pos - 1
            chip = self._chip_at(target)
            if chip is not None:
                sel = self.textCursor()
                sel.setPosition(chip[0])
                sel.setPosition(chip[0] + 1,
                                QTextCursor.MoveMode.KeepAnchor)
                self.setTextCursor(sel)
                self._press_chip = chip[0]
                self._press_pos = ev.position().toPoint()
                ev.accept()
                return
        self._press_chip = None
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press_chip is not None and \
                (ev.buttons() & Qt.MouseButton.LeftButton):
            from PyQt6.QtWidgets import QApplication
            if (ev.position().toPoint() - self._press_pos
                    ).manhattanLength() >= QApplication.startDragDistance():
                self._drag_chip()
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._press_chip = None
        super().mouseReleaseEvent(ev)

    def _drag_chip(self):
        from PyQt6.QtCore import QMimeData
        from PyQt6.QtGui import QDrag
        src = self.textCursor()
        if not src.hasSelection():
            return
        self._press_chip = None
        mime = self.createMimeDataFromSelection()
        drag = QDrag(self)
        drag.setMimeData(mime)
        # keep positions honest: src cursor auto-adjusts through edits
        self._suspend = True
        try:
            result = drag.exec(Qt.DropAction.MoveAction |
                               Qt.DropAction.CopyAction,
                               Qt.DropAction.MoveAction)
            if result == Qt.DropAction.MoveAction and \
                    self._drop_happened_here:
                src.removeSelectedText()
        finally:
            self._drop_happened_here = False
            self._suspend = False
        self._revalidate(commit=True)

    _drop_happened_here = False

    # ------------------------------------------------------------------ #
    # clipboard / drops carry markup text
    # ------------------------------------------------------------------ #
    def createMimeDataFromSelection(self):
        from PyQt6.QtCore import QMimeData
        c = self.textCursor()
        a, b = c.selectionStart(), c.selectionEnd()
        text = self._serialize()
        # doc positions map 1:1 onto serialized token list positions
        out = []
        pos = 0
        i = 0
        toks = {t[0]: t[1] for t in self._doc_tokens}
        for ch_pos in range(a, b):
            if ch_pos in toks:
                out.append(toks[ch_pos])
            else:
                out.append(self._doc_char(ch_pos))
        m = QMimeData()
        m.setText(''.join(out))
        return m

    def _doc_char(self, pos: int) -> str:
        c = self.textCursor()
        c.setPosition(pos)
        c.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
        t = c.selectedText()
        return '\n' if t == '\u2029' else t   # para separator

    def insertFromMimeData(self, source):
        self._drop_happened_here = True
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            self._insert_markup(cur, source.text() or '')
        finally:
            cur.endEditBlock()

    # ------------------------------------------------------------------ #
    # toolbar operations
    # ------------------------------------------------------------------ #
    def wrap_selection(self, sid: str):
        if self.markup is None:
            return
        c = self.textCursor()
        a, b = (c.selectionStart(), c.selectionEnd()) \
            if c.hasSelection() else (c.position(), c.position())
        self._suspend = True
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            cur.setPosition(b)
            cur.insertText(_OBJ, self._chip_format(sid + CLOSE))
            cur.setPosition(a)
            cur.insertText(_OBJ, self._chip_format(OPEN + sid))
        finally:
            cur.endEditBlock()
            self._suspend = False
        if a == b:
            cur.setPosition(a + 1)
            self.setTextCursor(cur)
        self._revalidate(commit=True)

    def remove_style_at(self, pos: int, sid: str):
        opens = [t for t in self._doc_tokens
                 if t[3] == sid and t[2] == 'open' and t[0] <= pos]
        if not opens:
            return
        o = opens[-1]
        p = self._partner_pos(o)
        ranges = sorted({(o[0], o[0] + 1)} |
                        ({(p, p + 1)} if p is not None else set()),
                        reverse=True)
        self._suspend = True
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            for x, y in ranges:
                cur.setPosition(x)
                cur.setPosition(y, QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
        finally:
            cur.endEditBlock()
            self._suspend = False
        self._revalidate(commit=True)

    def contextMenuEvent(self, ev):
        menu = self.createStandardContextMenu()
        pos = self.cursorForPosition(ev.pos()).position()
        active = []
        for t in self._doc_tokens:
            if t[2] == 'open' and t[0] < pos:
                p = self._partner_pos(t)
                if p is None or p >= pos:
                    active.append(t[3])
        if active:
            menu.addSeparator()
            for sid in active:
                act = menu.addAction(f'Remove style "{sid}" here')
                act.triggered.connect(
                    lambda _=False, s=sid, p=pos:
                    self.remove_style_at(p, s))
        menu.exec(ev.globalPos())

    # ------------------------------------------------------------------ #
    def _changed(self):
        if self._loading or self._reverting or self._suspend or \
                self.markup is None:
            return
        self._revalidate(commit=True)

    def _revalidate(self, commit: bool):
        if self.markup is None:
            self._doc_tokens = []
            self.setExtraSelections([])
            return
        text = self._serialize()
        parsed, reason = self.markup.parse(text)
        if parsed is None:
            self._reverting = True
            try:
                if self.document().isUndoAvailable():
                    self.undo()
                else:
                    cur = self.textCursor()
                    cur.select(QTextCursor.SelectionType.Document)
                    self._insert_markup(cur, self.markup.text)
            finally:
                self._reverting = False
            self.rejected.emit(reason)
            text = self._serialize()
            parsed, _ = self.markup.parse(text)
            if parsed is None:
                return
        self._decorate()
        if commit:
            tree, _ = self.markup.to_tree(text)
            if tree is not None:
                self.committed.emit(tree)

    def _decorate(self):
        """Live bold/italic preview of the text between chips."""
        sels = []
        active: List[str] = []
        run_start: Optional[int] = None
        run_active: tuple = ()
        end_pos = self.document().characterCount() - 1
        tok_map = {t[0]: t for t in self._doc_tokens}

        def flush(endp):
            nonlocal run_start
            if run_start is None or run_start >= endp:
                run_start = None
                return
            f = QTextCharFormat()
            if 'b' in run_active:
                f.setFontWeight(QFont.Weight.Bold)
            if 'i' in run_active:
                f.setFontItalic(True)
            if f.propertyCount():
                sel = QTextEdit.ExtraSelection()
                c = self.textCursor()
                c.setPosition(run_start)
                c.setPosition(endp, QTextCursor.MoveMode.KeepAnchor)
                sel.cursor = c
                sel.format = f
                sels.append(sel)
            run_start = None

        p = 0
        while p < end_pos:
            t = tok_map.get(p)
            if t is not None:
                flush(p)
                if t[2] == 'open':
                    active.append(t[3])
                elif t[2] == 'close' and t[3] in active:
                    active.remove(t[3])
                p += 1
                continue
            if run_start is None:
                run_start = p
                run_active = tuple(active)
            p += 1
        flush(end_pos)
        self.setExtraSelections(sels)


class SelectedCuePane(QWidget):
    """Bound to the current (last-selected) cue."""

    changed = pyqtSignal()          # cue styling edited

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Optional[SubtitleDocument] = None
        self.cue: Optional[Cue] = None
        self._loading = False

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(8, 2, 4, 4)
        cl.setSpacing(3)

        self.lbl_cue = QLabel('No cue selected')
        self.lbl_cue.setStyleSheet('color:#9a9a9a;')
        cl.addWidget(self.lbl_cue)

        row = QHBoxLayout()
        row.addWidget(QLabel('Cue styles:'))
        self.ed_refs = QLineEdit()
        self.ed_refs.setPlaceholderText(
            'cue-level (<p>) style ids — empty = default')
        self.ed_refs.setToolTip(
            'Named styles applied to the WHOLE cue (the <p> element), '
            'including ones inherited from <body>/<div>. For styling '
            'parts of the text, use the editor below.')
        row.addWidget(self.ed_refs, 1)
        self.btn_add = QPushButton('Add style ▾')
        self.btn_add.setToolTip(
            'Wrap the selected text below in one of the document\'s '
            'named styles (no selection: inserts an empty style at the '
            'cursor).')
        self.btn_b = QToolButton()
        self.btn_b.setText('B')
        f = self.btn_b.font()
        f.setBold(True)
        self.btn_b.setFont(f)
        self.btn_b.setToolTip('Bold the selection (independent of styles)')
        self.btn_i = QToolButton()
        self.btn_i.setText('I')
        f = self.btn_i.font()
        f.setItalic(True)
        self.btn_i.setFont(f)
        self.btn_i.setToolTip('Italicize the selection (independent of '
                              'styles)')
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_b)
        row.addWidget(self.btn_i)
        cl.addLayout(row)

        self.editor = TokenStyleEdit()
        cl.addWidget(self.editor)
        self.lbl_hint = QLabel('')
        self.lbl_hint.setStyleSheet('color:#c8a050; font-size:11px;')
        self.lbl_hint.setWordWrap(True)
        cl.addWidget(self.lbl_hint)

        self.section = CollapsibleSection('Selected cue (styled text)',
                                          content, expanded=False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.addWidget(self.section)

        self.ed_refs.editingFinished.connect(self._refs_edited)
        self.btn_add.clicked.connect(self._add_style_menu)
        self.btn_b.clicked.connect(lambda: self.editor.wrap_selection('b'))
        self.btn_i.clicked.connect(lambda: self.editor.wrap_selection('i'))
        self.editor.committed.connect(self._tree_committed)
        self.editor.rejected.connect(self._edit_rejected)

    # ------------------------------------------------------------------ #
    def set_cue(self, doc: Optional[SubtitleDocument], cue: Optional[Cue],
                n_selected: int = 1):
        self._loading = True
        self.doc = doc
        self.cue = cue
        self.lbl_hint.setText('')
        if cue is None or doc is None:
            self.lbl_cue.setText('No cue selected')
            self.ed_refs.setText('')
            self.ed_refs.setEnabled(False)
            self.editor.load(None)
            self._loading = False
            return
        extra = f'   ({n_selected} selected — this edits the current one)' \
            if n_selected > 1 else ''
        self.lbl_cue.setText(
            f'{format_display_time(cue.begin_ms)} → '
            f'{format_display_time(cue.end_ms)}{extra}')
        self.ed_refs.setEnabled(True)
        self.ed_refs.setText(' '.join(cue.style_refs))
        self.editor.load(CueMarkup.from_cue(doc, cue))
        self._loading = False

    # ------------------------------------------------------------------ #
    def _refs_edited(self):
        if self._loading or self.cue is None or self.doc is None:
            return
        refs = parse_style_refs(self.doc, self.ed_refs.text())
        if refs is None:
            self.ed_refs.setText(' '.join(self.cue.style_refs))
            self.lbl_hint.setText('Unknown style id — reverted. Styles '
                                  'are managed in Settings → Styles.')
            return
        if refs != self.cue.style_refs:
            self.cue.style_refs = refs
            self.changed.emit()

    def _add_style_menu(self):
        if self.doc is None or self.cue is None:
            return
        if not self.doc.styles:
            self.lbl_hint.setText('This document has no named styles yet '
                                  '— add some in Settings → Styles.')
            return
        menu = QMenu(self)
        for sid in sorted(self.doc.styles.keys()):
            menu.addAction(sid)
        act = menu.exec(self.btn_add.mapToGlobal(
            self.btn_add.rect().bottomLeft()))
        if act is not None:
            self.editor.wrap_selection(act.text())

    def _tree_committed(self, root):
        if self._loading or self.cue is None:
            return
        self.cue.root = root
        self.lbl_hint.setText('')
        self.changed.emit()

    def _edit_rejected(self, reason: str):
        self.lbl_hint.setText(f'Edit reverted: {reason}.')
