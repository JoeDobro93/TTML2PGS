"""
Preview pane with two modes:

* **Player mode** (embedded, SubtitleEdit-style) — a real QtMultimedia
  player renders the bound video with subtitle overlays composited live
  and kept in sync during playback (overlapping cues included, straight
  from the same CueRenderer that feeds the .sup encoder). Selecting a cue
  seeks to its first frame, paused. Falls back to stills automatically
  when the platform can't decode the file.
* **Stills mode** — canvas mock-up with matte AR guides, optional
  ffmpeg-extracted video frame (HDR tone-map toggle) behind the cues.

Plus: pop-out window locked 1:1 to the output pixel size (opening it
pauses the embedded player), and external-player hand-off (MPC-BE /
MPC-HC / VLC / mpv presets) seeked to the selected cue.
"""

from __future__ import annotations

import bisect
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import (QObject, QPoint, QRectF, QSizeF, Qt, QTimer, QUrl,
                          pyqtSignal)
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                             QDoubleSpinBox, QGraphicsPixmapItem,
                             QGraphicsScene, QGraphicsView, QHBoxLayout,
                             QLabel, QMenu, QPushButton, QSizePolicy,
                             QSlider, QStackedLayout, QVBoxLayout, QWidget)

from ...core.model import Cue, SubtitleDocument
from ...core.overrides import OverrideSet
from ...core.renderer import CueRenderer, RenderedCue, compute_canvas
from ...core.video import extract_frame

# Embedded playback is optional — degrade to stills when QtMultimedia or
# its platform backend is unavailable.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    MULTIMEDIA_AVAILABLE = True
except Exception:                                    # pragma: no cover
    MULTIMEDIA_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Background rendering
# --------------------------------------------------------------------------- #

@dataclass
class PreviewScene:
    canvas_w: int = 1920
    canvas_h: int = 1080
    content: Tuple[float, float, float, float] = (0, 0, 1920, 1080)
    renders: List[RenderedCue] = None
    frame_jpeg: Optional[bytes] = None


@dataclass
class _RenderContext:
    doc: SubtitleDocument
    overrides: OverrideSet
    video_res: Optional[Tuple[int, int]]
    video_path: Optional[str]
    is_hdr: bool = False
    generation: int = 0


