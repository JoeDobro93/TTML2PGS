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
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                             QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QListWidget,
                             QListWidgetItem, QMenu, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from ...core.merge import (all_variants, common_variants, merge_documents,
                           merged_out_path, merged_track_name, plan_merge,
                           variant_label)
from ...core.parsers import SUBTITLE_EXTENSIONS
from ...core.timing import fps_label
from ..state import AppState, DocumentSession

COL_NAME, COL_LANG, COL_VIDEO, COL_RES, COL_HDR, COL_SRC_FPS, COL_TGT_FPS, \
    COL_CONFORM, COL_OFFSET, COL_OUT = range(10)


class _MergeDialog(QDialog):
    """Pick the primary + secondary language for a batch merge.
    Options missing from any selected episode are greyed out."""

    def __init__(self, variants: List[str], common: List[str],
                 app_settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Merge subtitles')
        self.app_settings = app_settings
        lay = QVBoxLayout(self)
        hint = QLabel(
            'Pick the PRIMARY language (sets the merged subtitle\'s '
            'language, initials and mux tag) and the SECONDARY one '
            'rendered alongside it. Greyed languages are missing from '
            'at least one selected episode.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: palette(mid); font-size: 11px;')
        lay.addWidget(hint)

        row = QHBoxLayout()
        self.lst_primary = QListWidget()
        self.lst_secondary = QListWidget()
        for title, lst in (('Primary', self.lst_primary),
                           ('Secondary', self.lst_secondary)):
            col = QVBoxLayout()
            col.addWidget(QLabel(title))
            col.addWidget(lst)
            w = QWidget()
            w.setLayout(col)
            row.addWidget(w)
            for v in variants:
                item = QListWidgetItem(variant_label(v))
                item.setData(Qt.ItemDataRole.UserRole, v)
                if v not in common:
                    item.setFlags(item.flags() &
                                  ~Qt.ItemFlag.ItemIsEnabled &
                                  ~Qt.ItemFlag.ItemIsSelectable)
                lst.addItem(item)
        lay.addLayout(row)

        self.chk_close = QCheckBox('Close unused subtitles')
        self.chk_close.setChecked(
            bool(app_settings.get('merge_close_unused', True)))
        lay.addWidget(self.chk_close)

        row_snap = QHBoxLayout()
        self.chk_snap = QCheckBox('Align overlaps after merging — '
                                  'threshold (s):')
        self.chk_snap.setToolTip(
            'Snap the secondary language\'s cue edges to the primary\'s '
            'cue boundaries (and align same-language overlaps by region '
            'position) right after each merge.')
        self.chk_snap.setChecked(
            bool(app_settings.get('merge_snap', False)))
        from PyQt6.QtWidgets import QDoubleSpinBox
        self.spin_snap = QDoubleSpinBox()
        self.spin_snap.setRange(0.05, 10.0)
        self.spin_snap.setSingleStep(0.05)
        self.spin_snap.setDecimals(2)
        self.spin_snap.setValue(
            float(app_settings.get('merge_snap_threshold', 0.5)))
        row_snap.addWidget(self.chk_snap)
        row_snap.addWidget(self.spin_snap)
        row_snap.addStretch()
        lay.addLayout(row_snap)

        self.btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        self.btns.accepted.connect(self.accept)
        self.btns.rejected.connect(self.reject)
        lay.addWidget(self.btns)

        self.lst_primary.itemSelectionChanged.connect(self._validate)
        self.lst_secondary.itemSelectionChanged.connect(self._validate)
        self._validate()

    def _sel(self, lst) -> str:
        items = lst.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else ''

    def _validate(self):
        p, s = self._sel(self.lst_primary), self._sel(self.lst_secondary)
        self.btns.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(p) and bool(s) and p != s)

    def accept(self):
        self.app_settings['merge_close_unused'] = \
            self.chk_close.isChecked()
        self.app_settings['merge_snap'] = self.chk_snap.isChecked()
        self.app_settings['merge_snap_threshold'] = \
            self.spin_snap.value()
        super().accept()

    def choice(self):
        return (self._sel(self.lst_primary),
                self._sel(self.lst_secondary),
                self.chk_close.isChecked())

    def snap_choice(self):
        """(align_after_merge, threshold_seconds)"""
        return self.chk_snap.isChecked(), self.spin_snap.value()


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
    overrides_loaded = pyqtSignal(object)      # OverrideSet from a .t2p

    HEADERS = ['Subtitle', 'Lang', 'Video', 'Res', 'HDR', 'Src fps',
               'Tgt fps', 'Conform', 'Offset ms', 'Output']

    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        #: called before any dialog opens (main window points this at
        #: PreviewPane.close_popout so the pop-out can't block dialogs)
        self.before_popup = lambda: None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        b_add = QPushButton('Add subtitles…')
        b_folder = QPushButton('Add folder…')
        b_close = QPushButton('Close selected')
        b_close_all = QPushButton('Close all…')
        b_merge = QPushButton('Merge selected…')
        b_merge.setToolTip(
            'Merge two languages per episode into ONE subtitle (e.g. '
            'Japanese dialogue + English forced signs). Highlight any '
            'file of each episode you want merged — every open file of '
            'those episodes is considered, and you pick the primary + '
            'secondary language once for the whole batch.')
        bar.addWidget(b_add)
        bar.addWidget(b_folder)
        bar.addWidget(b_close)
        bar.addWidget(b_close_all)
        bar.addWidget(b_merge)
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
        # multi-select for bulk close / bulk add-to-queue; the ACTIVE
        # session still follows the current row alone
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        # every column individually resizable; Output soaks up the rest
        for c in range(len(self.HEADERS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for c, w in ((COL_NAME, 240), (COL_LANG, 46), (COL_VIDEO, 180),
                     (COL_RES, 78), (COL_HDR, 40),
                     (COL_SRC_FPS, 64), (COL_TGT_FPS, 64),
                     (COL_CONFORM, 120), (COL_OFFSET, 70)):
            self.table.setColumnWidth(c, w)
        # file-name cells: character-level elision that keeps the
        # extension chain visible (word wrap would hide at spaces), and
        # a ~20-character floor on how narrow the columns can get
        from .elide import (FileElideDelegate, enforce_min_section_width,
                            min_chars_width)
        self.table.setWordWrap(False)
        name_cols = (COL_NAME, COL_VIDEO, COL_OUT)
        for c in name_cols:
            self.table.setItemDelegateForColumn(
                c, FileElideDelegate(self.table))
        min_w = min_chars_width(self.table)
        for c in name_cols:
            if self.table.columnWidth(c) < min_w:
                self.table.setColumnWidth(c, min_w)
        enforce_min_section_width(hh, name_cols, min_w)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.table)

        b_add.clicked.connect(self._add_files)
        b_folder.clicked.connect(self._add_folder)
        b_close.clicked.connect(self._close_selected)
        b_close_all.clicked.connect(self._close_all)
        b_merge.clicked.connect(self._merge_selected)
        self.b_render.clicked.connect(self._render_selected)
        self.b_render_all.clicked.connect(self.render_all_requested.emit)
        self.table.currentCellChanged.connect(self._row_changed)
        self.table.cellDoubleClicked.connect(self._cell_double)
        self.table.itemChanged.connect(self._item_edited)
        self.table.customContextMenuRequested.connect(self._context_menu)
        from PyQt6.QtGui import QKeySequence, QShortcut
        sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self._close_selected)
        self._loading = False

    # ------------------------------------------------------------------ #
    def _selected_rows(self) -> List[int]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return [r for r in rows if 0 <= r < len(self.state.sessions)]

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
        self.before_popup()
        exts = ' '.join(f'*{e}' for e in SUBTITLE_EXTENSIONS) + ' *.t2p'
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Open subtitles', '', f'Subtitles ({exts})')
        batch = {'all': None}
        for p in paths:
            self._open(p, batch)

    def _add_folder(self):
        self.before_popup()
        folder = QFileDialog.getExistingDirectory(self, 'Add folder')
        if not folder:
            return
        batch = {'all': None}
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(SUBTITLE_EXTENSIONS):
                self._open(os.path.join(folder, fn), batch)

    def _open(self, path: str, batch: Optional[dict] = None):
        from PyQt6.QtWidgets import QCheckBox, QMessageBox
        self.before_popup()  # reopen/overrides prompts may appear
        # same FILE NAME already open (any folder)? confirm a reload
        # instead of loading it twice
        idx = self.state.find_session_by_name(path)
        if idx >= 0:
            choice = batch.get('all') if batch else None
            if choice is None:
                box = QMessageBox(self)
                box.setWindowTitle('Subtitle already open')
                box.setText(f"'{os.path.basename(path)}' is already "
                            f"open.\nReload it? (The open copy — "
                            f"including any edits — is replaced.)")
                box.setStandardButtons(QMessageBox.StandardButton.Ok |
                                       QMessageBox.StandardButton.Cancel)
                box.button(QMessageBox.StandardButton.Ok).setText('Reload')
                box.button(QMessageBox.StandardButton.Cancel).setText(
                    "Don't reopen")
                chk = QCheckBox('Do this for all')
                if batch is not None:
                    box.setCheckBox(chk)
                r = box.exec()
                choice = (r == QMessageBox.StandardButton.Ok)
                if batch is not None and chk.isChecked():
                    batch['all'] = choice
            if choice:
                try:
                    self.state.reload_session(idx, path)
                except Exception as e:
                    QMessageBox.warning(self, 'Open failed',
                                        f'{path}\n\n{e}')
                    return
                self.refresh()
                self.session_activated.emit(self.state.active_index)
            return
        try:
            self.state.open_subtitle(path)
        except Exception as e:
            QMessageBox.warning(self, 'Open failed', f'{path}\n\n{e}')
            return
        self._maybe_adopt_project_overrides(path)
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    def _maybe_adopt_project_overrides(self, path: str):
        """A .t2p can carry the Global Overrides it was saved with —
        offer to apply them."""
        from PyQt6.QtWidgets import QMessageBox
        if not path.lower().endswith('.t2p'):
            return
        try:
            from ...core.project import load_project
            _doc, ov, _extras = load_project(path)
        except Exception:
            return
        if ov is None:
            return
        r = QMessageBox.question(
            self, 'Project overrides',
            'This project file contains saved Global Overrides '
            '(Default and per-language settings).\n\n'
            'Overwrite the current Global Overrides with the ones in '
            'this file?',
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.overrides_loaded.emit(ov)

    def _close_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        self.before_popup()
        if len(rows) > 1:
            from PyQt6.QtWidgets import QMessageBox
            if QMessageBox.question(
                    self, 'Close subtitles',
                    f'Close {len(rows)} subtitle(s)?') != \
                    QMessageBox.StandardButton.Yes:
                return
        for row in reversed(rows):
            self.state.close_session(row)
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    def _close_all(self):
        from PyQt6.QtWidgets import QMessageBox
        n = len(self.state.sessions)
        if not n:
            return
        self.before_popup()
        if QMessageBox.question(
                self, 'Close all',
                f'Close all {n} subtitle(s)?') != \
                QMessageBox.StandardButton.Yes:
            return
        for i in reversed(range(n)):
            self.state.close_session(i)
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    def _render_selected(self):
        for row in self._selected_rows():
            self.render_requested.emit(row)

    # ------------------------------------------------------------------ #
    # Merge mode
    # ------------------------------------------------------------------ #
    def _merge_selected(self):
        from PyQt6.QtWidgets import QMessageBox
        self.before_popup()
        rows = self._selected_rows()
        sel = [self.state.sessions[r] for r in rows
               if not self.state.sessions[r].merged_from]
        if not sel:
            QMessageBox.information(
                self, 'Merge', 'Highlight at least one subtitle of each '
                'episode you want merged.')
            return
        pool = [(s.sub_path, s.doc.language) for s in self.state.sessions
                if not s.merged_from]
        groups = plan_merge(pool, [s.sub_path for s in sel])
        common = common_variants(groups)
        if len(common) < 2:
            per = '\n'.join(
                f"• {stem}: " + ', '.join(variant_label(v) for v in vs)
                for stem, vs in groups.items())
            QMessageBox.warning(
                self, 'Merge',
                'The languages don\'t all work out — fewer than two '
                'language options are present in EVERY selected '
                'episode:\n\n' + per)
            return
        dlg = _MergeDialog(all_variants(groups), common,
                           self.state.settings, self)
        if not dlg.exec():
            return
        prim, sec, close_unused = dlg.choice()
        do_snap, snap_s = dlg.snap_choice()

        to_close: List[str] = []
        first_merged: Optional[DocumentSession] = None
        for stem, variants in groups.items():
            p_path, s_path = variants[prim], variants[sec]
            p_sess = next(s for s in self.state.sessions
                          if s.sub_path == p_path)
            s_sess = next(s for s in self.state.sessions
                          if s.sub_path == s_path)
            # honor per-source timing (offset / fps conform): when the
            # two sides differ, bake each side's transform into its
            # cues so the merged job needs only one timing
            p_doc, s_doc = p_sess.doc, s_sess.doc
            p_plan, s_plan = p_sess.retime_plan(), s_sess.retime_plan()
            if (p_sess.offset_ms != s_sess.offset_ms or
                    p_plan != s_plan):
                import copy as _copy
                from ...core.merge import bake_timing
                p_doc = _copy.deepcopy(p_doc)
                s_doc = _copy.deepcopy(s_doc)
                bake_timing(p_doc, p_plan, p_sess.offset_ms)
                bake_timing(s_doc, s_plan, s_sess.offset_ms)
                p_sess.offset_ms = 0.0
                p_sess.use_manual_conform = True   # baked: no re-conform
                p_sess.manual_src_fps = None
                p_sess.manual_dst_fps = None
            p_sess.doc = merge_documents(p_doc, s_doc, prim, sec)
            if do_snap:
                from ...core.merge import align_overlaps
                align_overlaps(p_sess.doc, p_sess.doc.language,
                               snap_s * 1000.0)
            p_sess.merged_from = [os.path.basename(p_path),
                                  os.path.basename(s_path)]
            p_sess.out_path = merged_out_path(
                p_path, p_sess.video_path, prim, sec)
            p_sess.track_name = merged_track_name(prim, sec)
            p_sess.dirty = True
            to_close.append(s_path)
            if close_unused:
                to_close += [p for v, p in variants.items()
                             if v not in (prim, sec)]
            if first_merged is None:
                first_merged = p_sess
        for path in to_close:
            i = next((i for i, s in enumerate(self.state.sessions)
                      if s.sub_path == path and not s.merged_from), -1)
            if i >= 0:
                self.state.close_session(i)
        if first_merged is not None:
            self.state.active_index = self.state.sessions.index(
                first_merged)
        self.refresh()
        self.session_activated.emit(self.state.active_index)

    # ------------------------------------------------------------------ #
    def _row_changed(self, row, col, prow, pcol):
        if self._loading or row < 0 or row == self.state.active_index:
            return
        self.state.active_index = row
        self.session_activated.emit(row)

    def _cell_double(self, row, col):
        if col == COL_VIDEO and 0 <= row < len(self.state.sessions):
            self.before_popup()
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
                if not sess.merged_from:
                    # merged docs keep per-cue source languages — only
                    # the document (mux/profile) language changes
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
        rows = self._selected_rows()
        if row not in rows:
            self.table.selectRow(row)
            rows = [row]
        many = len(rows) > 1
        menu = QMenu(self)
        a_render = menu.addAction(
            f'Add {len(rows)} selected to the queue' if many
            else 'Add this file to the queue')
        a_close = menu.addAction(
            f'Close {len(rows)} selected\tDel' if many
            else 'Close this file\tDel')
        menu.addSeparator()
        a_rematch = menu.addAction('Re-match video from folder')
        a_unbind = menu.addAction('Unbind video')
        a_offset_all = menu.addAction('Copy offset to all files')
        a_auto_conform = menu.addAction('Reset target fps to auto')
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        sess = self.state.sessions[row]
        if act == a_render:
            for r in rows:
                self.render_requested.emit(r)
        elif act == a_close:
            self._close_selected()
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
