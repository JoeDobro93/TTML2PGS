"""
Filename-friendly text elision for table/tree cells.

Qt's default word-wrapped cell painting hides text at word boundaries
when a column narrows — space-less names ("For_All_Mankind_…") vanish
almost immediately. These helpers give character-level elision that
always keeps the trailing extension chain readable:

    For_All_Mankind_S02E04_Pathfinder.en.forced.vtt
    →  For_All_Mank….en.forced.vtt          (narrow column)
"""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QStyledItemDelegate

#: trailing dot-chain: '.ja.vtt', '.en.forced.vtt', '.ja+en.forced.sup'
_EXT_RE = re.compile(r'(?:\.[A-Za-z0-9+\-]{1,10}){1,4}$')


def elide_filename(fm, text: str, width: int) -> str:
    """Longest prefix that fits + '…' + the FULL extension chain; falls
    back to plain middle elision when even the chain can't fit."""
    if fm.horizontalAdvance(text) <= width:
        return text
    m = _EXT_RE.search(text)
    if m and m.start() > 0:
        suffix = m.group(0)
        head = text[:m.start()]
        tail_w = fm.horizontalAdvance('…' + suffix)
        if tail_w <= width:
            lo, hi = 0, len(head)
            while lo < hi:                       # longest fitting prefix
                mid = (lo + hi + 1) // 2
                if fm.horizontalAdvance(head[:mid]) + tail_w <= width:
                    lo = mid
                else:
                    hi = mid - 1
            return head[:lo] + '…' + suffix
    return fm.elidedText(text, Qt.TextElideMode.ElideMiddle, width)


class FileElideDelegate(QStyledItemDelegate):
    """Paints file-name cells with extension-preserving elision."""

    PAD = 8                                      # cell text margins

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.text = elide_filename(
            option.fontMetrics, option.text,
            max(0, option.rect.width() - self.PAD))
        option.textElideMode = Qt.TextElideMode.ElideNone


def min_chars_width(widget, n: int = 20) -> int:
    """Width of n 'o' characters in the widget's font — the floor for
    file-name columns."""
    return widget.fontMetrics().horizontalAdvance('o' * n)


def enforce_min_section_width(header, columns, min_width: int):
    """Snap the given header sections back to min_width when the user
    drags them narrower."""
    def _on_resize(idx, _old, new):
        if idx in columns and 0 < new < min_width:
            header.blockSignals(True)
            header.resizeSection(idx, min_width)
            header.blockSignals(False)
    header.sectionResized.connect(_on_resize)
