"""
Preview pane.

* Renders the selected cue **plus every other cue active at the same
  time** (overlaps shown exactly as they will be in the .sup).
* 16:9 stage with adjustable aspect-ratio matte guides (letterbox /
  pillarbox) — guides only, they never move the subtitles.
* Optional video-frame background (ffmpeg extraction, HDR tone-map
  toggle), extracted at the cue's first frame.
* Pop-out window locked to the exact output pixel size of the target
  .sup.
* "Open in player" launches the user's mapped external player (MPC-BE,
  MPC-HC, VLC, mpv presets) seeked to the cue start.

Rendering runs on a worker thread; results arrive via signals.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QImage, QPainter, QPixmap, QAction,
                         QGuiApplication)
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                             QDoubleSpinBox, QHBoxLayout, QLabel, QMenu,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from ...core.model import Cue, SubtitleDocument
from ...core.overrides import OverrideSet
from ...core.renderer import CanvasSpec, CueRenderer, RenderedCue, compute_canvas
from ...core.video import extract_frame


@dataclass
class PreviewScene:
    canvas_w: int = 1920
    canvas_h: int = 1080
    content: Tuple[float, float, float, float] = (0, 0, 1920, 1080)
    renders: List[RenderedCue] = None
    frame_jpeg: Optional[bytes] = None


class _RenderWorker(QObject):
    """Debounced background renderer for the preview."""
    scene_ready = pyqtSignal(object)     # PreviewScene

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._pending = None
        self._thread: Optional[threading.Thread] = None

    def request(self, doc: SubtitleDocument, cue: Cue,
                overrides: OverrideSet, video_res, video_path: Optional[str],
                want_frame: bool, tone_map: bool):
        with self._lock:
            self._pending = (doc, cue, overrides, video_res, video_path,
                             want_frame, tone_map)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self):
        while True:
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None:
                return
            doc, cue, overrides, video_res, video_path, want_frame, \
                tone_map = job
            try:
                scene = self._render(doc, cue, overrides, video_res,
                                     video_path, want_frame, tone_map)
                self.scene_ready.emit(scene)
            except Exception:
                import traceback
                traceback.print_exc()

    def _render(self, doc, cue, overrides, video_res, video_path,
                want_frame, tone_map) -> PreviewScene:
        canvas = compute_canvas(video_res, overrides.layout)
        renderer = CueRenderer(doc, canvas, overrides)
        scene = PreviewScene(canvas_w=canvas.width, canvas_h=canvas.height,
                             content=canvas.content, renders=[])
        # All cues visible at the first frame of the selected cue
        t = cue.begin_ms + 1.0
        active = [c for c in doc.cues
                  if c.begin_ms <= t < c.end_ms and c.enabled]
        if cue not in active:
            active.append(cue)
        active.sort(key=lambda c: (c.begin_ms, c.uid))
        for c in active:
            rc = renderer.render_cue(c)
            if rc is not None:
                scene.renders.append(rc)
        if want_frame and video_path:
            scene.frame_jpeg = extract_frame(video_path, cue.begin_ms + 1.0,
                                             tone_map=tone_map)
        return scene


class _Stage(QWidget):
    """The 16:9 (or popped-out exact-size) compositing surface."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: Optional[PreviewScene] = None
        self.matte_ar: Optional[float] = None          # guide AR (None = off)
        self.bg_color = QColor('#606060')
        self.show_frame = True
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(200, 112)
        self._frame_pix: Optional[QPixmap] = None
        self._cue_pixmaps: List[Tuple[int, int, QPixmap]] = []

    def set_scene(self, scene: PreviewScene):
        self.scene = scene
        self._frame_pix = None
        if scene.frame_jpeg:
            pm = QPixmap()
            if pm.loadFromData(scene.frame_jpeg):
                self._frame_pix = pm
        self._cue_pixmaps = []
        for rc in scene.renders or []:
            arr = np.ascontiguousarray(rc.bitmap)
            h, w = arr.shape[:2]
            img = QImage(arr.data, w, h, w * 4,
                         QImage.Format.Format_RGBA8888)
            self._cue_pixmaps.append((rc.x, rc.y,
                                      QPixmap.fromImage(img.copy())))
        self.update()

    # ------------------------------------------------------------------ #
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#1c1c1e'))
        if self.scene is None:
            p.end()
            return
        cw, ch = self.scene.canvas_w, self.scene.canvas_h
        # fit canvas into widget
        scale = min(self.width() / cw, self.height() / ch)
        vw, vh = cw * scale, ch * scale
        ox = (self.width() - vw) / 2
        oy = (self.height() - vh) / 2
        canvas_rect = (int(ox), int(oy), int(vw), int(vh))

        # canvas background = black bars
        p.fillRect(*canvas_rect, QColor('#000000'))

        # matte guide: the chosen aspect centered in the canvas
        if self.matte_ar:
            car = cw / ch
            if self.matte_ar >= car:
                mw, mh = vw, vw / self.matte_ar
            else:
                mh, mw = vh, vh * self.matte_ar
            mx = ox + (vw - mw) / 2
            my = oy + (vh - mh) / 2
            p.fillRect(int(mx), int(my), int(mw), int(mh), self.bg_color)
        else:
            p.fillRect(*canvas_rect, self.bg_color)

        # video frame fills the canvas (letterboxed by aspect)
        if self.show_frame and self._frame_pix is not None:
            fp = self._frame_pix
            fs = min(vw / fp.width(), vh / fp.height())
            fw, fh = fp.width() * fs, fp.height() * fs
            fx = ox + (vw - fw) / 2
            fy = oy + (vh - fh) / 2
            p.drawPixmap(int(fx), int(fy), int(fw), int(fh), fp)

        # content-rect guide (dashed) when it differs from full canvas
        cx, cy, cw2, ch2 = self.scene.content
        if abs(cw2 - cw) > 1 or abs(ch2 - ch) > 1:
            pen = p.pen()
            pen.setColor(QColor(90, 160, 255, 160))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(int(ox + cx * scale), int(oy + cy * scale),
                       int(cw2 * scale), int(ch2 * scale))

        # subtitles
        for x, y, pm in self._cue_pixmaps:
            p.drawPixmap(int(ox + x * scale), int(oy + y * scale),
                         int(pm.width() * scale), int(pm.height() * scale),
                         pm)
        p.end()


