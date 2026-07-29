"""
Main window: workspace layout, menus, queue integration.

Layout
------
┌───────┬──────────────────────────┬───────────────────────────┐
│ Queue │ Cue pane (top-left)      │ Preview (top-right)       │
│ dock  ├─ Selected cue (collapsed)┼───────────────────────────┤
│ (left)│ Sources (bottom-left)    │ Settings (bottom-right)   │
└───────┴──────────────────────────┴───────────────────────────┘
Queue dock defaults to the left (movable to any edge); showing it
widens the window when the screen allows.
"""

from __future__ import annotations

import copy
import os
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (QDockWidget, QFileDialog, QMainWindow,
                             QMessageBox, QSplitter, QStatusBar, QWidget)

from ..core.exporters import export_srt, export_ttml, export_vtt
from ..core.jobqueue import QueueManager
from ..core.pipeline import RenderSettings
from ..core.project import save_project
from .state import AppState, DocumentSession
from .widgets.cue_editor import SelectedCuePane
from .widgets.cue_table import CuePane
from .widgets.preview import PreviewPane
from .widgets.queue_view import QueuePane
from .widgets.settings_panel import SettingsPane
from .widgets.sources import SourcesPane


class MainWindow(QMainWindow):
    queue_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle('TTML2PGS 2 — direct-render subtitle studio')
        self.resize(1720, 1000)

        self.state = AppState()
        self.state.load_settings()

        self.queue = QueueManager(state_path=self.state.queue_state_path)
        self.queue.on_change = self.queue_changed.emit
        self.queue_changed.connect(self._on_queue_changed,
                                   Qt.ConnectionType.QueuedConnection)

        # ---- panes ---------------------------------------------------- #
        self.cue_pane = CuePane()
        self.sel_cue_pane = SelectedCuePane()
        self.sources_pane = SourcesPane(self.state)
        self.preview_pane = PreviewPane()
        self.settings_pane = SettingsPane(self.state.overrides,
                                          self.state.settings)
        self.queue_pane = QueuePane(self.queue)

        # cue table + the collapsible selected-cue editor share a column
        # so expanding the editor borrows space from the table
        from PyQt6.QtWidgets import QVBoxLayout
        cue_col = QWidget()
        ccl = QVBoxLayout(cue_col)
        ccl.setContentsMargins(0, 0, 0, 0)
        ccl.setSpacing(0)
        ccl.addWidget(self.cue_pane, 1)
        ccl.addWidget(self.sel_cue_pane)

        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(cue_col)
        left.addWidget(self.sources_pane)
        left.setSizes([620, 300])
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.preview_pane)
        right.addWidget(self.settings_pane)
        right.setSizes([520, 420])
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([980, 720])
        self.setCentralWidget(split)

        self.queue_dock = QDockWidget('Render queue', self)
        self.queue_dock.setWidget(self.queue_pane)
        # default LEFT (drag to top/bottom/right if preferred)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea,
                           self.queue_dock)
        self.queue_dock.hide()

        self.setStatusBar(QStatusBar())

        # ---- signals --------------------------------------------------- #
        self.sources_pane.session_activated.connect(self._activate_session)
        self.sources_pane.session_changed.connect(self._session_changed)
        self.sources_pane.render_requested.connect(self._render_one)
        self.sources_pane.render_all_requested.connect(self._render_all)
        self.sources_pane.add_sup_requested.connect(self._queue_external_sup)
        self.cue_pane.cue_selected.connect(self.preview_pane.set_cue)
        self.cue_pane.cue_selected.connect(self._cue_selected_for_editor)
        self.cue_pane.cues_changed.connect(self._cues_edited)
        self.sel_cue_pane.changed.connect(self._cue_style_edited)
        self.settings_pane.overrides_changed.connect(self._overrides_edited)
        self.settings_pane.document_changed.connect(self._doc_edited)

        self._build_menu()

        # restore last session
        restored = self.state.restore_session()
        n_queue = self.queue.load_state()
        self.queue.start()
        self.sources_pane.refresh()
        if restored:
            self._activate_session(self.state.active_index)
        if n_queue:
            self._show_queue()
            self.queue_pane.refresh()
            self.statusBar().showMessage(
                f'Restored {n_queue} queued job(s) from last session — '
                f'paused. Resume continues started work; added jobs wait '
                f'for Render all/selected.', 8000)
            self.queue.pause_all()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(30_000)
        self._save_timer.timeout.connect(self._autosave)
        self._save_timer.start()

    # ------------------------------------------------------------------ #
    def _build_menu(self):
        m_file = self.menuBar().addMenu('&File')

        def act(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        act(m_file, '&Open subtitles…', self.sources_pane._add_files,
            'Ctrl+O')
        act(m_file, 'Add &folder…', self.sources_pane._add_folder)
        m_file.addSeparator()
        act(m_file, '&Save project (.t2p)…', self._save_project, 'Ctrl+S')
        m_export = m_file.addMenu('&Export')
        act(m_export, 'TTML…', lambda: self._export('ttml'))
        act(m_export, 'WebVTT…', lambda: self._export('vtt'))
        act(m_export, 'SRT…', lambda: self._export('srt'))
        m_file.addSeparator()
        act(m_file, 'E&xit', self.close, 'Ctrl+Q')

        m_render = self.menuBar().addMenu('&Render')
        act(m_render, 'Add &current file to queue', self._render_current,
            'F5')
        act(m_render, 'Add &all files to queue', self._render_all,
            'Ctrl+F5')
        m_render.addSeparator()
        act(m_render, '&Start queue (render all)', self._start_queue,
            'Ctrl+R')
        act(m_render, 'Show &queue', lambda: self._show_queue())
        act(m_render, '&Pause queue', self.queue.pause_all)
        act(m_render, '&Resume queue', self.queue.resume_all)

        m_help = self.menuBar().addMenu('&Help')
        act(m_help, '&About', self._about)

    # ------------------------------------------------------------------ #
    # Session handling
    # ------------------------------------------------------------------ #
    def _activate_session(self, index: int):
        sess = self.state.active
        if sess is None:
            self.cue_pane.set_document(None)
            self.settings_pane.set_document(None)
            self.sel_cue_pane.set_cue(None, None)
            return
        self.cue_pane.set_document(sess.doc)
        self.settings_pane.set_document(sess.doc)
        self._push_preview_context(sess)
        self.setWindowTitle(
            f'TTML2PGS 2 — {sess.display_name}')

    def _push_preview_context(self, sess: Optional[DocumentSession]):
        if sess is None:
            return
        self.preview_pane.set_context(
            sess.doc, self.state.overrides,
            sess.video_path,
            sess.video_info.resolution if sess.video_info else None,
            self.state.settings,
            is_hdr=bool(sess.video_info and sess.video_info.is_hdr))

    def _session_changed(self, row: int):
        sess = self.state.active
        if sess is not None and row == self.state.active_index:
            self._push_preview_context(sess)
        self.sources_pane.refresh()

    def _cue_selected_for_editor(self, cue):
        sess = self.state.active
        self.sel_cue_pane.set_cue(
            sess.doc if sess else None, cue,
            n_selected=max(1, len(self.cue_pane.selected_cues())))

    def _cue_style_edited(self):
        """Selected-cue pane edits: refresh just that row + preview."""
        sess = self.state.active
        if sess:
            sess.dirty = True
        if self.sel_cue_pane.cue is not None:
            self.cue_pane.refresh_cue(self.sel_cue_pane.cue)
        self.preview_pane.schedule_render()

    def _cues_edited(self):
        """Edits made inside the cue pane (its model is already current)."""
        sess = self.state.active
        if sess:
            sess.dirty = True
        self.preview_pane.schedule_render()

    def _doc_edited(self):
        """Style/region/initial edits from the settings pane."""
        sess = self.state.active
        if sess:
            sess.dirty = True
        self.cue_pane.refresh()
        self.cue_pane.refresh_regions()
        self.preview_pane.schedule_render()

    def _overrides_edited(self):
        self.state.save_settings()
        self.preview_pane.schedule_render()

    def _autosave(self):
        self.state.save_settings()
        self.state.save_session()

    # ------------------------------------------------------------------ #
    # Rendering / queueing
    # ------------------------------------------------------------------ #
    def _show_queue(self):
        """Show the queue dock; when it's docked on a side, widen the
        window so it doesn't crush the other panes (screen permitting)."""
        was_hidden = self.queue_dock.isHidden()
        self.queue_dock.show()
        if not was_hidden or self.queue_dock.isFloating():
            return
        area = self.dockWidgetArea(self.queue_dock)
        if area not in (Qt.DockWidgetArea.LeftDockWidgetArea,
                        Qt.DockWidgetArea.RightDockWidgetArea):
            return
        if self.isMaximized() or self.isFullScreen():
            return
        screen = self.screen()
        if screen is None:
            return
        need = max(self.queue_pane.sizeHint().width(), 340)
        avail = screen.availableGeometry()
        grow = min(need, max(0, avail.width() -
                             self.frameGeometry().width()))
        if grow <= 20:
            return
        self.resize(self.width() + grow, self.height())
        fg = self.frameGeometry()
        if fg.right() > avail.right():          # keep fully on screen
            self.move(self.x() - (fg.right() - avail.right()), self.y())

    def _start_queue(self):
        self.queue.start_all()
        self._show_queue()
        self.queue_pane.refresh()

    def _render_current(self):
        if self.state.active_index >= 0:
            self._render_one(self.state.active_index)

    def _render_one(self, row: int):
        if not (0 <= row < len(self.state.sessions)):
            return
        sess = self.state.sessions[row]
        self._enqueue(sess)
        self._show_queue()
        self.queue_pane.refresh()

    def _render_all(self):
        for sess in self.state.sessions:
            self._enqueue(sess)
        self._show_queue()
        self.queue_pane.refresh()

    def _enqueue(self, sess: DocumentSession):
        settings = RenderSettings(
            out_path=sess.out_path or sess.default_out_path(),
            video_res=(sess.video_info.resolution
                       if sess.video_info else None),
            target_fps=sess.target_fps(),
            retime=sess.retime_plan(),
            offset_ms=sess.offset_ms,
            selected_only=self.sources_pane.selected_cues_only(),
            is_hdr=bool(sess.video_info and sess.video_info.is_hdr))
        # snapshot doc + overrides so later edits don't affect queued work
        doc_snapshot = copy.deepcopy(sess.doc)
        ov_snapshot = copy.deepcopy(self.state.overrides)
        video = sess.video_path if self.state.settings.get(
            'remux_after_render', True) else None
        job = self.queue.add_render(
            doc_snapshot, sess.sub_path, settings, ov_snapshot,
            video_path=sess.video_path, lang=sess.doc.language)
        group = None
        for g in self.queue.snapshot():
            if job in g.render_jobs:
                group = g
                break
        if group is not None:
            group.mux_enabled = bool(
                sess.video_path and
                self.state.settings.get('remux_after_render', True))
            group.replace_original = self.state.settings.get(
                'replace_original', True)
        self.queue.move_to_subs = self.state.settings.get(
            'move_to_subs_folder', False)
        self.statusBar().showMessage(
            f'Added {os.path.basename(settings.out_path)} to the queue — '
            f'start it from the queue panel', 5000)

    def _queue_external_sup(self, video_path: str, sup_path: str):
        from ..core.parsers import detect_language_from_filename
        lang = detect_language_from_filename(sup_path) or 'und'
        self.queue.add_external_sup(video_path, sup_path, lang=lang)
        self._show_queue()
        self.queue_pane.refresh()

    def _on_queue_changed(self):
        self.queue_pane.refresh()

    # ------------------------------------------------------------------ #
    # Save / export
    # ------------------------------------------------------------------ #
    def _save_project(self):
        sess = self.state.active
        if sess is None:
            return
        default = os.path.splitext(sess.sub_path)[0] + '.t2p'
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save project', default, 'TTML2PGS project (*.t2p)')
        if not path:
            return
        save_project(path, sess.doc, self.state.overrides, {
            'video_path': sess.video_path,
            'offset_ms': sess.offset_ms,
            'out_path': sess.out_path,
        })
        sess.dirty = False
        self.statusBar().showMessage(f'Saved {path}', 4000)

    def _export(self, fmt: str):
        sess = self.state.active
        if sess is None:
            return
        ext = {'ttml': '.ttml', 'vtt': '.vtt', 'srt': '.srt'}[fmt]
        default = os.path.splitext(sess.sub_path)[0] + ext
        path, _ = QFileDialog.getSaveFileName(
            self, f'Export {fmt.upper()}', default, f'*{ext}')
        if not path:
            return
        try:
            if fmt == 'ttml':
                text = export_ttml(sess.doc)
            elif fmt == 'vtt':
                text = export_vtt(sess.doc)
            else:
                text = export_srt(sess.doc)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            self.statusBar().showMessage(f'Exported {path}', 4000)
        except Exception as e:
            QMessageBox.warning(self, 'Export failed', str(e))

    def _about(self):
        QMessageBox.about(
            self, 'TTML2PGS 2',
            '<b>TTML2PGS 2</b><br>'
            'Direct pixel renderer for TTML / WebVTT / SRT → PGS (.sup), '
            'with CJK-correct fonts, ruby, vertical text, per-language '
            'overrides and a video-grouped render/mux queue.<br><br>'
            'HarfBuzz + FreeType + NumPy — no browser involved.')

    # ------------------------------------------------------------------ #
    def closeEvent(self, ev):
        dirty = [s for s in self.state.sessions if s.dirty]
        if dirty and not self.queue.is_idle():
            pass  # queue keeps its own persistent state
        if dirty:
            names = '\n'.join(f'• {s.display_name}' for s in dirty[:8])
            r = QMessageBox.question(
                self, 'Unsaved changes',
                f'These files have unsaved edits:\n{names}\n\n'
                'Close anyway? (Use File → Save project to keep edits.)',
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.Cancel)
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        self.state.save_settings()
        self.state.save_session()
        self.queue.shutdown()
        self.preview_pane.shutdown_players()
        if self.preview_pane.popout:
            self.preview_pane.popout.close()
        super().closeEvent(ev)
