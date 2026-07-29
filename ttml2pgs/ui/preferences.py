"""
Preferences dialog.

Application-level settings that aren't part of a document or the
per-render overrides:

* **Default profiles** — fallback "initials" per language. A profile is
  applied only where the subtitle file itself specifies nothing (no
  inline styling, no named style, no document initials), i.e. it fills
  the gaps the file leaves open. The Default profile applies to every
  subtitle; a language profile, when present, is used *instead of*
  Default for subtitles in that language.
* **Player** — embedded preview engine (mpv / Qt Multimedia) and the
  external player used for the video the subtitles are synced against.
* **Performance** — parallel rendering (worker process count).
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QScrollArea, QSpinBox, QSplitter,
                             QTabWidget, QVBoxLayout, QWidget)

from ..core.model import Style
from ..core.overrides import OverrideSet
from ..core.pipeline import MIN_PARALLEL_CUES, auto_workers
from .widgets.settings_panel import StyleEditor, compact, guard_wheel_children


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet('color: palette(mid); font-size: 11px;')
    return lbl


class PreferencesDialog(QDialog):
    """Non-modal preferences window (menu bar → Preferences)."""

    #: default profiles changed → re-render previews + persist
    profiles_changed = pyqtSignal()
    #: app settings (player / performance) changed → persist
    settings_changed = pyqtSignal()

    def __init__(self, overrides: OverrideSet, app_settings: Dict,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle('Preferences')
        self.overrides = overrides
        self.app_settings = app_settings
        self.resize(720, 520)

        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)

        self._build_profiles_tab()
        self._build_player_tab()
        self._build_performance_tab()

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.close)
        lay.addWidget(btns)
        guard_wheel_children(self)

    # ------------------------------------------------------------------ #
    # Default profiles
    # ------------------------------------------------------------------ #
    def _build_profiles_tab(self):
        tab = QWidget()
        tl = QVBoxLayout(tab)
        tl.addWidget(_hint(
            'Profiles are fallback defaults — "the initials if no '
            'initials are set". A checked property applies only where '
            'the subtitle file itself specifies nothing (no inline '
            'styling, no named style, no document initials), so files '
            'that do carry styling are left untouched. The Default '
            'profile applies to every subtitle; add a language profile '
            'to be used instead of Default for that language.'))

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(2, 2, 2, 2)
        self.profile_list = QListWidget()
        ll.addWidget(self.profile_list)
        row = QHBoxLayout()
        self.btn_add_profile = QPushButton('Add language…')
        self.btn_del_profile = QPushButton('Remove')
        row.addWidget(self.btn_add_profile)
        row.addWidget(self.btn_del_profile)
        ll.addLayout(row)
        split.addWidget(left)

        self.profile_editor = StyleEditor()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.profile_editor)
        split.addWidget(scroll)
        split.setSizes([170, 430])
        tl.addWidget(split, 1)
        self.tabs.addTab(tab, 'Default profiles')

        self.btn_add_profile.clicked.connect(self._add_profile)
        self.btn_del_profile.clicked.connect(self._del_profile)
        self.profile_list.currentItemChanged.connect(self._profile_selected)
        self.profile_editor.changed.connect(self.profiles_changed.emit)
        self._reload_profiles()

    def _profile_keys(self):
        return [''] + sorted(k for k in self.overrides.profiles if k)

    def _reload_profiles(self, select: str = ''):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for key in self._profile_keys():
            item = QListWidgetItem(key if key else 'Default')
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.profile_list.addItem(item)
            if key == select:
                self.profile_list.setCurrentItem(item)
        self.profile_list.blockSignals(False)
        if self.profile_list.currentRow() < 0:
            self.profile_list.setCurrentRow(0)
        else:
            self._profile_selected(self.profile_list.currentItem(), None)

    def _profile_selected(self, item, _prev=None):
        if item is None:
            self.profile_editor.load(None)
            self.btn_del_profile.setEnabled(False)
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        style = self.overrides.profiles.setdefault(
            key, Style(id='__profile__'))
        self.profile_editor.load(style)
        self.btn_del_profile.setEnabled(bool(key))

    def _add_profile(self):
        lang, ok = QInputDialog.getText(
            self, 'Add language profile',
            'Language code (e.g. ja, zh-Hant, en):')
        lang = (lang or '').strip().lower()
        if not ok or not lang:
            return
        self.overrides.profiles.setdefault(lang, Style(id='__profile__'))
        self._reload_profiles(select=lang)

    def _del_profile(self):
        item = self.profile_list.currentItem()
        key = item.data(Qt.ItemDataRole.UserRole) if item else ''
        if not key:
            return                       # Default row can't be removed
        self.overrides.profiles.pop(key, None)
        self._reload_profiles()
        self.profiles_changed.emit()

    # ------------------------------------------------------------------ #
    # Player
    # ------------------------------------------------------------------ #
    def _build_player_tab(self):
        tab = QWidget()
        fl = QFormLayout(tab)
        fl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        s = self.app_settings

        self.cmb_engine = QComboBox()
        self.cmb_engine.addItems([
            'Auto — mpv when available (HDR-correct)',
            'Qt Multimedia only'])
        self.cmb_engine.setCurrentIndex(
            1 if s.get('player_engine', 'auto') == 'qt' else 0)
        self.cmb_engine.setToolTip(
            'Engine for the EMBEDDED preview player. mpv (libmpv) '
            'tone-maps HDR correctly and decodes almost anything — '
            'install mpv, or on Windows drop libmpv-2.dll into the '
            'folder below. Qt Multimedia is the fallback (no HDR tone '
            'mapping).')
        compact(self.cmb_engine)
        fl.addRow('Embedded engine:', self.cmb_engine)

        self.ed_mpv_dir = QLineEdit(s.get('mpv_dll_dir', ''))
        self.ed_mpv_dir.setPlaceholderText(
            r'folder containing libmpv-2.dll (Windows only)')
        fl.addRow('libmpv folder:', self.ed_mpv_dir)

        fl.addRow(_hint('External player: used by "Open in player" to '
                        'show the video the subtitles are overlaid on '
                        '(for eyeballing sync). {file} = video path, '
                        '{ms} = start position in milliseconds.'))
        self.ed_player = QLineEdit(s.get('external_player', ''))
        self.ed_player.setPlaceholderText(
            r'e.g. C:\Program Files\MPC-BE\mpc-be64.exe')
        fl.addRow('External exe:', self.ed_player)
        self.ed_player_args = QLineEdit(
            s.get('external_player_args', '"{file}" /start {ms}'))
        fl.addRow('Arguments:', self.ed_player_args)

        self.cmb_engine.currentIndexChanged.connect(self._player_changed)
        self.ed_mpv_dir.editingFinished.connect(self._player_changed)
        self.ed_player.editingFinished.connect(self._player_changed)
        self.ed_player_args.editingFinished.connect(self._player_changed)
        self.tabs.addTab(tab, 'Player')

    def _player_changed(self, *_):
        s = self.app_settings
        s['player_engine'] = ('qt' if self.cmb_engine.currentIndex() == 1
                              else 'auto')
        s['mpv_dll_dir'] = self.ed_mpv_dir.text().strip()
        s['external_player'] = self.ed_player.text().strip()
        s['external_player_args'] = self.ed_player_args.text().strip()
        self.settings_changed.emit()

    # ------------------------------------------------------------------ #
    # Performance
    # ------------------------------------------------------------------ #
    def _build_performance_tab(self):
        tab = QWidget()
        fl = QFormLayout(tab)
        fl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(0, 32)
        self.spin_workers.setSpecialValueText(
            f'Auto ({auto_workers()} on this machine)')
        self.spin_workers.setValue(
            int(self.app_settings.get('render_workers', 0) or 0))
        compact(self.spin_workers)
        fl.addRow('Render processes:', self.spin_workers)
        fl.addRow(_hint(
            'Cues render in parallel across this many worker processes '
            '(the .sup output is byte-identical to a single-process '
            'render). Auto = CPU cores minus one, capped at 8 — one '
            'core stays free for the UI and muxing. 1 disables '
            f'parallelism; jobs under {MIN_PARALLEL_CUES} cues always '
            'render in-process because starting workers would cost '
            'more than it saves.'))

        self.spin_workers.valueChanged.connect(self._perf_changed)
        self.tabs.addTab(tab, 'Performance')

    def _perf_changed(self, *_):
        self.app_settings['render_workers'] = int(self.spin_workers.value())
        self.settings_changed.emit()

    # ------------------------------------------------------------------ #
    def closeEvent(self, ev):
        # drop profiles that ended up with nothing set — an empty
        # language profile would otherwise shadow Default
        empty = [k for k, v in self.overrides.profiles.items()
                 if not v.set_props()]
        for k in empty:
            del self.overrides.profiles[k]
        super().closeEvent(ev)