class _RenderWorker(QObject):
    """Debounced background renderer for scenes + single-cue requests."""
    scene_ready = pyqtSignal(object)                 # PreviewScene
    cue_ready = pyqtSignal(int, object, int)         # uid, RenderedCue|None, gen

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._scene_job = None
        self._cue_jobs: List[tuple] = []
        self._thread: Optional[threading.Thread] = None

    def request_scene(self, ctx: _RenderContext, cue: Cue,
                      want_frame: bool, tone_map: bool):
        with self._lock:
            self._scene_job = (ctx, cue, want_frame, tone_map)
            self._kick()

    def request_cue(self, ctx: _RenderContext, cue: Cue):
        with self._lock:
            self._cue_jobs.append((ctx, cue))
            self._kick()

    def _kick(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            with self._lock:
                job = None
                kind = None
                if self._scene_job is not None:
                    job, self._scene_job = self._scene_job, None
                    kind = 'scene'
                elif self._cue_jobs:
                    job = self._cue_jobs.pop(0)
                    kind = 'cue'
            if job is None:
                return
            try:
                if kind == 'scene':
                    self._do_scene(*job)
                else:
                    self._do_cue(*job)
            except Exception:                        # pragma: no cover
                import traceback
                traceback.print_exc()

    def _make_renderer(self, ctx: _RenderContext):
        canvas = compute_canvas(ctx.video_res, ctx.overrides.layout)
        return canvas, CueRenderer(ctx.doc, canvas, ctx.overrides,
                                   is_hdr=ctx.is_hdr)

    def _do_scene(self, ctx: _RenderContext, cue: Cue,
                  want_frame: bool, tone_map: bool):
        canvas, renderer = self._make_renderer(ctx)
        scene = PreviewScene(canvas_w=canvas.width, canvas_h=canvas.height,
                             content=canvas.content, renders=[])
        t = cue.begin_ms + 1.0
        active = [c for c in ctx.doc.cues
                  if c.begin_ms <= t < c.end_ms and c.enabled]
        if cue not in active:
            active.append(cue)
        active.sort(key=lambda c: (c.begin_ms, c.uid))
        for c in active:
            rc = renderer.render_cue(c)
            if rc is not None:
                scene.renders.append(rc)
        if want_frame and ctx.video_path:
            scene.frame_jpeg = extract_frame(ctx.video_path,
                                             cue.begin_ms + 1.0,
                                             tone_map=tone_map)
        self.scene_ready.emit(scene)

    def _do_cue(self, ctx: _RenderContext, cue: Cue):
        _, renderer = self._make_renderer(ctx)
        rc = renderer.render_cue(cue)
        self.cue_ready.emit(cue.uid, rc, ctx.generation)


# --------------------------------------------------------------------------- #
# Stills stage (canvas mock-up)
# --------------------------------------------------------------------------- #

class _Stage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: Optional[PreviewScene] = None
        self.matte_ar: Optional[float] = None
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
            img = QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
            self._cue_pixmaps.append((rc.x, rc.y,
                                      QPixmap.fromImage(img.copy())))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#1c1c1e'))
        if self.scene is None:
            p.end()
            return
        cw, ch = self.scene.canvas_w, self.scene.canvas_h
        scale = min(self.width() / cw, self.height() / ch)
        vw, vh = cw * scale, ch * scale
        ox = (self.width() - vw) / 2
        oy = (self.height() - vh) / 2
        p.fillRect(int(ox), int(oy), int(vw), int(vh), QColor('#000000'))

        if self.matte_ar:
            car = cw / ch
            if self.matte_ar >= car:
                mw, mh = vw, vw / self.matte_ar
            else:
                mh, mw = vh, vh * self.matte_ar
            p.fillRect(int(ox + (vw - mw) / 2), int(oy + (vh - mh) / 2),
                       int(mw), int(mh), self.bg_color)
        else:
            p.fillRect(int(ox), int(oy), int(vw), int(vh), self.bg_color)

        if self.show_frame and self._frame_pix is not None:
            fp = self._frame_pix
            fs = min(vw / fp.width(), vh / fp.height())
            fw, fh = fp.width() * fs, fp.height() * fs
            p.drawPixmap(int(ox + (vw - fw) / 2), int(oy + (vh - fh) / 2),
                         int(fw), int(fh), fp)

        cx, cy, cw2, ch2 = self.scene.content
        if abs(cw2 - cw) > 1 or abs(ch2 - ch) > 1:
            pen = p.pen()
            pen.setColor(QColor(90, 160, 255, 160))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(int(ox + cx * scale), int(oy + cy * scale),
                       int(cw2 * scale), int(ch2 * scale))

        for x, y, pm in self._cue_pixmaps:
            p.drawPixmap(int(ox + x * scale), int(oy + y * scale),
                         int(pm.width() * scale), int(pm.height() * scale),
                         pm)
        p.end()


# --------------------------------------------------------------------------- #
# Player stage (embedded video + live overlays)
# --------------------------------------------------------------------------- #

class _PlayerView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor('#1c1c1e'))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.video_item = None
        if MULTIMEDIA_AVAILABLE:
            self.video_item = QGraphicsVideoItem()
            self.scene().addItem(self.video_item)
        self._overlay_items: List[QGraphicsPixmapItem] = []
        self._canvas = (1920, 1080)

    def set_canvas(self, w: int, h: int):
        self._canvas = (w, h)
        self.scene().setSceneRect(QRectF(0, 0, w, h))
        if self.video_item is not None:
            self.video_item.setSize(QSizeF(w, h))
        self._fit()

    def set_overlays(self, renders: List[RenderedCue]):
        for it in self._overlay_items:
            self.scene().removeItem(it)
        self._overlay_items.clear()
        for rc in renders:
            arr = np.ascontiguousarray(rc.bitmap)
            h, w = arr.shape[:2]
            img = QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
            item = QGraphicsPixmapItem(QPixmap.fromImage(img.copy()))
            item.setPos(rc.x, rc.y)
            item.setZValue(10)
            self.scene().addItem(item)
            self._overlay_items.append(item)

    def _fit(self):
        self.fitInView(QRectF(0, 0, *self._canvas),
                       Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()


# --------------------------------------------------------------------------- #
# Pop-out
# --------------------------------------------------------------------------- #

class PopOutWindow(QWidget):
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
        from PyQt6.QtGui import QGuiApplication
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


PLAYER_PRESETS = {
    'MPC-BE': ('mpc-be64.exe', '"{file}" /start {ms}'),
    'MPC-HC': ('mpc-hc64.exe', '"{file}" /start {ms}'),
    'VLC': ('vlc', '--start-time={sec} "{file}"'),
    'mpv': ('mpv', '--start={sec} "{file}"'),
}


def _fmt_ms(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# The pane
# --------------------------------------------------------------------------- #

class PreviewPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Optional[SubtitleDocument] = None
        self.cue: Optional[Cue] = None
        self.overrides = OverrideSet()
        self.video_path: Optional[str] = None
        self.video_res: Optional[Tuple[int, int]] = None
        self.is_hdr = False
        self.app_settings: dict = {}
        self.popout: Optional[PopOutWindow] = None
        self._generation = 0
        self._player_cache: Dict[int, Optional[RenderedCue]] = {}
        self._pending_cues: set = set()
        self._cue_starts: List[float] = []
        self._sorted_cues: List[Cue] = []
        self._player = None
        self._audio = None
        self._player_failed = False

        self.worker = _RenderWorker()
        self.worker.scene_ready.connect(self._on_scene)
        self.worker.cue_ready.connect(self._on_cue_render)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._do_render)

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._tick)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        self.chk_player = QCheckBox('Embedded player')
        self.chk_player.setToolTip(
            'Play the bound video right here with live subtitle overlays '
            '(SubtitleEdit-style). Unavailable if the platform cannot '
            'decode the file — stills mode is used instead.')
        self.chk_player.setEnabled(MULTIMEDIA_AVAILABLE)
        bar.addWidget(self.chk_player)
        bar.addWidget(QLabel('Matte AR:'))
        self.spin_ar_w = QDoubleSpinBox()
        self.spin_ar_w.setRange(0.1, 10000)
        self.spin_ar_w.setValue(16.0)
        self.spin_ar_w.setDecimals(3)
        self.spin_ar_h = QDoubleSpinBox()
        self.spin_ar_h.setRange(0.1, 10000)
        self.spin_ar_h.setValue(9.0)
        self.spin_ar_h.setDecimals(3)
        self.chk_matte = QCheckBox('Matte')
        self.chk_matte.setChecked(True)
        self.btn_bg = QPushButton('BG')
        self.chk_frames = QCheckBox('Video frames')
        self.chk_tonemap = QCheckBox('Tone-map HDR')
        for w in (self.spin_ar_w, QLabel(':'), self.spin_ar_h,
                  self.chk_matte, self.btn_bg, self.chk_frames,
                  self.chk_tonemap):
            bar.addWidget(w)
        bar.addStretch()
        self.btn_popout = QPushButton('Pop out (1:1)')
        self.btn_player = QPushButton('Open in player ▸')
        bar.addWidget(self.btn_popout)
        bar.addWidget(self.btn_player)
        lay.addLayout(bar)

        self._stack = QStackedLayout()
        self.stage = _Stage()
        self.player_view = _PlayerView()
        holder = QWidget()
        holder.setLayout(self._stack)
        self._stack.addWidget(self.stage)
        self._stack.addWidget(self.player_view)
        lay.addWidget(holder, 1)

        # transport controls
        transport = QHBoxLayout()
        self.btn_play = QPushButton('▶')
        self.btn_play.setFixedWidth(36)
        self.btn_play.setEnabled(False)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.lbl_time = QLabel('00:00:00')
        self.chk_mute = QCheckBox('Mute')
        transport.addWidget(self.btn_play)
        transport.addWidget(self.slider, 1)
        transport.addWidget(self.lbl_time)
        transport.addWidget(self.chk_mute)
        lay.addLayout(transport)

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
        self.btn_player.clicked.connect(self._open_player_menu)
        self.chk_player.toggled.connect(self._player_mode_changed)
        self.btn_play.clicked.connect(self._toggle_play)
        self.slider.sliderMoved.connect(self._slider_seek)
        self.chk_mute.toggled.connect(self._mute_changed)
        self._matte_changed()

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #
    def set_context(self, doc: Optional[SubtitleDocument],
                    overrides: OverrideSet,
                    video_path: Optional[str],
                    video_res: Optional[Tuple[int, int]],
                    app_settings: dict,
                    is_hdr: bool = False):
        video_changed = video_path != self.video_path
        self.doc = doc
        self.overrides = overrides
        self.video_path = video_path
        self.video_res = video_res
        self.is_hdr = is_hdr
        self.app_settings = app_settings
        self._invalidate_renders()
        self._rebuild_cue_index()
        if video_res:
            self.spin_ar_w.blockSignals(True)
            self.spin_ar_h.blockSignals(True)
            self.spin_ar_w.setValue(float(video_res[0]))
            self.spin_ar_h.setValue(float(video_res[1]))
            self.spin_ar_w.blockSignals(False)
            self.spin_ar_h.blockSignals(False)
            self._matte_changed()
        if video_changed:
            self._player_failed = False
            if self._player is not None:
                self._load_player_source()
        self.schedule_render()

    def _ctx(self) -> Optional[_RenderContext]:
        if self.doc is None:
            return None
        return _RenderContext(self.doc, self.overrides, self.video_res,
                              self.video_path, self.is_hdr,
                              self._generation)

    def _invalidate_renders(self):
        self._generation += 1
        self._player_cache.clear()
        self._pending_cues.clear()

    def _rebuild_cue_index(self):
        if self.doc is None:
            self._sorted_cues, self._cue_starts = [], []
            return
        self._sorted_cues = self.doc.sorted_cues()
        self._cue_starts = [c.begin_ms for c in self._sorted_cues]

    def set_cue(self, cue: Optional[Cue]):
        self.cue = cue
        if cue is not None and self._player_active():
            self._player.setPosition(int(cue.begin_ms) + 10)
            self._player.pause()
            self._sync_play_button()
            self._update_overlays(cue.begin_ms + 10)
        self.schedule_render()

    # ------------------------------------------------------------------ #
    # Stills pipeline
    # ------------------------------------------------------------------ #
    def schedule_render(self):
        self._invalidate_renders()
        self._rebuild_cue_index()
        self._debounce.start()

    def _do_render(self):
        ctx = self._ctx()
        if ctx is None or self.cue is None:
            return
        if self._player_active():
            self._update_overlays(self._player.position(), force=True)
        self.worker.request_scene(ctx, self.cue,
                                  self.chk_frames.isChecked() and
                                  not self._player_active(),
                                  self.chk_tonemap.isChecked())

    def _on_scene(self, scene: PreviewScene):
        self.stage.set_scene(scene)
        self.player_view.set_canvas(scene.canvas_w, scene.canvas_h)
        n = len(scene.renders or [])
        mode = 'player' if self._player_active() else 'stills'
        self.lbl_info.setText(
            f"canvas {scene.canvas_w}x{scene.canvas_h} · {n} cue"
            f"{'s' if n != 1 else ''} on screen · {mode}"
            + (' · HDR' if self.is_hdr else ''))
        if self.popout is not None:
            self.popout.lock_size(scene.canvas_w, scene.canvas_h)
            self.popout.stage.matte_ar = self.stage.matte_ar
            self.popout.stage.bg_color = self.stage.bg_color
            self.popout.stage.set_scene(scene)

    # ------------------------------------------------------------------ #
    # Embedded player
    # ------------------------------------------------------------------ #
    def _player_active(self) -> bool:
        return (MULTIMEDIA_AVAILABLE and self.chk_player.isChecked()
                and self._player is not None and not self._player_failed)

    def _player_mode_changed(self, on: bool):
        if on and not MULTIMEDIA_AVAILABLE:
            self.chk_player.setChecked(False)
            return
        if on and not self.video_path:
            self.lbl_info.setText('No video bound — player mode needs a '
                                  'matched video.')
            self.chk_player.setChecked(False)
            return
        if on:
            self._ensure_player()
            self._stack.setCurrentWidget(self.player_view)
            self.btn_play.setEnabled(True)
            self.slider.setEnabled(True)
            if self.cue is not None:
                self._player.setPosition(int(self.cue.begin_ms) + 10)
                self._player.pause()
            self._update_overlays(self._player.position() if self._player
                                  else 0, force=True)
        else:
            if self._player is not None:
                self._player.pause()
            self._play_timer.stop()
            self._stack.setCurrentWidget(self.stage)
            self.btn_play.setEnabled(False)
            self.slider.setEnabled(False)
            self.schedule_render()
        self._sync_play_button()

    def _ensure_player(self):
        if self._player is not None:
            return
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.7)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self.player_view.video_item)
        self._player.positionChanged.connect(self._position_changed)
        self._player.durationChanged.connect(
            lambda d: self.slider.setRange(0, int(d)))
        self._player.mediaStatusChanged.connect(self._media_status)
        self._player.playbackStateChanged.connect(
            lambda *_: self._sync_play_button())
        self._load_player_source()

    def _load_player_source(self):
        if self._player is None or not self.video_path:
            return
        self._player_failed = False
        self._player.setSource(QUrl.fromLocalFile(self.video_path))

    def _media_status(self, status):
        if not MULTIMEDIA_AVAILABLE:
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._player_failed = True
            self.lbl_info.setText(
                'Embedded player cannot decode this file (missing platform '
                'codec) — using stills mode. External player still works.')
            self.chk_player.setChecked(False)

    def _toggle_play(self):
        if not self._player_active():
            return
        if self._player.playbackState() == \
                QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._play_timer.stop()
        else:
            self._player.play()
            self._play_timer.start()
        self._sync_play_button()

    def _sync_play_button(self):
        if MULTIMEDIA_AVAILABLE and self._player is not None and \
                self._player.playbackState() == \
                QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play.setText('⏸')
        else:
            self.btn_play.setText('▶')

    def _slider_seek(self, pos: int):
        if self._player_active():
            self._player.setPosition(pos)
            self._update_overlays(pos, force=True)

    def _mute_changed(self, muted: bool):
        if self._audio is not None:
            self._audio.setMuted(muted)

    def _position_changed(self, pos: int):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(pos))
            self.slider.blockSignals(False)
        self.lbl_time.setText(_fmt_ms(pos))

    def _tick(self):
        if self._player_active():
            self._update_overlays(self._player.position())

    # -- overlay sync ---------------------------------------------------- #
    def _active_cues_at(self, ms: float) -> List[Cue]:
        out = []
        idx = bisect.bisect_right(self._cue_starts, ms)
        for c in self._sorted_cues[:idx]:
            if c.enabled and c.begin_ms <= ms < c.end_ms:
                out.append(c)
        return out

    _last_overlay_key: tuple = ()

    def _update_overlays(self, ms: float, force: bool = False):
        active = self._active_cues_at(ms)
        key = tuple(c.uid for c in active)
        if key == self._last_overlay_key and not force:
            return
        renders = []
        missing = False
        ctx = self._ctx()
        for c in active:
            if c.uid in self._player_cache:
                rc = self._player_cache[c.uid]
                if rc is not None:
                    renders.append(rc)
            else:
                missing = True
                if ctx is not None and c.uid not in self._pending_cues:
                    self._pending_cues.add(c.uid)
                    self.worker.request_cue(ctx, c)
        if not missing:
            self._last_overlay_key = key
        self.player_view.set_overlays(renders)

    def _on_cue_render(self, uid: int, rc, generation: int):
        if generation != self._generation:
            return
        self._player_cache[uid] = rc
        self._pending_cues.discard(uid)
        if self._player_active():
            self._update_overlays(self._player.position(), force=True)

    # ------------------------------------------------------------------ #
    # Matte / bg / popout / external player
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
            # per design: the 1:1 pop-out pauses the embedded player
            if self._player_active():
                self._player.pause()
                self._play_timer.stop()
                self._sync_play_button()
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
            # ensure the stills scene is fresh for the popout
            self._debounce.start()
        else:
            self.popout.close()

    def _popout_closed(self):
        self.popout = None
        self.btn_popout.setText('Pop out (1:1)')

    def _open_player_menu(self):
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
            subprocess.Popen([exe] + shlex.split(args,
                                                 posix=(os.name != 'nt')))
        except OSError as e:
            self.lbl_info.setText(f'Player launch failed: {e}')
