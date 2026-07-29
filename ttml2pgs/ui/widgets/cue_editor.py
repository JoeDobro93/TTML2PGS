"""
Selected-cue pane: a collapsible editor for the current cue's styling.

Reveals what the cue actually carries — its named style references
(including ones inherited from TTML <body>/<div> containers, which the
parser folds into every cue) and its inline <p> style — and lets both be
edited. Collapsed by default so it takes no space; sits between the cue
table and the sources pane.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QScrollArea, QToolButton, QVBoxLayout, QWidget)

from ...core.model import Cue, Style, SubtitleDocument
from ...core.timing import format_display_time
from .cue_table import parse_style_refs
from .settings_panel import CollapsibleSection, StyleEditor


class SelectedCuePane(QWidget):
    """Bound to the current (last-selected) cue."""

    changed = pyqtSignal()          # cue styling edited

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Optional[SubtitleDocument] = None
        self.cue: Optional[Cue] = None
        self._inline: Optional[Style] = None
        self._loading = False

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(8, 2, 4, 4)
        cl.setSpacing(3)

        self.lbl_cue = QLabel('No cue selected')
        self.lbl_cue.setStyleSheet('color:#9a9a9a;')
        cl.addWidget(self.lbl_cue)

        row = QHBoxLayout()
        row.addWidget(QLabel('Named styles:'))
        self.ed_refs = QLineEdit()
        self.ed_refs.setPlaceholderText(
            'space-separated style ids — empty = default (Initials)')
        self.ed_refs.setToolTip(
            'The named styles applied to this cue, outermost first — '
            'including ones inherited from TTML <body>/<div> containers. '
            'Edit ids or clear to defer to the document Initials.')
        self.btn_add = QToolButton()
        self.btn_add.setText('+')
        self.btn_add.setToolTip('Append one of the document\'s styles')
        row.addWidget(self.ed_refs, 1)
        row.addWidget(self.btn_add)
        cl.addLayout(row)

        hint = QLabel('Inline <p> style — checked rows are set on THIS cue '
                      'and win over its named styles:')
        hint.setStyleSheet('color:#9a9a9a; font-size:11px;')
        hint.setWordWrap(True)
        cl.addWidget(hint)

        self.style_editor = StyleEditor()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.style_editor)
        scroll.setFixedHeight(230)
        cl.addWidget(scroll)

        self.section = CollapsibleSection('Selected cue (styles)', content,
                                          expanded=False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.addWidget(self.section)

        self.ed_refs.editingFinished.connect(self._refs_edited)
        self.btn_add.clicked.connect(self._add_style_menu)
        self.style_editor.changed.connect(self._inline_edited)

    # ------------------------------------------------------------------ #
    def set_cue(self, doc: Optional[SubtitleDocument], cue: Optional[Cue],
                n_selected: int = 1):
        self._loading = True
        self.doc = doc
        self.cue = cue
        if cue is None or doc is None:
            self.lbl_cue.setText('No cue selected')
            self.ed_refs.setText('')
            self.ed_refs.setEnabled(False)
            self.btn_add.setEnabled(False)
            self.style_editor.load(None)
            self._inline = None
            self._loading = False
            return
        snippet = cue.plain_text().replace('\n', ' ⏎ ')
        if len(snippet) > 60:
            snippet = snippet[:57] + '…'
        extra = f'  (1 of {n_selected} selected — edits apply to this ' \
                f'one; use the table columns for bulk changes)' \
            if n_selected > 1 else ''
        self.lbl_cue.setText(
            f'{format_display_time(cue.begin_ms)} → '
            f'{format_display_time(cue.end_ms)}   {snippet}{extra}')
        self.ed_refs.setEnabled(True)
        self.btn_add.setEnabled(True)
        self.ed_refs.setText(' '.join(cue.style_refs))
        # bind the editor to the cue's inline style (created lazily,
        # detached again when everything is unchecked)
        self._inline = cue.inline_style if cue.inline_style is not None \
            else Style()
        self.style_editor.load(self._inline)
        self._loading = False

    # ------------------------------------------------------------------ #
    def _refs_edited(self):
        if self._loading or self.cue is None or self.doc is None:
            return
        refs = parse_style_refs(self.doc, self.ed_refs.text())
        if refs is None:
            # unknown id: revert to the cue's actual refs
            self.ed_refs.setText(' '.join(self.cue.style_refs))
            return
        if refs != self.cue.style_refs:
            self.cue.style_refs = refs
            self.changed.emit()

    def _add_style_menu(self):
        if self.doc is None or self.cue is None or not self.doc.styles:
            return
        menu = QMenu(self)
        for sid in sorted(self.doc.styles.keys()):
            menu.addAction(sid)
        act = menu.exec(self.btn_add.mapToGlobal(
            self.btn_add.rect().bottomLeft()))
        if act is None:
            return
        refs = self.cue.style_refs + [act.text()]
        self.cue.style_refs = refs
        self.ed_refs.setText(' '.join(refs))
        self.changed.emit()

    def _inline_edited(self):
        if self._loading or self.cue is None or self._inline is None:
            return
        self.cue.inline_style = None if self._inline.is_empty() \
            else self._inline
        self.changed.emit()
