"""
Queue pane: video-grouped tree of render jobs + mux status.

Flow: files are **added** to the queue from the Sources pane, then
**started** here — "Render all", "Render selected", or per-item via the
context menu. Pause/Resume only affect started work; jobs sitting in
"added" never run until you start them. A group's mux fires when every
job in it is finished (unstarted jobs hold it, visibly).

The tree refreshes **in place** (items are diffed by id, never
rebuilt), so multi-selection, the shift-click anchor, scroll position
and expansion all survive the constant progress updates a running
render produces. Del removes the selection; the context menu adapts to
it (bulk actions on multi-select, per-item actions otherwise, queue
options on empty space).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QKeySequence, \
    QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                             QLabel, QMenu, QPushButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from ...core.jobqueue import JobState, QueueManager, RenderJob, VideoGroup

_STATE_COLORS = {
    JobState.PENDING: '#a0a0a0',
    JobState.RUNNING: '#6aa7ff',
    JobState.PAUSED: '#e0b040',
    JobState.DONE: '#69d26a',
    JobState.FAILED: '#ff6a6a',
    JobState.CANCELED: '#808080',
    JobState.WAITING: '#9a9a9a',
}

_ADDED_COLOR = '#c8b06a'      # added-but-not-started


def _job_state_label(j: RenderJob) -> str:
    if j.state == JobState.PENDING:
        return 'queued' if j.started else 'added'
    if j.state == JobState.DONE and not j.started:
        # a loaded .sup: rendered, but like all added work it waits
        # for a start before its video muxes
        return 'added · done'
    return j.state.value


def _group_summary(g: VideoGroup) -> Tuple[str, str, str, str]:
    """(state_text, state_color, progress_text, info_text) for a group
    row — meaningful even (especially) when the group is collapsed."""
    jobs = g.render_jobs
    n = len(jobs)
    n_done = sum(1 for j in jobs if j.state == JobState.DONE)
    n_fail = sum(1 for j in jobs if j.state == JobState.FAILED)
    n_pause = sum(1 for j in jobs if j.state == JobState.PAUSED)
    # unstarted work of ANY state (including loaded already-done .sups)
    # counts as 'added' — it holds the group's mux until started
    n_added = g.unstarted_count()
    n_queued = sum(1 for j in jobs
                   if j.started and j.state == JobState.PENDING)
    running = any(j.state == JobState.RUNNING for j in jobs)
    renders_settled = all(j.state.is_terminal() for j in jobs)

    wants_mux = bool(g.video_path) and g.mux_enabled and \
        (jobs or g.external_sups)
    fully_done = (n_done == n and n > 0 or (not jobs and g.external_sups)) \
        and (not wants_mux or g.mux_state == JobState.DONE)

    # phase (one word) + color
    if running:
        phase, color = 'rendering', _STATE_COLORS[JobState.RUNNING]
    elif g.mux_state == JobState.RUNNING:
        phase, color = 'muxing', _STATE_COLORS[JobState.RUNNING]
    elif n_fail or g.mux_state == JobState.FAILED:
        phase, color = 'failed', _STATE_COLORS[JobState.FAILED]
    elif n_pause:
        phase, color = 'paused', _STATE_COLORS[JobState.PAUSED]
    elif fully_done:
        phase, color = 'done', _STATE_COLORS[JobState.DONE]
    elif n_added:
        phase, color = f'{n_added} added', _ADDED_COLOR
    elif n_queued:
        phase, color = 'queued', _STATE_COLORS[JobState.PENDING]
    elif renders_settled and wants_mux and \
            g.mux_state == JobState.WAITING:
        phase, color = 'mux next', _STATE_COLORS[JobState.WAITING]
    else:
        phase, color = '', _STATE_COLORS[JobState.WAITING]

    state_txt = f"{n_done}/{n}" if n else 'external'
    if phase:
        state_txt += f' · {phase}'

    # progress: render average until renders settle, then mux progress
    if n and not renders_settled:
        avg = sum(1.0 if j.state == JobState.DONE else j.progress
                  for j in jobs) / n
        prog = f"{avg * 100:.0f}%"
    elif g.mux_state == JobState.RUNNING:
        prog = f"mux {g.mux_progress * 100:.0f}%"
    elif fully_done:
        prog = '100%'
    elif n and renders_settled and n_done < n:
        prog = f"{n_done}/{n} ok"
    else:
        prog = ''

    # info: mux status + delivery hint
    info = ''
    if g.video_path:
        n_wait = g.unstarted_count()
        info = {JobState.WAITING: 'mux: waiting for renders',
                JobState.RUNNING: 'mux: running',
                JobState.DONE: 'mux: done',
                JobState.FAILED: f'mux FAILED: {g.mux_error}',
                }.get(g.mux_state, '')
        if g.mux_state == JobState.WAITING and n_wait:
            info = f'mux: waiting — {n_wait} job(s) not started yet'
        elif g.mux_state == JobState.WAITING and renders_settled and jobs:
            info = 'mux: next up'
        if not g.mux_enabled:
            info = 'mux: disabled'
        elif not g.replace_original:
            info += '  → *.muxed.mkv'
        if not info:
            info = g.mux_message
    return state_txt, color, prog, info


class QueuePane(QWidget):
    #: queue-wide options changed here (persist + mirror in Settings)
    settings_changed = pyqtSignal()

    def __init__(self, queue: QueueManager,
                 app_settings: Optional[dict] = None):
        super().__init__()
        self.queue = queue
        self.app_settings = app_settings
        #: called before any dialog opens (main window points this at
        #: PreviewPane.close_popout so the pop-out can't block dialogs)
        self.before_popup = lambda: None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self._updating = False           # guard: programmatic item edits

        # two tidy rows: RUN CONTROL on top, queue management below
        self.b_start_all = QPushButton('▶ Start all')
        self.b_start_all.setToolTip(
            'Start every CHECKED item whose video is checked too '
            '(MakeMKV-style): renders anything not yet rendered, and '
            'arms the videos\' muxes — loaded .sups just arm. '
            'Unchecked rows sit out.')
        self.b_start_sel = QPushButton('▶ Start selected')
        self.b_start_sel.setToolTip(
            'Start only the highlighted subtitles / video groups '
            '(render if needed, then mux).')
        self.b_pause = QPushButton('⏸ Pause')
        self.b_pause.setToolTip(
            'Pause: the running render checkpoints between cues; '
            'started jobs stay queued. Added-only work is untouched.')
        self.b_resume = QPushButton('⏵ Resume')
        self.b_resume.setToolTip(
            'Continue paused/started work. Added-only work keeps '
            'waiting for a Start.')
        self.b_add_sups = QPushButton('Queue .sup files…')
        self.b_add_sups.setToolTip(
            'Load already-rendered .sup files (several at once). Each '
            'is matched to a video by file name and appears as a '
            'finished render — start it like anything else to mux. '
            'Language, forced flag and track label come from the '
            'extension chain (.ja, .en.forced, .ja+en.forced…).')
        self.b_check_sel = QPushButton('☑ Sel.')
        self.b_check_sel.setToolTip(
            'Tick the checkbox of every highlighted row (videos and '
            'subtitles alike).')
        self.b_uncheck_sel = QPushButton('☐ Sel.')
        self.b_uncheck_sel.setToolTip(
            'Untick the checkbox of every highlighted row — they sit '
            'out of "Start all".')
        from PyQt6.QtWidgets import QToolButton
        self.b_clear = QToolButton()
        self.b_clear.setText('Clear ')
        self.b_clear.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        m_clear = QMenu(self.b_clear)
        m_clear.addAction('Clear finished', self._clear_finished)
        m_clear.addAction('Clear all…', self._clear_all)
        self.b_clear.setMenu(m_clear)
        row1 = QHBoxLayout()
        row1.addWidget(self.b_start_all)
        row1.addWidget(self.b_start_sel)
        row1.addWidget(self.b_pause)
        row1.addWidget(self.b_resume)
        row1.addStretch()
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(self.b_add_sups)
        row2.addWidget(self.b_check_sel)
        row2.addWidget(self.b_uncheck_sel)
        row2.addWidget(self.b_clear)
        row2.addStretch()
        self.lbl_status = QLabel('')
        row2.addWidget(self.lbl_status)
        lay.addLayout(row2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(['Item', 'State', 'Progress', 'Info'])
        hh = self.tree.header()
        # every column individually resizable; Info soaks up the rest
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 110)
        self.tree.setColumnWidth(2, 80)
        # file names elide keeping the extension chain; ~20-char floor
        from .elide import (FileElideDelegate, enforce_min_section_width,
                            min_chars_width)
        self.tree.setItemDelegateForColumn(0, FileElideDelegate(self.tree))
        enforce_min_section_width(hh, (0,), min_chars_width(self.tree))
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.tree)

        self.b_add_sups.clicked.connect(self._queue_sups)
        self.b_start_all.clicked.connect(self._start_all)
        self.b_start_sel.clicked.connect(self._start_selected)
        self.b_check_sel.clicked.connect(lambda: self._check_selected(True))
        self.b_uncheck_sel.clicked.connect(
            lambda: self._check_selected(False))
        self.b_pause.clicked.connect(self.queue.pause_all)
        self.b_resume.clicked.connect(self.queue.resume_all)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemChanged.connect(self._item_check_changed)
        self.tree.itemSelectionChanged.connect(self._constrain_selection)

        # Del removes whatever is selected (running jobs are skipped by
        # the engine; deleting a group cancels + removes it)
        sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.tree)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self._remove_selected)

    # ------------------------------------------------------------------ #
    # Checkboxes (MakeMKV-style arming) + selection rules
    # ------------------------------------------------------------------ #
    def _item_check_changed(self, item: QTreeWidgetItem, col: int):
        if self._updating or col != 0:
            return
        kind = item.data(1, Qt.ItemDataRole.UserRole)
        ident = item.data(0, Qt.ItemDataRole.UserRole)
        on = item.checkState(0) == Qt.CheckState.Checked
        if kind == 'group':
            self.queue.set_group_checked(ident, on)
        elif kind == 'job':
            self.queue.set_job_checked(ident, on)
        self.refresh()

    def _constrain_selection(self):
        """MakeMKV-style selection: one KIND at a time. Shift-selecting
        group rows never grabs their children; shift-selecting children
        stays inside the clicked video's group. The prune is deferred a
        tick — deselecting from inside the selectionChanged signal
        leaves Qt's per-item isSelected() cache stale."""
        if self._updating:
            return
        cur = self.tree.currentItem()
        if cur is None:
            return
        cur_kind = cur.data(1, Qt.ItemDataRole.UserRole)
        cur_parent = cur.parent()
        bad = []
        for it in self.tree.selectedItems():
            if it is cur:
                continue
            kind = it.data(1, Qt.ItemDataRole.UserRole)
            if (kind == 'group') != (cur_kind == 'group'):
                bad.append(it)
            elif cur_kind != 'group' and it.parent() is not cur_parent:
                bad.append(it)
        if not bad:
            return

        def prune():
            self._updating = True
            try:
                for it in bad:
                    try:
                        it.setSelected(False)
                    except RuntimeError:
                        pass             # item removed meanwhile
            finally:
                self._updating = False
        QTimer.singleShot(0, prune)

    # ------------------------------------------------------------------ #
    # Selection helpers
    # ------------------------------------------------------------------ #
    def _selection(self) -> List[tuple]:
        out = []
        for item in self.tree.selectedItems():
            kind = item.data(1, Qt.ItemDataRole.UserRole)
            ident = item.data(0, Qt.ItemDataRole.UserRole)
            if kind and ident is not None:
                out.append((kind, ident))
        return out

    def _selected_job_ids(self) -> List[int]:
        """Job ids covered by the selection — a selected group counts as
        all of its jobs (without double-counting explicit ones)."""
        ids: List[int] = []
        groups = {g.id: g for g in self.queue.snapshot()}
        for kind, ident in self._selection():
            if kind == 'job' and ident not in ids:
                ids.append(ident)
            elif kind == 'group' and ident in groups:
                for j in groups[ident].render_jobs:
                    if j.id not in ids:
                        ids.append(j.id)
        return ids

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def _start_all(self):
        self.queue.start_all()
        self.refresh()

    def _start_selected(self):
        for kind, ident in self._selection():
            if kind == 'job':
                self.queue.start_job(ident)
            elif kind == 'group':
                self.queue.start_group(ident)
        self.refresh()

    def _check_selected(self, on: bool):
        """Tick/untick the arming checkbox of every highlighted row."""
        for kind, ident in self._selection():
            if kind == 'group':
                self.queue.set_group_checked(ident, on)
            elif kind == 'job':
                self.queue.set_job_checked(ident, on)
        self.refresh()

    def _pause_selected(self):
        for jid in self._selected_job_ids():
            self.queue.pause_job(jid)
        self.refresh()

    def _cancel_selected(self):
        for jid in self._selected_job_ids():
            self.queue.cancel_job(jid)
        self.refresh()

    def _retry_selected(self):
        groups = {g.id: g for g in self.queue.snapshot()}
        for jid in self._selected_job_ids():
            self.queue.retry_job(jid)
        for kind, ident in self._selection():
            if kind == 'group' and ident in groups and \
                    groups[ident].mux_state in (JobState.FAILED,
                                                JobState.CANCELED):
                self.queue.retry_mux(ident)
        self.refresh()

    def _remove_selected(self):
        sel = self._selection()
        for kind, ident in sel:
            if kind == 'group':
                self.queue.remove_group(ident)
            else:                          # jobs and external sups
                self.queue.remove_job(ident)
        self.refresh()

    def _clear_all(self):
        from PyQt6.QtWidgets import QMessageBox
        groups = self.queue.snapshot()
        if not groups:
            return
        self.before_popup()
        if QMessageBox.question(
                self, 'Clear queue',
                f'Remove all {len(groups)} video group(s) from the '
                f'queue? Running jobs are canceled.') != \
                QMessageBox.StandardButton.Yes:
            return
        for g in groups:
            self.queue.remove_group(g.id)
        self.refresh()

    def _queue_sups(self):
        """Pick several already-rendered .sup files and queue them for
        muxing, matched to videos by file name."""
        self.before_popup()
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Queue .sup files for muxing', '',
            'PGS subtitles (*.sup)')
        if not paths:
            return
        unmatched = self.queue_sup_files(paths)
        self.refresh()
        if unmatched:
            shown = '\n'.join('• ' + os.path.basename(p)
                              for p in unmatched[:12])
            if len(unmatched) > 12:
                shown += f'\n… and {len(unmatched) - 12} more'
            QMessageBox.warning(
                self, 'Queue .sup files',
                'No matching video found next to these files (they '
                'were not queued):\n\n' + shown)

    def queue_sup_files(self, paths) -> List[str]:
        """Match each .sup to a video next to it (same stem rules as
        subtitles — language/flag/'ja+en' tokens ignored) and queue it
        as a DONE render job, exactly as if its render had just
        finished: it waits like any added work until you start it,
        then the group muxes. Language, track label and forced flag
        come from the extension chain. Returns unmatched paths."""
        from ...core.video import find_matching_video, parse_sup_name
        unmatched: List[str] = []
        for p in paths:
            video = find_matching_video(p)
            if not video:
                unmatched.append(p)
                continue
            lang, track, _forced = parse_sup_name(p)
            self.queue.add_finished_sup(video, p, lang=lang,
                                        track_name=track)
        return unmatched

    def _clear_finished(self):
        for g in self.queue.snapshot():
            all_done = all(j.state.is_terminal() for j in g.render_jobs) and \
                g.mux_state in (JobState.DONE, JobState.FAILED,
                                JobState.CANCELED) or \
                (not g.video_path and
                 all(j.state.is_terminal() for j in g.render_jobs))
            if g.render_jobs and all_done:
                self.queue.remove_group(g.id)
        self.refresh()

    def _toggle_move_subs(self, on: bool):
        self.queue.move_to_subs = bool(on)
        if self.app_settings is not None:
            self.app_settings['move_to_subs_folder'] = bool(on)
        self.settings_changed.emit()

    def _edit_track_name(self, job_id: int):
        """Edit the mux track metadata name (e.g. 'ja+en' for merges)."""
        from PyQt6.QtWidgets import QInputDialog
        job = self.queue.find_job(job_id)
        if job is None:
            return
        self.before_popup()
        name, ok = QInputDialog.getText(
            self, 'Mux track name',
            'Track name written into the MKV for this subtitle\n'
            '(empty = no name):', text=job.track_name)
        if ok:
            self.queue.set_track_name(job_id, name)
            self.refresh()

    @staticmethod
    def _open_folder(path: Optional[str]):
        if path:
            d = os.path.dirname(os.path.abspath(path))
            if os.path.isdir(d):
                QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ------------------------------------------------------------------ #
    # In-place refresh (selection/anchor/scroll survive)
    # ------------------------------------------------------------------ #
    def refresh(self):
        if self._updating:
            return
        self._updating = True
        try:
            self._refresh_impl()
        finally:
            self._updating = False

    def _refresh_impl(self):
        groups = self.queue.snapshot()

        # -- top level: prune, insert, reorder --------------------------- #
        want = {g.id for g in groups}
        by_id: Dict[int, QTreeWidgetItem] = {}
        for idx in reversed(range(self.tree.topLevelItemCount())):
            it = self.tree.topLevelItem(idx)
            gid = it.data(0, Qt.ItemDataRole.UserRole)
            if gid in want:
                by_id[gid] = it
            else:
                self.tree.takeTopLevelItem(idx)
        for pos, g in enumerate(groups):
            it = by_id.get(g.id)
            if it is None:
                it = QTreeWidgetItem(['', '', '', ''])
                it.setData(0, Qt.ItemDataRole.UserRole, g.id)
                it.setData(1, Qt.ItemDataRole.UserRole, 'group')
                self.tree.insertTopLevelItem(pos, it)
                it.setExpanded(True)
                by_id[g.id] = it
            elif self.tree.indexOfTopLevelItem(it) != pos:
                expanded = it.isExpanded()
                self.tree.takeTopLevelItem(
                    self.tree.indexOfTopLevelItem(it))
                self.tree.insertTopLevelItem(pos, it)
                it.setExpanded(expanded)
            self._update_group_item(it, g)
            self._sync_children(it, g)

        # -- status bar + buttons ---------------------------------------- #
        total = done = added = active = 0
        for g in groups:
            for j in g.render_jobs:
                total += 1
                if j.state == JobState.DONE:
                    done += 1
                # unstarted work of any state (loaded .sups included)
                if not j.started and j.state not in (JobState.FAILED,
                                                     JobState.CANCELED):
                    added += 1
                if j.started and not j.state.is_terminal():
                    active += 1
        paused = self.queue.is_paused()
        state = 'PAUSED' if paused else \
            ('idle' if self.queue.is_idle() else 'working')
        bits = [f"{done}/{total} rendered"]
        if added:
            bits.append(f"{added} added (not started)")
        self.lbl_status.setText(' · '.join(bits) + f' · {state}')
        self.b_pause.setEnabled(not paused and active > 0)
        self.b_resume.setEnabled(paused)
        self.b_start_all.setEnabled(added > 0 or paused)

    def _update_group_item(self, it: QTreeWidgetItem, g: VideoGroup):
        state_txt, color, prog, info = _group_summary(g)
        for col, txt in ((0, g.label()), (1, state_txt), (2, prog),
                         (3, info)):
            if it.text(col) != txt:
                it.setText(col, txt)
        it.setForeground(1, QBrush(QColor(color)))
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        want = Qt.CheckState.Checked if g.checked \
            else Qt.CheckState.Unchecked
        if it.checkState(0) != want:
            it.setCheckState(0, want)

    def _sync_children(self, gi: QTreeWidgetItem, g: VideoGroup):
        want_keys = [('job', j.id) for j in g.render_jobs] + \
                    [('external', e.id) for e in g.external_sups]
        by_key: Dict[tuple, QTreeWidgetItem] = {}
        for idx in reversed(range(gi.childCount())):
            ch = gi.child(idx)
            key = (ch.data(1, Qt.ItemDataRole.UserRole),
                   ch.data(0, Qt.ItemDataRole.UserRole))
            if key in want_keys:
                by_key[key] = ch
            else:
                gi.takeChild(idx)
        rows = [(('job', j.id), j) for j in g.render_jobs] + \
               [(('external', e.id), e) for e in g.external_sups]
        for pos, (key, obj) in enumerate(rows):
            ch = by_key.get(key)
            if ch is None:
                ch = QTreeWidgetItem(['', '', '', ''])
                ch.setData(0, Qt.ItemDataRole.UserRole, key[1])
                ch.setData(1, Qt.ItemDataRole.UserRole, key[0])
                gi.insertChild(pos, ch)
                by_key[key] = ch
            elif gi.indexOfChild(ch) != pos:
                gi.takeChild(gi.indexOfChild(ch))
                gi.insertChild(pos, ch)
            if key[0] == 'job':
                self._update_job_item(ch, obj)
            else:
                self._update_external_item(ch, obj)

    def _update_job_item(self, it: QTreeWidgetItem, j: RenderJob):
        label = _job_state_label(j)
        info = j.error or j.message
        if j.track_name:
            info = f'{info}  [track: {j.track_name}]'.strip()
        for col, txt in ((0, j.label()), (1, label),
                         (2, f"{j.progress * 100:.0f}%"),
                         (3, info)):
            if it.text(col) != txt:
                it.setText(col, txt)
        color = _ADDED_COLOR if label.startswith('added') \
            else _STATE_COLORS.get(j.state, '#ccc')
        it.setForeground(1, QBrush(QColor(color)))
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        want = Qt.CheckState.Checked if j.checked \
            else Qt.CheckState.Unchecked
        if it.checkState(0) != want:
            it.setCheckState(0, want)

    def _update_external_item(self, it: QTreeWidgetItem, e):
        info = f'lang={e.lang}'
        if e.track_name:
            info += f' · "{e.track_name}"'
        for col, txt in ((0, os.path.basename(e.sup_path)),
                         (1, 'external'), (2, ''), (3, info)):
            if it.text(col) != txt:
                it.setText(col, txt)

    # ------------------------------------------------------------------ #
    # Context menu (selection-aware)
    # ------------------------------------------------------------------ #
    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is not None:
            key = (item.data(1, Qt.ItemDataRole.UserRole),
                   item.data(0, Qt.ItemDataRole.UserRole))
            if key not in self._selection():
                # right-click outside the selection targets that item
                self.tree.setCurrentItem(item)
        sel = self._selection()
        menu = QMenu(self)
        if not sel:
            self._queue_menu(menu)
        elif len(sel) > 1:
            self._bulk_menu(menu, sel)
        else:
            kind, ident = sel[0]
            if kind == 'job':
                self._job_menu(menu, ident)
            elif kind == 'group':
                self._group_menu(menu, ident)
            elif kind == 'external':
                self._ext_menu(menu, ident)
            else:
                a_remove = menu.addAction('Remove from queue\tDel')
                a_remove.triggered.connect(self._remove_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        self.refresh()

    def _queue_menu(self, menu: QMenu):
        menu.addAction('▶ Start all', self.queue.start_all)
        menu.addSeparator()
        menu.addAction('Expand all groups', self.tree.expandAll)
        menu.addAction('Collapse all groups', self.tree.collapseAll)
        menu.addSeparator()
        a_move = menu.addAction("Move sources into 'subs' subfolder "
                                'after mux')
        a_move.setCheckable(True)
        a_move.setChecked(self.queue.move_to_subs)
        a_move.toggled.connect(self._toggle_move_subs)
        menu.addAction('Clear finished', self._clear_finished)

    def _bulk_menu(self, menu: QMenu, sel: List[tuple]):
        n = len(sel)
        n_groups = sum(1 for k, _ in sel if k == 'group')
        label = f'{n} items' if n_groups else f'{n} jobs'
        menu.addAction(f'▶ Start selected ({label})',
                       self._start_selected)
        menu.addAction(f'☑ Check selected ({label})',
                       lambda: self._check_selected(True))
        menu.addAction(f'☐ Uncheck selected ({label})',
                       lambda: self._check_selected(False))
        menu.addAction('Pause selected', self._pause_selected)
        menu.addAction('Resume/Retry selected', self._retry_resume_selected)
        menu.addAction('Cancel selected', self._cancel_selected)
        gids = self._selected_group_ids()
        if gids:
            menu.addSeparator()
            gl = f'{len(gids)} video{"s" if len(gids) != 1 else ""}'
            m_mux = menu.addMenu(f'Mux settings ({gl})')
            m_mux.addAction('Mux into video: ON',
                            lambda: self._set_mux_selected(True))
            m_mux.addAction('Mux into video: OFF',
                            lambda: self._set_mux_selected(False))
            m_mux.addSeparator()
            m_mux.addAction('Replace original video: ON',
                            lambda: self._set_replace_selected(True))
            m_mux.addAction('Replace original video: OFF '
                            '(write *.muxed.mkv)',
                            lambda: self._set_replace_selected(False))
        menu.addSeparator()
        menu.addAction(f'Remove selected ({label})\tDel',
                       self._remove_selected)

    def _selected_group_ids(self) -> List[int]:
        """Groups touched by the selection: selected group rows plus the
        parent groups of any selected jobs."""
        sel = self._selection()
        gids = {ident for kind, ident in sel if kind == 'group'}
        jids = {ident for kind, ident in sel if kind == 'job'}
        if jids:
            for g in self.queue.snapshot():
                if any(j.id in jids for j in g.render_jobs):
                    gids.add(g.id)
        return sorted(gids)

    def _set_mux_selected(self, on: bool):
        for gid in self._selected_group_ids():
            self.queue.set_group_mux(gid, on)
        self.refresh()

    def _set_replace_selected(self, on: bool):
        for gid in self._selected_group_ids():
            self.queue.set_group_replace(gid, on)
        self.refresh()

    def _retry_resume_selected(self):
        for jid in self._selected_job_ids():
            j = self.queue.find_job(jid)
            if j is None:
                continue
            if j.state == JobState.PAUSED:
                self.queue.resume_job(jid)
            elif j.state in (JobState.FAILED, JobState.CANCELED):
                self.queue.retry_job(jid)
        for kind, ident in self._selection():
            if kind == 'group':
                g = next((x for x in self.queue.snapshot()
                          if x.id == ident), None)
                if g and g.mux_state in (JobState.FAILED,
                                         JobState.CANCELED):
                    self.queue.retry_mux(ident)
        self.refresh()

    def _job_menu(self, menu: QMenu, ident: int):
        job = self.queue.find_job(ident)
        a_start = menu.addAction('Start this job')
        if job is not None and (job.started or job.state.is_terminal()):
            a_start.setEnabled(job.state == JobState.PAUSED)
        a_pause = menu.addAction('Pause job')
        a_cancel = menu.addAction('Cancel job')
        a_retry = menu.addAction('Retry job')
        menu.addSeparator()
        a_track = menu.addAction('Set mux track name…')
        a_open = menu.addAction('Open output folder')
        a_open.setEnabled(bool(job and job.settings.out_path))
        menu.addSeparator()
        a_up = menu.addAction('Move up')
        a_down = menu.addAction('Move down')
        a_remove = menu.addAction('Remove from queue\tDel')
        a_start.triggered.connect(lambda: self.queue.start_job(ident))
        a_pause.triggered.connect(lambda: self.queue.pause_job(ident))
        a_cancel.triggered.connect(lambda: self.queue.cancel_job(ident))
        a_retry.triggered.connect(lambda: self.queue.retry_job(ident))
        a_track.triggered.connect(lambda: self._edit_track_name(ident))
        a_open.triggered.connect(
            lambda: self._open_folder(job.settings.out_path if job else None))
        a_up.triggered.connect(lambda: self.queue.move_job(ident, -1))
        a_down.triggered.connect(lambda: self.queue.move_job(ident, +1))
        a_remove.triggered.connect(lambda: self.queue.remove_job(ident))

    def _ext_menu(self, menu: QMenu, ident: int):
        e = self.queue.find_external(ident)
        a_track = menu.addAction('Set mux track name…')
        a_open = menu.addAction('Open .sup folder')
        a_open.setEnabled(bool(e))
        menu.addSeparator()
        a_remove = menu.addAction('Remove from queue\tDel')
        a_track.triggered.connect(lambda: self._edit_ext_track_name(ident))
        a_open.triggered.connect(
            lambda: self._open_folder(e.sup_path if e else None))
        a_remove.triggered.connect(lambda: self.queue.remove_job(ident))

    def _edit_ext_track_name(self, ext_id: int):
        from PyQt6.QtWidgets import QInputDialog
        e = self.queue.find_external(ext_id)
        if e is None:
            return
        self.before_popup()
        name, ok = QInputDialog.getText(
            self, 'Mux track name',
            'Track name written into the MKV for this subtitle\n'
            '(empty = no name):', text=e.track_name)
        if ok:
            self.queue.set_external_track_name(ext_id, name)
            self.refresh()

    def _group_menu(self, menu: QMenu, ident: int):
        g = next((x for x in self.queue.snapshot() if x.id == ident), None)
        a_start = menu.addAction('Start checked subtitles')
        menu.addSeparator()
        a_sel_all = menu.addAction('Select all subtitles')
        a_sel_none = menu.addAction('Unselect all subtitles')
        a_sel_all.triggered.connect(
            lambda: self.queue.check_all_jobs(ident, True))
        a_sel_none.triggered.connect(
            lambda: self.queue.check_all_jobs(ident, False))
        menu.addSeparator()
        a_mux = menu.addAction('Mux into video when renders finish')
        a_mux.setCheckable(True)
        a_mux.setChecked(bool(g and g.mux_enabled))
        a_repl = menu.addAction('Replace original video (else *.muxed.mkv)')
        a_repl.setCheckable(True)
        a_repl.setChecked(bool(g and g.replace_original))
        a_repl.setEnabled(bool(g and g.mux_enabled))
        a_retry_mux = menu.addAction('Retry mux')
        a_retry_mux.setEnabled(bool(
            g and g.mux_state in (JobState.FAILED, JobState.CANCELED)))
        menu.addSeparator()
        a_open = menu.addAction('Open video folder')
        a_open.setEnabled(bool(g and g.video_path))
        menu.addSeparator()
        a_up = menu.addAction('Move group up')
        a_down = menu.addAction('Move group down')
        a_remove = menu.addAction('Remove group\tDel')
        a_start.triggered.connect(lambda: self.queue.start_group(ident))
        a_mux.toggled.connect(
            lambda on: self.queue.set_group_mux(ident, on))
        a_repl.toggled.connect(
            lambda on: self.queue.set_group_replace(ident, on))
        a_retry_mux.triggered.connect(lambda: self.queue.retry_mux(ident))
        a_open.triggered.connect(
            lambda: self._open_folder(g.video_path if g else None))
        a_up.triggered.connect(lambda: self.queue.move_group(ident, -1))
        a_down.triggered.connect(lambda: self.queue.move_group(ident, +1))
        a_remove.triggered.connect(lambda: self.queue.remove_group(ident))
