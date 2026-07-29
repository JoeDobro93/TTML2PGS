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

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
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


class TokenStyleEdit(QTextEdit):
    """QTextEdit hosting the token markup with validation-revert."""

    committed = pyqtSignal(object)          # rebuilt SpanNode root
    rejected = pyqtSignal(str)              # reason an edit was reverted

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFixedHeight(96)
        self.setToolTip(
            'The cue text with visible style tokens (⟦style … style⟧). '
            'Drag tokens to move where styling starts/ends; overlaps '
            'auto-normalize. Deleting a token also removes its partner '
            '(text is kept). Invalid edits revert.')
        self.markup: Optional[CueMarkup] = None
        self._tokens: List[tuple] = []
        self._loading = False
        self._reverting = False
        self._last_good = ''
        self.textChanged.connect(self._changed)
        self.cursorPositionChanged.connect(self._snap_cursor)

    # ------------------------------------------------------------------ #
    def load(self, markup: Optional[CueMarkup]):
        self._loading = True
        self.markup = markup
        self.setPlainText(markup.text if markup else '')
        self.document().clearUndoRedoStacks()
        self.setEnabled(markup is not None)
        self._last_good = markup.text if markup else ''
        self._loading = False
        self._revalidate(commit=False)

    # ------------------------------------------------------------------ #
    def _token_at(self, pos: int) -> Optional[tuple]:
        for t in self._tokens:
            if t[0] < pos < t[1]:
                return t
        return None

    def _token_span(self, a: int, b: int) -> Tuple[int, int]:
        """Expand [a,b) so it never splits a token."""
        for t in self._tokens:
            if t[0] < a < t[1]:
                a = t[0]
            if t[0] < b < t[1]:
                b = t[1]
        return a, b

    def _snap_cursor(self):
        if self._loading or self._reverting:
            return
        c = self.textCursor()
        if c.hasSelection():
            return
        t = self._token_at(c.position())
        if t is not None:
            c.setPosition(t[1] if (c.position() - t[0]) > (t[1] - t[0]) / 2
                          else t[0])
            self.blockSignals(True)
            self.setTextCursor(c)
            self.blockSignals(False)

    # ------------------------------------------------------------------ #
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
        """Token-aware delete: expand partial tokens, remove partners of
        fully-deleted tokens. Returns True when handled."""
        c = self.textCursor()
        if c.hasSelection():
            a, b = c.selectionStart(), c.selectionEnd()
        else:
            pos = c.position()
            a, b = (pos, pos + 1) if forward else (pos - 1, pos)
            if a < 0 or b > len(self.toPlainText()):
                return True
        a, b = self._token_span(a, b)
        # partners of tokens fully inside [a,b) that live outside it
        doomed = [t for t in self._tokens if a <= t[0] and t[1] <= b]
        extra: List[Tuple[int, int]] = []
        for t in doomed:
            partner = self._partner(t)
            if partner and not (a <= partner[0] and partner[1] <= b):
                extra.append((partner[0], partner[1]))
        ranges = sorted(set(extra) | {(a, b)}, reverse=True)
        cur = self.textCursor()
        cur.beginEditBlock()
        for x, y in ranges:
            cur.setPosition(x)
            cur.setPosition(y, QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
        if insert:
            cur.insertText(insert)
        cur.endEditBlock()
        return True

    def _partner(self, tok: tuple) -> Optional[tuple]:
        s, e, sid, kind = tok
        if kind == 'atom':
            return None
        if kind == 'open':
            for t in self._tokens:
                if t[0] <= s or t[2] != sid:
                    continue
                if t[3] == 'close':
                    return t
        else:
            best = None
            for t in self._tokens:
                if t[1] <= s and t[2] == sid and t[3] == 'open':
                    best = t
            return best
        return None

    def insertFromMimeData(self, source):
        # drops land outside tokens
        c = self.textCursor()
        t = self._token_at(c.position())
        if t is not None and not c.hasSelection():
            c.setPosition(t[1])
            self.setTextCursor(c)
        super().insertFromMimeData(source)

    # ------------------------------------------------------------------ #
    def wrap_selection(self, sid: str):
        """Toolbar entry: wrap the selection (or cursor) in ⟦sid…sid⟧."""
        if self.markup is None:
            return
        c = self.textCursor()
        a, b = (c.selectionStart(), c.selectionEnd()) \
            if c.hasSelection() else (c.position(), c.position())
        a, b = self._token_span(a, b)
        cur = self.textCursor()
        cur.beginEditBlock()
        cur.setPosition(b)
        cur.insertText(sid + CLOSE)
        cur.setPosition(a)
        cur.insertText(OPEN + sid)
        cur.endEditBlock()
        # cursor between the tokens for an empty wrap
        if a == b:
            cur.setPosition(a + len(OPEN + sid))
            self.setTextCursor(cur)

    def remove_style_at(self, pos: int, sid: str):
        pair = [t for t in self._tokens if t[2] == sid]
        opens = [t for t in pair if t[3] == 'open' and t[0] <= pos]
        if not opens:
            return
        o = opens[-1]
        cl = self._partner(o)
        ranges = sorted([r for r in (o, cl) if r], reverse=True)
        cur = self.textCursor()
        cur.beginEditBlock()
        for t in ranges:
            cur.setPosition(t[0])
            cur.setPosition(t[1], QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
        cur.endEditBlock()

    def contextMenuEvent(self, ev):
        menu = self.createStandardContextMenu()
        if self.markup is not None:
            pos = self.cursorForPosition(ev.pos()).position()
            parsed, _ = self.markup.parse(self.toPlainText())
            if parsed:
                items, _tok = parsed
                # styles active at pos: count chars up to pos… simpler:
                # use tokens: opens before pos without close before pos
                active = []
                for t in self._tokens:
                    if t[3] == 'open' and t[1] <= pos:
                        p = self._partner(t)
                        if p is None or p[0] >= pos:
                            active.append(t[2])
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
        if self._loading or self._reverting or self.markup is None:
            return
        self._revalidate(commit=True)

    def _revalidate(self, commit: bool):
        if self.markup is None:
            self._tokens = []
            self.setExtraSelections([])
            return
        text = self.toPlainText()
        parsed, reason = self.markup.parse(text)
        if parsed is None:
            self._reverting = True
            try:
                if self.document().isUndoAvailable():
                    self.undo()
                else:
                    self.setPlainText(self._last_good)
            finally:
                self._reverting = False
            self.rejected.emit(reason)
            # re-highlight the restored text
            parsed, _ = self.markup.parse(self.toPlainText())
            if parsed is None:
                self._tokens = []
                return
            text = self.toPlainText()
        items, tokens = parsed
        self._tokens = tokens
        self._last_good = text
        self._decorate(items, tokens)
        if commit:
            tree, _ = self.markup.to_tree(text)
            if tree is not None:
                self.committed.emit(tree)

    def _decorate(self, items, tokens):
        sels = []
        for s, e, sid, kind in tokens:
            f = QTextCharFormat()
            f.setBackground(_chip_color(sid))
            f.setForeground(QColor(240, 240, 240))
            f.setFontWeight(QFont.Weight.Bold)
            sel = QTextEdit.ExtraSelection()
            c = self.textCursor()
            c.setPosition(s)
            c.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = c
            sel.format = f
            sels.append(sel)
        # live formatting of the text runs (bold/italic from b/i tokens)
        pos = 0
        text = self.toPlainText()
        idx = 0
        starts = {t[0]: t for t in tokens}
        run_start = None
        run_active: tuple = ()

        def flush(endpos):
            nonlocal run_start
            if run_start is None or run_start >= endpos:
                run_start = None
                return
            f = QTextCharFormat()
            if 'b' in run_active:
                f.setFontWeight(QFont.Weight.Bold)
            if 'i' in run_active:
                f.setFontItalic(True)
            if f != QTextCharFormat():
                sel = QTextEdit.ExtraSelection()
                c = self.textCursor()
                c.setPosition(run_start)
                c.setPosition(endpos, QTextCursor.MoveMode.KeepAnchor)
                sel.cursor = c
                sel.format = f
                sels.append(sel)
            run_start = None

        # walk chars using token map to track b/i activity
        active: List[str] = []
        while pos < len(text):
            t = starts.get(pos)
            if t is not None:
                flush(pos)
                if t[3] == 'open':
                    active.append(t[2])
                elif t[3] == 'close' and t[2] in active:
                    active.remove(t[2])
                pos = t[1]
                continue
            if run_start is None:
                run_start = pos
                run_active = tuple(active)
            pos += 1
        flush(pos)
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