class PopOutWindow(QWidget):
    """Frameless preview locked to the exact output pixel size."""
    closed = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.Tool)
        self.stage = _Stage(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stage)
        self._drag = QPoint()

    def lock_size(self, w: int, h: int):
        screen = self.screen() or QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        self.setFixedSize(max(1, round(w / dpr)), max(1, round(h / dpr)))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.globalPosition().toPoint() - \
                self.frameGeometry().topLeft()
        elif ev.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            act = menu.addAction('Close pop-out')
            if menu.exec(ev.globalPosition().toPoint()) == act:
                self.close()

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def closeEvent(self, ev):
        self.closed.emit()
        super().closeEvent(ev)


#: external player presets: name -> (executable hint, args template)
PLAYER_PRESETS = {
    'MPC-BE': ('mpc-be64.exe', '"{file}" /start {ms}'),
    'MPC-HC': ('mpc-hc64.exe', '"{file}" /start {ms}'),
    'VLC': ('vlc', '--start-time={sec} "{file}"'),
    'mpv': ('mpv', '--start={sec} "{file}"'),
}


class PreviewPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Optional[SubtitleDocument] = None
        self.cue: Optional[Cue] = None
        self.overrides = OverrideSet()
        self.video_path: Optional[str] = None
        self.video_res: Optional[Tuple[int, int]] = None
        self.app_settings: dict = {}
        self.popout: Optional[PopOutWindow] = None

        self.worker = _RenderWorker()
        self.worker.scene_ready.connect(self._on_scene)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._do_render)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        bar.addWidget(QLabel('Matte AR:'))
        self.spin_ar_w = QDoubleSpinBox()
        self.spin_ar_w.setRange(0.1, 10000)
        self.spin_ar_w.setValue(16.0)
        self.spin_ar_w.setDecimals(3)
        self.spin_ar_h = QDoubleSpinBox()
        self.spin_ar_h.setRange(0.1, 10000)
        self.spin_ar_h.setValue(9.0)
        self.spin_ar_h.setDecimals(3)
        self.chk_matte = QCheckBox('Show matte')
        self.chk_matte.setChecked(True)
        self.btn_bg = QPushButton('BG color')
        self.chk_frames = QCheckBox('Video frames')
        self.chk_tonemap = QCheckBox('Tone-map HDR')
        self.btn_popout = QPushButton('Pop out (1:1)')
        self.btn_player = QPushButton('Open in player ▸')
        for w in (self.spin_ar_w, QLabel(':'), self.spin_ar_h,
                  self.chk_matte, self.btn_bg, self.chk_frames,
                  self.chk_tonemap):
            if isinstance(w, QWidget):
                bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.btn_popout)
        bar.addWidget(self.btn_player)
        lay.addLayout(bar)

        self.stage = _Stage()
        lay.addWidget(self.stage, 1)

        self.lbl_info = QLabel('')
        self.lbl_info.setStyleSheet('color:#9a9a9a;')
        lay.addWidget(self.lbl_info)

        self.spin_ar_w.valueChanged.connect(self._matte_changed)
        self.spin_ar_h.valueChanged.connect(self._matte_changed)
        self.chk_matte.toggled.connect(self._matte_changed)
        self.btn_bg.clicked.connect(self._pick_bg)
        self.chk_frames.toggled.connect(lambda *_: self.schedule_render())
        self.chk_tonemap.toggled.connect(lambda *_: self.schedule_render())
        self.btn_popout.clicked.connect(self._toggle_popout)
        self.btn_player.clicked.connect(self._open_player)
        self._matte_changed()

    # ------------------------------------------------------------------ #
    def set_context(self, doc: Optional[SubtitleDocument],
                    overrides: OverrideSet,
                    video_path: Optional[str],
                    video_res: Optional[Tuple[int, int]],
                    app_settings: dict):
        self.doc = doc
        self.overrides = overrides
        self.video_path = video_path
        self.video_res = video_res
        self.app_settings = app_settings
        if video_res:
            self.spin_ar_w.blockSignals(True)
            self.spin_ar_h.blockSignals(True)
            self.spin_ar_w.setValue(float(video_res[0]))
            self.spin_ar_h.setValue(float(video_res[1]))
            self.spin_ar_w.blockSignals(False)
            self.spin_ar_h.blockSignals(False)
            self._matte_changed()
        self.schedule_render()

    def set_cue(self, cue: Optional[Cue]):
        self.cue = cue
        self.schedule_render()

    def schedule_render(self):
        self._debounce.start()

    def _do_render(self):
        if self.doc is None or self.cue is None:
            return
        self.worker.request(self.doc, self.cue, self.overrides,
                            self.video_res, self.video_path,
                            self.chk_frames.isChecked(),
                            self.chk_tonemap.isChecked())

    def _on_scene(self, scene: PreviewScene):
        self.stage.set_scene(scene)
        n = len(scene.renders or [])
        self.lbl_info.setText(
            f"canvas {scene.canvas_w}x{scene.canvas_h} · "
            f"{n} cue{'s' if n != 1 else ''} on screen"
            + (' · overlap' if n > 1 else ''))
        if self.popout is not None:
            self.popout.lock_size(scene.canvas_w, scene.canvas_h)
            self.popout.stage.matte_ar = self.stage.matte_ar
            self.popout.stage.bg_color = self.stage.bg_color
            self.popout.stage.set_scene(scene)

    # ------------------------------------------------------------------ #
    def _matte_changed(self):
        if self.chk_matte.isChecked() and self.spin_ar_h.value() > 0:
            self.stage.matte_ar = self.spin_ar_w.value() / self.spin_ar_h.value()
        else:
            self.stage.matte_ar = None
        self.stage.update()
        if self.popout:
            self.popout.stage.matte_ar = self.stage.matte_ar
            self.popout.stage.update()

    def _pick_bg(self):
        c = QColorDialog.getColor(self.stage.bg_color, self)
        if c.isValid():
            self.stage.bg_color = c
            self.stage.update()
            if self.popout:
                self.popout.stage.bg_color = c
                self.popout.stage.update()

    def _toggle_popout(self):
        if self.popout is None:
            self.popout = PopOutWindow()
            self.popout.closed.connect(self._popout_closed)
            if self.stage.scene:
                self.popout.lock_size(self.stage.scene.canvas_w,
                                      self.stage.scene.canvas_h)
                self.popout.stage.matte_ar = self.stage.matte_ar
                self.popout.stage.bg_color = self.stage.bg_color
                self.popout.stage.set_scene(self.stage.scene)
            self.popout.show()
            self.btn_popout.setText('Close pop-out')
        else:
            self.popout.close()

    def _popout_closed(self):
        self.popout = None
        self.btn_popout.setText('Pop out (1:1)')

    # ------------------------------------------------------------------ #
    def _open_player(self):
        menu = QMenu(self)
        act_here = menu.addAction('Open at selected cue')
        menu.addSeparator()
        preset_actions = {}
        for name in PLAYER_PRESETS:
            preset_actions[menu.addAction(f'Use preset: {name}')] = name
        chosen = menu.exec(self.btn_player.mapToGlobal(
            QPoint(0, self.btn_player.height())))
        if chosen is None:
            return
        if chosen in preset_actions:
            exe, args = PLAYER_PRESETS[preset_actions[chosen]]
            self.app_settings['external_player'] = exe
            self.app_settings['external_player_args'] = args
            return
        if chosen == act_here:
            self._launch_player()

    def _launch_player(self):
        if not self.video_path or self.cue is None:
            self.lbl_info.setText('No video bound — cannot open player.')
            return
        exe = self.app_settings.get('external_player', '')
        args_tpl = self.app_settings.get('external_player_args',
                                         '"{file}" /start {ms}')
        if not exe:
            self.lbl_info.setText(
                'No player mapped — pick a preset from the button menu, '
                'or set the path in Settings.')
            return
        ms = int(self.cue.begin_ms)
        args = args_tpl.format(file=self.video_path, ms=ms,
                               sec=f"{ms / 1000.0:.3f}")
        try:
            subprocess.Popen([exe] + shlex.split(args, posix=(os.name != 'nt')))
        except OSError as e:
            self.lbl_info.setText(f'Player launch failed: {e}')
