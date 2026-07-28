"""
Queue window: video-grouped tree of render jobs + mux status, with
pause/resume/cancel/retry/reorder controls, live progress, and drag-free
reordering via move buttons.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QMenu,
                             QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from ...core.jobqueue import JobState, QueueManager

_STATE_COLORS = {
    JobState.PENDING: '#a0a0a0',
    JobState.RUNNING: '#6aa7ff',
    JobState.PAUSED: '#e0b040',
    JobState.DONE: '#69d26a',
    JobState.FAILED: '#ff6a6a',
    JobState.CANCELED: '#808080',
    JobState.WAITING: '#9a9a9a',
}


class QueuePane(QWidget):
    def __init__(self, queue: QueueManager):
        super().__init__()
        self.queue = queue
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        self.b_pause = QPushButton('Pause queue')
        self.b_resume = QPushButton('Resume queue')
        self.b_clear = QPushButton('Clear finished')
        bar.addWidget(self.b_pause)
        bar.addWidget(self.b_resume)
        bar.addWidget(self.b_clear)
        bar.addStretch()
        self.lbl_status = QLabel('')
        bar.addWidget(self.lbl_status)
        lay.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(['Item', 'State', 'Progress', 'Info'])
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 90)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.tree)

        self.b_pause.clicked.connect(self.queue.pause_all)
        self.b_resume.clicked.connect(self.queue.resume_all)
        self.b_clear.clicked.connect(self._clear_finished)
        self.tree.customContextMenuRequested.connect(self._menu)

    # ------------------------------------------------------------------ #
    def refresh(self):
        expanded: Dict[int, bool] = {}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            expanded[it.data(0, Qt.ItemDataRole.UserRole)] = it.isExpanded()
        self.tree.clear()

        groups = self.queue.snapshot()
        total = done = 0
        for g in groups:
            gi = QTreeWidgetItem([g.label(), '', '', ''])
            gi.setData(0, Qt.ItemDataRole.UserRole, g.id)
            gi.setData(1, Qt.ItemDataRole.UserRole, 'group')
            if g.video_path:
                mux_txt = {JobState.WAITING: 'mux: waiting for renders',
                           JobState.RUNNING: 'mux: running',
                           JobState.DONE: 'mux: done',
                           JobState.FAILED: f'mux FAILED: {g.mux_error}',
                           }.get(g.mux_state, '')
                if not g.mux_enabled:
                    mux_txt = 'mux: disabled'
                gi.setText(3, mux_txt or g.mux_message)
                if g.mux_state == JobState.RUNNING:
                    gi.setText(2, f"{g.mux_progress * 100:.0f}%")
            self.tree.addTopLevelItem(gi)
            for j in g.render_jobs:
                total += 1
                if j.state == JobState.DONE:
                    done += 1
                ji = QTreeWidgetItem([
                    j.label(), j.state.value,
                    f"{j.progress * 100:.0f}%", j.error or j.message])
                ji.setData(0, Qt.ItemDataRole.UserRole, j.id)
                ji.setData(1, Qt.ItemDataRole.UserRole, 'job')
                ji.setForeground(1, QBrush(QColor(
                    _STATE_COLORS.get(j.state, '#ccc'))))
                gi.addChild(ji)
            for e in g.external_sups:
                ei = QTreeWidgetItem([
                    os.path.basename(e.sup_path), 'external', '',
                    f'lang={e.lang}'])
                ei.setData(0, Qt.ItemDataRole.UserRole, e.id)
                ei.setData(1, Qt.ItemDataRole.UserRole, 'external')
                gi.addChild(ei)
            gi.setExpanded(expanded.get(g.id, True))

        paused = self.queue.is_paused()
        state = 'PAUSED' if paused else \
            ('idle' if self.queue.is_idle() else 'working')
        self.lbl_status.setText(f"{done}/{total} renders done · {state}")
        self.b_pause.setEnabled(not paused)
        self.b_resume.setEnabled(paused)

    # ------------------------------------------------------------------ #
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

    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(1, Qt.ItemDataRole.UserRole)
        ident = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if kind == 'job':
            a_pause = menu.addAction('Pause job')
            a_resume = menu.addAction('Resume job')
            a_cancel = menu.addAction('Cancel job')
            a_retry = menu.addAction('Retry job')
            menu.addSeparator()
            a_up = menu.addAction('Move up')
            a_down = menu.addAction('Move down')
            a_remove = menu.addAction('Remove from queue')
            act = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if act == a_pause:
                self.queue.pause_job(ident)
            elif act == a_resume:
                self.queue.resume_job(ident)
            elif act == a_cancel:
                self.queue.cancel_job(ident)
            elif act == a_retry:
                self.queue.retry_job(ident)
            elif act == a_up:
                self.queue.move_job(ident, -1)
            elif act == a_down:
                self.queue.move_job(ident, +1)
            elif act == a_remove:
                self.queue.remove_job(ident)
        elif kind == 'group':
            g = next((x for x in self.queue.snapshot() if x.id == ident), None)
            a_mux = menu.addAction(
                'Disable mux for this video' if (g and g.mux_enabled)
                else 'Enable mux for this video')
            a_up = menu.addAction('Move group up')
            a_down = menu.addAction('Move group down')
            a_remove = menu.addAction('Remove group')
            act = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if act == a_mux and g:
                self.queue.set_group_mux(ident, not g.mux_enabled)
            elif act == a_up:
                self.queue.move_group(ident, -1)
            elif act == a_down:
                self.queue.move_group(ident, +1)
            elif act == a_remove:
                self.queue.remove_group(ident)
        elif kind == 'external':
            a_remove = menu.addAction('Remove from queue')
            act = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if act == a_remove:
                self.queue.remove_job(ident)
        self.refresh()
