"""
Sources pane: the open subtitle files, their matched videos and render
targets. Switching rows switches the whole workspace (cues, preview,
settings) to that document.
"""

from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QMenu, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from ...core.parsers import SUBTITLE_EXTENSIONS
from ...core.timing import fps_label
from ..state import AppState, DocumentSession

COL_NAME, COL_LANG, COL_VIDEO, COL_RES, COL_HDR, COL_SRC_FPS, COL_TGT_FPS, \
    COL_CONFORM, COL_OFFSET, COL_OUT = range(10)


def _parse_fps(text: str):
    """'23.976', '24000/1001' or '24' → Fraction | None."""
    from fractions import Fraction
    from ...core.timing import normalize_fps
    text = (text or '').strip()
    if not text or text in ('?', '-'):
        return None
    try:
        if '/' in text:
            n, d = text.split('/')
            return Fraction(int(n), int(d))
        return normalize_fps(float(text))
    except (ValueError, ZeroDivisionError):
        return None


class SourcesPane(QWidget):
    session_activated = pyqtSignal(int)        # index into state.sessions
    session_changed = pyqtSignal(int)          # data edited (video/offset…)
    render_requested = pyqtSignal(int)         # render this one
    render_all_requested = pyqtSignal()
    add_sup_requested = pyqtSignal(str, str)   # video_path, sup_path

    HEADERS = ['Subtitle', 'Lang', 'Video', 'Res', 'HDR', 'Src fps',
               'Tgt fps', 'Conform', 'Offset ms', 'Output']

    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        b_add = QPushButton('Add subtitles…')
        b_folder = QPushButton('Add folder…')
        b_sup = QPushButton('Queue external .sup…')
        b_close = QPushButton('Close selected')
        bar.addWidget(b_add)
        bar.addWidget(b_folder)
        bar.addWidget(b_sup)
        bar.addWidget(b_close)
        bar.addStretch()
        from PyQt6.QtWidgets import QCheckBox
        self.chk_selected_only = QCheckBox('Only checked cues')
        self.chk_selected_only.setToolTip(
            'Render only cues whose checkbox is ticked in the cue pane '
            '(applies to the render you queue next).')
        bar.addWidget(self.chk_selected_only)
        self.b_render = QPushButton('Add to queue')
        self.b_render.setToolTip(
            'Add the selected file to the render queue. Start it from '
            'the queue panel (Render all / Render selected).')
        self.b_render_all = QPushButton('Add ALL to queue')
        self.b_render_all.setStyleSheet('font-weight:bold;')
        self.b_render_all.setToolTip(
            'Add every open file to the render queue. Start them from '
            'the queue panel.')
        bar.addWidget(self.b_render)
        bar.addWidget(self.b_render_all)
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_VIDEO, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_OUT, QHeaderView.ResizeMode.Stretch)
        for c, w in ((COL_LANG, 46), (COL_RES, 78), (COL_HDR, 40),
                     (COL_SRC_FPS, 64), (COL_TGT_FPS, 64),
                     (COL_CONFORM, 120), (COL_OFFSET, 70)):
            self.table.setColumnWidth(c, w)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.table)

        b_add.clicked.connect(self._add_files)
        b_folder.clicked.connect(self._add_folder)
        b_sup.clicked.connect(self._add_external_sup)
        b_close.clicked.connect(self._close_selected)
        self.b_render.clicked.connect(self._render_selected)
        self.b_render_all.clicked.connect(self.render_all_requested.emit)
        self.table.currentCellChanged.connect(self._row_changed)
        self.table.cellDoubleClicked.connect(self._cell_double)
        self.table.itemChanged.connect(self._item_edited)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self._loading = False

    # ------------------------------------------------------------------ #
    def selected_cues_only(self) -> bool:
        return self.chk_selected_only.isChecked()

    # ------------------------------------------------------------------ #
    def refresh(self):
        self._loading = True
        st = self.state
        self.table.setRowCount(len(st.sessions))
        for row, sess in enumerate(st.sessions):
            self._fill_row(row, sess)
        if 0 <= st.active_index < len(st.sessions):
            self.table.selectRow(st.active_index)
        self._loading = False

    def _fill_row(self, row: int, sess: DocumentSession):
        def put(col, text, editable=False, tip=None):
            it = QTableWidgetItem(text)
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if tip:
                it.setToolTip(tip)
            self.table.setItem(row, col, it)
            return it

        put(COL_NAME, sess.display_name, tip=sess.sub_path)
        put(COL_LANG, sess.doc.language, editable=True,
            tip='Language code — double-click to edit. Determines which '
                'override set and fonts apply.')
        put(COL_VIDEO,
            os.path.basename(sess.video_path) if sess.video_path
            else '(double-click to pick)',
            tip=sess.video_path or 'No video bound')
        vi = sess.video_info
        put(COL_RES, f"{vi.width}x{vi.height}" if vi else '-')
        hdr = put(COL_HDR, 'HDR' if (vi and vi.is_hdr) else
                  ('SDR' if vi else '-'))
        if vi and vi.is_hdr:
            hdr.setForeground(QBrush(QColor('#69d26a')))
        if sess.manual_src_fps:
            put(COL_SRC_FPS, fps_label(sess.manual_src_fps), editable=True,
                tip='Manually forced source frame rate (double-click to '
                    'edit, clear to reset)')
        elif sess.doc.fps:
            put(COL_SRC_FPS, fps_label(sess.doc.fps), editable=True,
                tip='Frame rate declared by the subtitle file. '
                    'Double-click to force a different one.')
        else:
            vi_fps = sess.video_info.fps if sess.video_info else None
            s = put(COL_SRC_FPS,
                    fps_label(vi_fps) if vi_fps else '?', editable=True,
                    tip='The subtitle file declares no frame rate — '
                        'assumed to match the video (no conform). '
                        'Double-click to force a source rate (e.g. 25 '
                        'for PAL-timed subs).')
            s.setForeground(QBrush(QColor('#8a8a8a')))
        put(COL_TGT_FPS, fps_label(sess.target_fps()), editable=True,
            tip='Target frame rate for the .sup (probed from the video). '
                'Double-click to force a different rate — timestamps are '
                'conformed src→target at render time.')
        plan = sess.retime_plan()
        c = put(COL_CONFORM, plan.description if plan else '—',
                tip='Automatic frame-rate conform applied at render time '
                    '(based on subtitle vs video fps). Use Cue pane → '
                    'Time tools for manual conform.')
        if plan:
            c.setForeground(QBrush(QColor('#e0b040')))
        put(COL_OFFSET, f"{sess.offset_ms:g}", editable=True)
        put(COL_OUT, os.path.basename(sess.out_path), editable=True,
            tip=sess.out_path)

    # ------------------------------------------------------------------ #
    def _add_files(self):
        exts = ' '.join(f'*{e}' for e in SUBTITLE_EXTENSIONS) + ' *.t2p'
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Open subtitles', '', f'Subtitles ({exts})')
        for p in paths:
            self._open(p)

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Add folder')
        if not folder:
            return
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(SUBTITLE_EXTENSIONS):
                self._open(os.path.join(folder, fn))

    def _open(self, path: str):
        try:
            self.state.open_subtitle(path)
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, 'Open failed', f'{path}\n\n{e}')
            return
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    def _add_external_sup(self):
        sup, _ = QFileDialog.getOpenFileName(
            self, 'Pick a .sup to queue for muxing', '', 'PGS (*.sup)')
        if not sup:
            return
        video, _ = QFileDialog.getOpenFileName(
            self, 'Target video for this .sup', '',
            'Video (*.mkv *.mp4 *.m4v *.ts *.m2ts)')
        if not video:
            return
        self.add_sup_requested.emit(video, sup)

    def _close_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self.state.close_session(row)
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    def _render_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.render_requested.emit(row)

    # ------------------------------------------------------------------ #
    def _row_changed(self, row, col, prow, pcol):
        if self._loading or row < 0 or row == self.state.active_index:
            return
        self.state.active_index = row
        self.session_activated.emit(row)

    def _cell_double(self, row, col):
        if col == COL_VIDEO and 0 <= row < len(self.state.sessions):
            path, _ = QFileDialog.getOpenFileName(
                self, 'Bind video', '',
                'Video (*.mkv *.mp4 *.m4v *.mov *.ts *.m2ts *.avi *.webm)')
            if path:
                self.state.sessions[row].bind_video(path)
                self.refresh()
                self.session_changed.emit(row)

    def _item_edited(self, item: QTableWidgetItem):
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self.state.sessions)):
            return
        sess = self.state.sessions[row]
        if col == COL_OFFSET:
            try:
                sess.offset_ms = float(item.text())
            except ValueError:
                pass
            self.session_changed.emit(row)
        elif col == COL_TGT_FPS:
            fps = _parse_fps(item.text())
            if fps:
                sess.manual_dst_fps = fps
                sess.use_manual_conform = True
            else:
                sess.use_manual_conform = False
                sess.manual_dst_fps = None
            self.refresh()
            self.session_changed.emit(row)
        elif col == COL_SRC_FPS:
            # forces the *source* rate; feeds the automatic conform
            # suggestion (subtitle fps vs video fps)
            sess.manual_src_fps = _parse_fps(item.text())
            self.refresh()
            self.session_changed.emit(row)
        elif col == COL_LANG:
            lang = item.text().strip()
            if lang:
                sess.doc.language = lang
                for cue in sess.doc.cues:
                    cue.lang = lang
                self.session_changed.emit(row)
        elif col == COL_OUT:
            name = item.text().strip()
            if name:
                sess.out_path = os.path.join(
                    os.path.dirname(sess.out_path or sess.sub_path), name)
                self.session_changed.emit(row)

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        a_render = menu.addAction('Add this file to the queue')
        a_rematch = menu.addAction('Re-match video from folder')
        a_unbind = menu.addAction('Unbind video')
        a_offset_all = menu.addAction('Copy offset to all files')
        a_auto_conform = menu.addAction('Reset target fps to auto')
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        sess = self.state.sessions[row]
        if act == a_render:
            self.render_requested.emit(row)
        elif act == a_rematch:
            sess.auto_match_video()
            self.refresh()
            self.session_changed.emit(row)
        elif act == a_unbind:
            sess.bind_video(None)
            self.refresh()
            self.session_changed.emit(row)
        elif act == a_offset_all:
            for s in self.state.sessions:
                s.offset_ms = sess.offset_ms
            self.refresh()
            self.session_changed.emit(row)
        elif act == a_auto_conform:
            sess.use_manual_conform = False
            sess.manual_dst_fps = None
            sess.manual_src_fps = None
            self.refresh()
            self.session_changed.emit(row)
