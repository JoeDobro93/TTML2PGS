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
from .mpv_player import MpvPlayerWidget, mpv_available

# Embedded playback is optional — degrade to stills when QtMultimedia or
# its platform backend is unavailable. mpv (libmpv), when installed, is
# preferred: it tone-maps HDR correctly and decodes far more.
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
    #: (rid, '#rrggbb', x, y, w, h, label_corner) per region — only
    #: populated when "Show regions" is on
    region_boxes: Optional[List[tuple]] = None
    #: safe-area padding inset per edge (px, canvas space); (0,0) = off
    pad: Tuple[float, float] = (0.0, 0.0)


@dataclass
class _RenderContext:
    doc: SubtitleDocument
    overrides: OverrideSet
    video_res: Optional[Tuple[int, int]]
    video_path: Optional[str]
    is_hdr: bool = False
    generation: int = 0
    show_regions: bool = False


def _region_overlay_colors(region_ids: List[str]) -> Dict[str, str]:
    """
    v1's region-overlay palette: hues spread evenly across the wheel,
    shuffled with a fixed seed + jitter (stable across renders), clamped
    to a bright legible band.
    """
    import colorsys
    import random
    ids = sorted(region_ids)
    n = max(1, len(ids))
    rng = random.Random(0xC0FFEE)
    hues = [((i + rng.uniform(-0.35, 0.35)) % n) / n for i in range(n)]
    rng.shuffle(hues)
    colors = {}
    for i, rid in enumerate(ids):
        h = hues[i] % 1.0
        s = 0.70 + rng.uniform(0.0, 0.18)
        light = 0.60 + rng.uniform(0.0, 0.08)
        r, g, b = colorsys.hls_to_rgb(h, light, s)
        colors[rid] = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    return colors


def compute_region_boxes(doc: SubtitleDocument,
                         renderer: CueRenderer) -> List[tuple]:
    """
    Canvas-absolute outline boxes for every region ("Show regions").

    Shrink-wrap regions (no width/height) would collapse to a point, so
    the missing main flow axis is filled to 100% and the cross axis
    becomes a visible band — same trick as v1. Regions resolving to the
    exact same box get nudged a pixel so both outlines stay visible.
    """
    from dataclasses import replace as dc_replace

    from ...core.units import Dim

    colors = _region_overlay_colors(list(doc.regions.keys()))
    boxes: List[tuple] = []
    seen: Dict[tuple, int] = {}
    BAND = 12.0

    # per-language safe-area padding applies by the language of the
    # CUES using each region (merged docs mix languages) — outlines
    # must move exactly like the text does
    region_lang: Dict[str, str] = {}
    for cue in doc.cues:
        if cue.region_id and cue.region_id not in region_lang:
            region_lang[cue.region_id] = cue.lang or doc.language

    for rid in sorted(doc.regions.keys()):
        region = doc.regions[rid]
        spec = doc.specified_style(region.style_refs, region.style)
        vertical = bool(spec.writing_mode) and \
            spec.writing_mode.startswith('tb')
        w, h = region.width, region.height
        if vertical:
            if h is None:
                h = Dim(100.0, '%')
            if w is None:
                w = Dim(BAND, '%')
        else:
            if w is None:
                w = Dim(100.0, '%')
            if h is None:
                h = Dim(BAND, '%')
        ov = dc_replace(region, width=w, height=h)

        rect = renderer._region_rect(
            ov, renderer.overrides.for_language(
                region_lang.get(rid, doc.language)))
        rw = rect['w'] if rect['w'] is not None else 0.0
        rh = rect['h'] if rect['h'] is not None else 0.0
        x, y = renderer._anchor_pos(rect, rw, rh)

        # clamp to the canvas: point-anchored shrink-wrap regions filled
        # to 100% can spill far off-screen (v1's HTML just clipped them)
        x0 = max(0.0, x)
        y0 = max(0.0, y)
        x1 = min(float(renderer.canvas.width), x + rw)
        y1 = min(float(renderer.canvas.height), y + rh)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        x, y, rw, rh = x0, y0, x1 - x0, y1 - y0

        sig = (round(x), round(y), round(rw), round(rh))
        nudge = seen.get(sig, 0)
        seen[sig] = nudge + 1
        x, y = x + nudge, y + nudge

        # label goes to the corner opposite the text, like v1
        da = spec.display_align or 'after'
        ta = spec.text_align or ('start' if vertical else 'center')
        v_side = 'bottom' if da in ('before',) else 'top'
        h_side = 'right' if ta in ('left', 'start') else 'left'
        boxes.append((rid, colors.get(rid, '#ff5050'),
                      float(x), float(y), float(rw), float(rh),
                      f'{v_side}-{h_side}'))
    return boxes


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
        # padding guides follow the ACTIVE language: a ja file previews
        # the ja padding when a ja set exists, else the Default set
        so = ctx.overrides.for_language(ctx.doc.language)
        pad_x, pad_y = canvas.pad_x, canvas.pad_y
        if so.use_padding:
            pad_x += canvas.content_w * (so.padding_h / 100.0) / 2.0
            pad_y += canvas.content_h * (so.padding_v / 100.0) / 2.0
        scene = PreviewScene(canvas_w=canvas.width, canvas_h=canvas.height,
                             content=canvas.content, renders=[],
                             pad=(pad_x, pad_y))
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
        if ctx.show_regions:
            try:
                scene.region_boxes = compute_region_boxes(ctx.doc, renderer)
            except Exception:                          # pragma: no cover
                scene.region_boxes = []
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
        self.bg_color = QColor('#B0C4DE')     # v1's LightSteelBlue matte
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

        # safe-area padding guides (where regions are allowed to anchor)
        px_, py_ = self.scene.pad
        if px_ > 0 or py_ > 0:
            pen = p.pen()
            pen.setColor(QColor(120, 220, 130, 180))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(int(ox + (cx + px_) * scale),
                       int(oy + (cy + py_) * scale),
                       int((cw2 - 2 * px_) * scale),
                       int((ch2 - 2 * py_) * scale))

        for x, y, pm in self._cue_pixmaps:
            p.drawPixmap(int(ox + x * scale), int(oy + y * scale),
                         int(pm.width() * scale), int(pm.height() * scale),
                         pm)

        for box in (self.scene.region_boxes or []):
            rid, hexc, bx, by, bw, bh, corner = box
            color = QColor(hexc)
            pen = p.pen()
            pen.setColor(color)
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.SolidLine)
            p.setPen(pen)
            rx, ry = ox + bx * scale, oy + by * scale
            rw, rh = bw * scale, bh * scale
            p.drawRect(int(rx), int(ry), int(rw), int(rh))
            f = p.font()
            f.setBold(True)
            f.setPixelSize(max(9, int(12 * min(1.5, scale * 3))))
            p.setFont(f)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(rid)
            tx = rx + 4 if corner.endswith('left') else rx + rw - tw - 4
            ty = ry + fm.ascent() + 3 if corner.startswith('top') \
                else ry + rh - fm.descent() - 3
            p.setPen(QColor(0, 0, 0, 220))
            p.drawText(int(tx + 1), int(ty + 1), rid)
            p.setPen(color)
            p.drawText(int(tx), int(ty), rid)
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
        self.setInteractive(False)
        # canvas-shaped black backdrop: with a video of a different AR the
        # letterbox/pillarbox bars show exactly like they would on a TV
        from PyQt6.QtGui import QBrush, QPen
        from PyQt6.QtWidgets import QGraphicsRectItem
        self._backdrop = QGraphicsRectItem(0, 0, 1920, 1080)
        self._backdrop.setBrush(QBrush(QColor('#000000')))
        self._backdrop.setPen(QPen(Qt.PenStyle.NoPen))
        self._backdrop.setZValue(-2)
        self.scene().addItem(self._backdrop)
        self.video_item = None
        if MULTIMEDIA_AVAILABLE:
            self.video_item = QGraphicsVideoItem()
            self.video_item.setAspectRatioMode(
                Qt.AspectRatioMode.KeepAspectRatio)
            self.scene().addItem(self.video_item)
        self._overlay_items: List[QGraphicsPixmapItem] = []
        self._region_items: List = []
        self._pad_item = None
        self._canvas = (1920, 1080)

    def set_canvas(self, w: int, h: int):
        self._canvas = (w, h)
        self.scene().setSceneRect(QRectF(0, 0, w, h))
        self._backdrop.setRect(0, 0, w, h)
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

    def set_padding_guide(self, rect: Optional[Tuple[float, float,
                                                     float, float]]):
        from PyQt6.QtGui import QPen
        from PyQt6.QtWidgets import QGraphicsRectItem
        if self._pad_item is not None:
            self.scene().removeItem(self._pad_item)
            self._pad_item = None
        if rect is None:
            return
        item = QGraphicsRectItem(*rect)
        pen = QPen(QColor(120, 220, 130, 200))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(2)
        item.setPen(pen)
        item.setZValue(19)
        self.scene().addItem(item)
        self._pad_item = item

    def set_region_boxes(self, boxes: Optional[List[tuple]]):
        from PyQt6.QtGui import QFont, QPen
        from PyQt6.QtWidgets import (QGraphicsRectItem,
                                     QGraphicsSimpleTextItem)
        for it in self._region_items:
            self.scene().removeItem(it)
        self._region_items.clear()
        for rid, hexc, bx, by, bw, bh, corner in (boxes or []):
            color = QColor(hexc)
            rect = QGraphicsRectItem(bx, by, bw, bh)
            pen = QPen(color)
            pen.setWidth(3)
            pen.setCosmetic(False)
            rect.setPen(pen)
            rect.setZValue(20)
            self.scene().addItem(rect)
            self._region_items.append(rect)
            label = QGraphicsSimpleTextItem(rid)
            f = QFont()
            f.setBold(True)
            f.setPixelSize(26)
            label.setFont(f)
            label.setBrush(color)
            br = label.boundingRect()
            lx = bx + 6 if corner.endswith('left') else bx + bw - br.width() - 6
            ly = by + 4 if corner.startswith('top') else by + bh - br.height() - 4
            label.setPos(lx, ly)
            label.setZValue(21)
            self.scene().addItem(label)
            self._region_items.append(label)

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
        # content never needs mouse input inside the pop-out — letting
        # events fall through keeps window dragging + the right-click
        # close menu working over the whole surface.
        self.stage.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stage)
        self._external: Optional[QWidget] = None
        self._drag = QPoint()

    def set_content(self, widget: QWidget):
        """Host a borrowed widget (the live player view) instead of the
        stills stage. release_content() gives it back."""
        self.stage.hide()
        self.layout().addWidget(widget)
        widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        widget.show()
        self._external = widget

    def release_content(self) -> Optional[QWidget]:
        w = self._external
        if w is not None:
            self.layout().removeWidget(w)
            w.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            w.setParent(None)
            self._external = None
        self.stage.show()
        return w

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

#: typical install locations probed when the executable isn't on PATH
_PLAYER_SEARCH_SUBPATHS = {
    'mpc-be64.exe': [r'MPC-BE\mpc-be64.exe', r'MPC-BE x64\mpc-be64.exe'],
    'mpc-hc64.exe': [r'MPC-HC\mpc-hc64.exe',
                     r'K-Lite Codec Pack\MPC-HC64\mpc-hc64.exe'],
    'vlc': [r'VideoLAN\VLC\vlc.exe'],
    'mpv': [r'mpv\mpv.exe'],
}


def resolve_player_exe(exe: str) -> Optional[str]:
    """Find the actual executable: absolute path, PATH lookup, then the
    usual Windows install folders."""
    import shutil
    if not exe:
        return None
    if os.path.isabs(exe):
        return exe if os.path.exists(exe) else None
    hit = shutil.which(exe)
    if hit:
        return hit
    roots = [os.environ.get('ProgramFiles'),
             os.environ.get('ProgramFiles(x86)'),
             os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs')]
    for sub in _PLAYER_SEARCH_SUBPATHS.get(exe.lower(), []):
        for root in roots:
            if root:
                cand = os.path.join(root, sub)
                if os.path.exists(cand):
                    return cand
    return None


def build_player_command(exe_path: str, args_template: str, file: str,
                         ms: int) -> List[str]:
    """
    Tokenize the *template* (not the substituted string) and fill each
    token — the file path never passes through shlex, so Windows paths
    with spaces/backslashes survive intact.
    """
    values = {'file': file, 'ms': ms, 'sec': f"{ms / 1000.0:.3f}",
              'time': _fmt_ms(ms)}
    toks = shlex.split(args_template or '"{file}"')
    return [exe_path] + [t.format(**values) for t in toks]


def _fmt_ms(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _region_overlay_bitmap(scene: 'PreviewScene'):
    """Rasterize the region boxes to one RGBA tile for the mpv overlay
    path (mpv draws pixel overlays, not scene items)."""
    boxes = scene.region_boxes or []
    if not boxes:
        return None
    from PIL import Image, ImageDraw, ImageFont
    pad = 4
    x0 = max(0, int(min(b[2] for b in boxes)) - pad)
    y0 = max(0, int(min(b[3] for b in boxes)) - pad)
    x1 = min(scene.canvas_w, int(max(b[2] + b[4] for b in boxes)) + pad)
    y1 = min(scene.canvas_h, int(max(b[3] + b[5] for b in boxes)) + pad)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    img = Image.new('RGBA', (x1 - x0, y1 - y0), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(24)
    except TypeError:                                  # Pillow < 10.1
        font = ImageFont.load_default()
    for rid, hexc, bx, by, bw, bh, corner in boxes:
        rx, ry = bx - x0, by - y0
        d.rectangle([rx, ry, rx + bw - 1, ry + bh - 1],
                    outline=hexc, width=3)
        tb = d.textbbox((0, 0), rid, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        tx = rx + 6 if corner.endswith('left') else rx + bw - tw - 6
        ty = ry + 4 if corner.startswith('top') else ry + bh - th - 8
        d.text((tx + 1, ty + 1), rid, fill=(0, 0, 0, 230), font=font)
        d.text((tx, ty), rid, fill=hexc, font=font)
    import numpy as _np
    return ('__regions__', x0, y0, _np.asarray(img).copy())


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
        self._mpv: Optional[MpvPlayerWidget] = None
        self._mpv_failed = False
        self._backend = ''                  # '' | 'mpv' | 'qt'
        self._mpv_region_overlay = None     # (x, y, bitmap) for mpv mode

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
            '(SubtitleEdit-style). Uses the mpv engine when libmpv is '
            'installed (correct HDR tone mapping, wide codec support), '
            'falling back to Qt Multimedia otherwise.')
        self.chk_player.setEnabled(MULTIMEDIA_AVAILABLE or mpv_available())
        bar.addWidget(self.chk_player)
        bar.addWidget(QLabel('AR:'))
        self.spin_ar_w = QDoubleSpinBox()
        self.spin_ar_w.setRange(0.1, 10000)
        self.spin_ar_w.setValue(16.0)
        self.spin_ar_w.setDecimals(3)
        self.spin_ar_h = QDoubleSpinBox()
        self.spin_ar_h.setRange(0.1, 10000)
        self.spin_ar_h.setValue(9.0)
        self.spin_ar_h.setDecimals(3)
        for sp in (self.spin_ar_w, self.spin_ar_h):
            sp.setMinimumWidth(52)
            sp.setSizePolicy(QSizePolicy.Policy.Preferred,
                             QSizePolicy.Policy.Fixed)
        self.chk_matte = QCheckBox('Matte')
        self.chk_matte.setChecked(True)
        self.btn_bg = QPushButton('BG')
        self.btn_bg.setFixedWidth(34)
        self.chk_frames = QCheckBox('Frames')
        self.chk_frames.setToolTip('Extract video frames behind stills')
        self.chk_tonemap = QCheckBox('Tone-map')
        self.chk_tonemap.setToolTip('Tone-map HDR sources for the still '
                                    'frame extraction')
        self.chk_regions = QCheckBox('Regions')
        self.chk_regions.setToolTip(
            'Outline every region with its name, each in a distinct '
            'color — works in stills and player mode. Useful for '
            'checking layout.')
        for w in (self.spin_ar_w, QLabel(':'), self.spin_ar_h,
                  self.chk_matte, self.btn_bg):
            bar.addWidget(w)
        bar.addStretch()
        # second toolbar row keeps the pane's minimum width low
        bar2 = QHBoxLayout()
        for w in (self.chk_frames, self.chk_tonemap, self.chk_regions):
            bar2.addWidget(w)
        bar2.addStretch()
        self.btn_popout = QPushButton('Pop out (1:1)')
        self.btn_popout.setToolTip(
            'Float the preview at the output pixel size. In player mode '
            'the LIVE player moves into the pop-out (video plays only '
            'there; the transport bar here still controls it). In stills '
            'mode it mirrors the still preview. Right-click it to close.')
        self.btn_player = QPushButton('Open in player ▸')
        self.btn_player.setToolTip(
            'Open the bound video in your own desktop player (MPC-BE, '
            'VLC, mpv…) seeked to the selected cue — for checking '
            'subtitle sync against real playback. Subtitles are NOT '
            'overlaid there; use the embedded player or pop-out for '
            'overlays.')
        bar2.addWidget(self.btn_popout)
        bar2.addWidget(self.btn_player)
        lay.addLayout(bar)
        lay.addLayout(bar2)

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
        self.chk_frames.toggled.connect(self._frames_toggled)
        self.chk_tonemap.toggled.connect(lambda *_: self.schedule_render())
        self.chk_regions.toggled.connect(lambda *_: self.schedule_render())
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
        bg = app_settings.get('preview_bg')
        if bg:
            self.stage.bg_color = QColor(bg)
            if self.popout:
                self.popout.stage.bg_color = QColor(bg)
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
            self._mpv_failed = False
            if self._player is not None:
                self._load_player_source()
            if self._mpv is not None and self._mpv.ok() and video_path:
                self._mpv.load(video_path)
        self.schedule_render()

    def clear_context(self):
        """All files closed: blank the preview completely — no stale
        frame, no playable video, no region boxes."""
        self.doc = None
        self.cue = None
        self.video_path = None
        self.video_res = None
        self._invalidate_renders()
        self._rebuild_cue_index()
        if self.chk_player.isChecked():
            self.chk_player.setChecked(False)     # back to stills mode
        if self._mpv is not None:
            self._mpv.unload()
        if self._player is not None:
            try:
                self._player.stop()
                self._player.setSource(QUrl())
            except Exception:
                pass
        for stage in filter(None, [self.stage,
                                   self.popout.stage if self.popout
                                   else None]):
            stage.scene = None
            stage._frame_pix = None
            stage._cue_pixmaps = []
            stage.update()
        self.player_view.set_region_boxes([])
        self.player_view.set_padding_guide(None)
        self.lbl_info.setText('')

    def _ctx(self) -> Optional[_RenderContext]:
        if self.doc is None:
            return None
        return _RenderContext(self.doc, self.overrides, self.video_res,
                              self.video_path, self.is_hdr,
                              self._generation,
                              show_regions=self.chk_regions.isChecked())

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
            self._pl_seek(cue.begin_ms + 10)
            self._pl_pause(True)
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
            self._update_overlays(self._pl_position(), force=True)
        # frame extraction strictly follows the 'Frames' toggle — for the
        # stills view and for a stills pop-out alike
        want_frame = bool(self.video_path) and \
            self.chk_frames.isChecked() and \
            (not self._player_active() or
             (self.popout is not None and not self._popped_player))
        self.worker.request_scene(ctx, self.cue, want_frame,
                                  self.chk_tonemap.isChecked())

    def _on_scene(self, scene: PreviewScene):
        self.stage.set_scene(scene)
        self.player_view.set_canvas(scene.canvas_w, scene.canvas_h)
        self.player_view.set_region_boxes(scene.region_boxes)
        px_, py_ = scene.pad
        if px_ > 0 or py_ > 0:
            cx, cy, cw, ch = scene.content
            self.player_view.set_padding_guide(
                (cx + px_, cy + py_, cw - 2 * px_, ch - 2 * py_))
        else:
            self.player_view.set_padding_guide(None)
        if self._mpv is not None:
            self._mpv.set_canvas(scene.canvas_w, scene.canvas_h,
                                 scene.content)
        self._mpv_region_overlay = _region_overlay_bitmap(scene) \
            if scene.region_boxes else None
        if self._backend == 'mpv' and self._player_active():
            self._update_overlays(self._pl_position(), force=True)
        n = len(scene.renders or [])
        mode = ('player·mpv' if self._backend == 'mpv' else 'player') \
            if self._player_active() else 'stills'
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
        if not self.chk_player.isChecked():
            return False
        if self._backend == 'mpv':
            return self._mpv is not None and self._mpv.ok()
        return (MULTIMEDIA_AVAILABLE and self._player is not None
                and not self._player_failed)

    def _want_mpv(self) -> bool:
        if self._mpv_failed:
            return False
        if self.app_settings.get('player_engine', 'auto') == 'qt':
            return False
        return mpv_available([self.app_settings.get('mpv_dll_dir', '')])

    def _player_mode_changed(self, on: bool):
        if self._popped_player and self.popout is not None:
            self.popout.close()          # reclaims the player first
        if on and not self.video_path:
            self.lbl_info.setText('No video bound — player mode needs a '
                                  'matched video.')
            self.chk_player.setChecked(False)
            return
        if on and self.chk_frames.isChecked():
            # player and frame extraction are mutually exclusive views
            self.chk_frames.blockSignals(True)
            self.chk_frames.setChecked(False)
            self.chk_frames.blockSignals(False)
        if on:
            if self._want_mpv() and self._ensure_mpv():
                self._backend = 'mpv'
                self._stack.setCurrentWidget(self._mpv)
                # reload if the file was released (mux) or has changed
                if self.video_path and \
                        self._mpv.loaded_path != self.video_path:
                    self._mpv.load(self.video_path)
            elif MULTIMEDIA_AVAILABLE:
                self._backend = 'qt'
                self._ensure_player()
                self._stack.setCurrentWidget(self.player_view)
            else:
                self.lbl_info.setText('No playback engine available — '
                                      'install mpv (libmpv) for embedded '
                                      'playback.')
                self.chk_player.setChecked(False)
                return
            self.btn_play.setEnabled(True)
            self.slider.setEnabled(True)
            # jump straight to the selected cue's frame, subtitle shown
            at = (self.cue.begin_ms + 10) if self.cue is not None else 0.0
            self._pl_seek(at)
            self._pl_pause(True)
            self._kickstart_qt(at)
            self._update_overlays(at, force=True)
            self.schedule_render()       # pushes overlays once rendered
        else:
            self._pl_pause(True)
            self._play_timer.stop()
            self._stack.setCurrentWidget(self.stage)
            self.btn_play.setEnabled(False)
            self.slider.setEnabled(False)
            self._backend = ''
            self.schedule_render()
        self._sync_play_button()

    def _kickstart_qt(self, ms: float):
        """QMediaPlayer stays BLACK until playback has started once.
        play() issued before the (async) media load completes is
        silently dropped — so remember the request and fire it from
        mediaStatusChanged once the media is actually loaded."""
        if self._backend != 'qt' or self._player is None:
            return
        self._pending_kick_ms = ms
        st = self._player.mediaStatus()
        if st in (QMediaPlayer.MediaStatus.LoadedMedia,
                  QMediaPlayer.MediaStatus.BufferedMedia,
                  QMediaPlayer.MediaStatus.BufferingMedia):
            self._do_kickstart()

    def _do_kickstart(self):
        ms = getattr(self, '_pending_kick_ms', None)
        if ms is None or self._player is None:
            return
        self._pending_kick_ms = None
        self._player.setPosition(int(ms))
        self._player.play()

        def settle():
            if self._player is not None and self.chk_player.isChecked():
                self._player.pause()
                self._player.setPosition(int(ms))
                self._sync_play_button()
                self._update_overlays(ms, force=True)
        QTimer.singleShot(180, settle)

    def _frames_toggled(self, on: bool):
        if on and self.chk_player.isChecked():
            # mutually exclusive with the embedded player
            self.chk_player.setChecked(False)   # → back to stills mode
        self.schedule_render()

    # -- backend-agnostic transport helpers ----------------------------- #
    def _pl_seek(self, ms: float):
        if self._backend == 'mpv' and self._mpv is not None:
            self._mpv.seek_ms(ms)
        elif self._player is not None:
            self._player.setPosition(int(ms))

    def _pl_pause(self, paused: bool):
        if self._backend == 'mpv' and self._mpv is not None:
            self._mpv.set_pause(paused)
        elif self._player is not None:
            if paused:
                self._player.pause()
            else:
                self._player.play()

    def _pl_position(self) -> float:
        if self._backend == 'mpv' and self._mpv is not None:
            return self._mpv.position_ms()
        return float(self._player.position()) if self._player else 0.0

    def _pl_playing(self) -> bool:
        if self._backend == 'mpv' and self._mpv is not None:
            return not self._mpv.is_paused()
        return bool(MULTIMEDIA_AVAILABLE and self._player is not None and
                    self._player.playbackState() ==
                    QMediaPlayer.PlaybackState.PlayingState)

    # -- mpv backend ----------------------------------------------------- #
    def _wire_mpv(self, w: MpvPlayerWidget):
        w.position_changed.connect(self._mpv_position)
        w.duration_changed.connect(
            lambda d: self.slider.setRange(0, int(d)))
        w.pause_changed.connect(lambda *_: self._sync_play_button())
        w.load_failed.connect(self._mpv_load_failed)

    def _ensure_mpv(self) -> bool:
        if self._mpv is not None and self._mpv.ok():
            return True
        w = MpvPlayerWidget()
        self._stack.addWidget(w)
        if not w.start():
            self._stack.removeWidget(w)
            w.deleteLater()
            self._mpv_failed = True
            return False
        self._wire_mpv(w)
        self._mpv = w
        w.set_mute(self.chk_mute.isChecked())
        if self.stage.scene:
            w.set_canvas(self.stage.scene.canvas_w,
                         self.stage.scene.canvas_h,
                         self.stage.scene.content)
        if self.video_path:
            start = (self.cue.begin_ms / 1000.0) if self.cue else 0.0
            w.load(self.video_path)
            w.seek_ms(start * 1000.0)
        return True

    def _mpv_position(self, ms: float):
        if self._backend != 'mpv':
            return
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(ms))
            self.slider.blockSignals(False)
        self.lbl_time.setText(_fmt_ms(ms))
        self._update_overlays(ms)

    def _mpv_load_failed(self, msg: str):
        self._mpv_failed = True
        if self._mpv is not None:
            self._mpv.shutdown()
        if self.chk_player.isChecked():
            if MULTIMEDIA_AVAILABLE:
                self.lbl_info.setText(
                    f'mpv failed ({msg}) — falling back to Qt Multimedia.')
                self._player_mode_changed(True)
            else:
                self.lbl_info.setText(
                    f'Embedded playback failed: {msg} — stills mode.')
                self.chk_player.setChecked(False)

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
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,
                      QMediaPlayer.MediaStatus.BufferedMedia):
            self._do_kickstart()         # deferred first-frame reveal
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._player_failed = True
            self.lbl_info.setText(
                'Embedded player cannot decode this file (missing platform '
                'codec) — using stills mode. External player still works.')
            self.chk_player.setChecked(False)

    def _toggle_play(self):
        if not self._player_active():
            return
        playing = self._pl_playing()
        self._pl_pause(playing)
        if self._backend == 'qt':
            if playing:
                self._play_timer.stop()
            else:
                self._play_timer.start()
        self._sync_play_button()

    def _sync_play_button(self):
        self.btn_play.setText('⏸' if self._pl_playing() else '▶')

    def _slider_seek(self, pos: int):
        if self._player_active():
            self._pl_seek(pos)
            self._update_overlays(pos, force=True)

    def _mute_changed(self, muted: bool):
        if self._audio is not None:
            self._audio.setMuted(muted)
        if self._mpv is not None:
            self._mpv.set_mute(muted)

    def _position_changed(self, pos: int):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(pos))
            self.slider.blockSignals(False)
        self.lbl_time.setText(_fmt_ms(pos))

    def _tick(self):
        # Qt backend has no per-frame position callback — poll. (mpv's
        # position_changed signal drives overlays instead.)
        if self._player_active() and self._backend == 'qt':
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
        if self._backend == 'mpv' and self._mpv is not None:
            items = [(rc.cue_uid, rc.x, rc.y, rc.bitmap) for rc in renders]
            if self._mpv_region_overlay is not None:
                items.append(self._mpv_region_overlay)
            self._mpv.set_overlays(items)
        else:
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
            self.app_settings['preview_bg'] = c.name()
            if self.popout:
                self.popout.stage.bg_color = c
                self.popout.stage.update()

    _popped_player = False

    def _canvas_size(self) -> Tuple[int, int]:
        if self.stage.scene:
            return self.stage.scene.canvas_w, self.stage.scene.canvas_h
        return self.player_view._canvas

    def close_popout(self):
        """Close the pop-out (if open) so dialogs aren't blocked by it.

        Called before any other window/dialog opens; the closed signal
        reclaims a popped-out player automatically.
        """
        if self.popout is not None:
            self.popout.close()

    def _toggle_popout(self):
        if self.popout is None:
            self.popout = PopOutWindow()
            self.popout.closed.connect(self._popout_closed)
            if self._player_active() and self._backend == 'mpv':
                # mpv is bound to a native window id — it can't be
                # reparented, so respawn the engine inside the pop-out at
                # the same position/pause state.
                self._popped_player = True
                self.popout.lock_size(*self._canvas_size())
                self.popout.show()
                self._respawn_mpv(into_popout=True)
                self._stack.setCurrentWidget(self.stage)
                self.lbl_info.setText(
                    'Playing in the pop-out window (1:1) — transport '
                    'below still controls it.')
            elif self._player_active():
                # Qt backend: the view widget moves over intact
                self._popped_player = True
                self._stack.removeWidget(self.player_view)
                self.popout.set_content(self.player_view)
                self.popout.lock_size(*self._canvas_size())
                self._stack.setCurrentWidget(self.stage)
                self.popout.show()
                self.lbl_info.setText(
                    'Playing in the pop-out window (1:1) — transport '
                    'below still controls it.')
            else:
                if self.stage.scene:
                    self.popout.lock_size(self.stage.scene.canvas_w,
                                          self.stage.scene.canvas_h)
                    self.popout.stage.matte_ar = self.stage.matte_ar
                    self.popout.stage.bg_color = self.stage.bg_color
                    self.popout.stage.set_scene(self.stage.scene)
                self.popout.show()
            self.btn_popout.setText('Close pop-out')
            # keep the stills scene fresh (frame extraction for stills popout)
            self._debounce.start()
        else:
            self.popout.close()

    def _respawn_mpv(self, into_popout: bool):
        """Tear down the mpv engine and bring it back inside the pop-out
        (or the main stack), restoring position and pause state."""
        pos, paused = 0.0, True
        if self._mpv is not None:
            pos = self._mpv.position_ms()
            paused = self._mpv.is_paused()
            old = self._mpv
            old.shutdown()
            if old.parent() is not None:
                if self.popout is not None and \
                        self.popout._external is old:
                    self.popout.release_content()
                else:
                    self._stack.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
            self._mpv = None
        w = MpvPlayerWidget()
        if into_popout and self.popout is not None:
            self.popout.set_content(w)
        else:
            self._stack.addWidget(w)
            self._stack.setCurrentWidget(w)
        if not w.start():
            w.deleteLater()
            self._mpv_failed = True
            self._mpv_load_failed('mpv restart failed')
            return
        self._wire_mpv(w)
        self._mpv = w
        w.set_mute(self.chk_mute.isChecked())
        if self.stage.scene:
            w.set_canvas(self.stage.scene.canvas_w,
                         self.stage.scene.canvas_h,
                         self.stage.scene.content)
        if self.video_path:
            w.load(self.video_path)
            w.seek_ms(pos)
            w.set_pause(paused)
        self._update_overlays(pos, force=True)

    def _popout_closed(self):
        if self._popped_player and self.popout is not None:
            if self._backend == 'mpv':
                self._respawn_mpv(into_popout=False)
            else:
                w = self.popout.release_content()
                if w is not None:
                    self._stack.addWidget(self.player_view)
                if self._player_active():
                    self._stack.setCurrentWidget(self.player_view)
            self._popped_player = False
        self.popout = None
        self.btn_popout.setText('Pop out (1:1)')

    def shutdown_players(self):
        """Called on app close: stop engines cleanly."""
        if self._mpv is not None:
            self._mpv.shutdown()
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    def release_video(self, path: str):
        """Drop any open handle on `path` — its mux is about to replace
        the file (Windows can't delete/rename a file a player holds
        open). Playback falls back to stills; re-enable Embed video
        after the mux to play the updated file."""
        if not path or not self.video_path:
            return
        if os.path.normcase(os.path.abspath(path)) != \
                os.path.normcase(os.path.abspath(self.video_path)):
            return
        if self._mpv is not None:
            self._mpv.unload()
        if self._player is not None:
            try:
                self._player.stop()
                self._player.setSource(QUrl())      # releases the handle
            except Exception:
                pass
        if self.chk_player.isChecked():
            self.chk_player.setChecked(False)       # → stills mode
            self.lbl_info.setText(
                'Player released the video for remuxing — re-enable '
                'Embed video when the mux finishes.')

    def _open_player_menu(self):
        menu = QMenu(self)
        act_here = menu.addAction('Open video at selected cue')
        act_here.setToolTip(
            'Launches your desktop player on the bound video, seeked to '
            'this cue — for judging subtitle/video sync in real playback.')
        menu.addSeparator()
        preset_actions = {}
        for name in PLAYER_PRESETS:
            preset_actions[menu.addAction(f'Use preset: {name}')] = name
        menu.addSeparator()
        act_pick = menu.addAction('Pick player executable…')
        chosen = menu.exec(self.btn_player.mapToGlobal(
            QPoint(0, self.btn_player.height())))
        if chosen is None:
            return
        if chosen in preset_actions:
            exe, args = PLAYER_PRESETS[preset_actions[chosen]]
            self.app_settings['external_player'] = exe
            self.app_settings['external_player_args'] = args
            found = resolve_player_exe(exe)
            if found:
                self.app_settings['external_player'] = found
                self.lbl_info.setText(f'External player set: {found}')
            else:
                self.lbl_info.setText(
                    f'Preset saved, but {exe} was not found on this '
                    f'system — use "Pick player executable…" to point at '
                    f'the install.')
            return
        if chosen == act_pick:
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, 'Pick the media player executable', '',
                'Programs (*.exe);;All files (*)' if os.name == 'nt'
                else 'All files (*)')
            if path:
                self.app_settings['external_player'] = path
                self.lbl_info.setText(f'External player set: {path}')
            return
        if chosen == act_here:
            self._launch_player()

    def _launch_player(self):
        from PyQt6.QtWidgets import QMessageBox
        if not self.video_path:
            QMessageBox.information(
                self, 'External player',
                'No video is bound to this subtitle — match one in the '
                'Sources pane first.')
            return
        exe = self.app_settings.get('external_player', '')
        args_tpl = self.app_settings.get('external_player_args',
                                         '"{file}" /start {ms}')
        if not exe:
            self.lbl_info.setText(
                'No player configured — pick a preset from this menu '
                'first.')
            self._open_player_menu()
            return
        exe_path = resolve_player_exe(exe)
        if exe_path is None:
            QMessageBox.warning(
                self, 'External player',
                f'Could not find "{exe}".\n\nUse "Pick player '
                f'executable…" in the player menu to point at your '
                f'install.')
            return
        ms = int(self.cue.begin_ms) if self.cue else 0
        cmd = build_player_command(exe_path, args_tpl, self.video_path, ms)
        try:
            subprocess.Popen(cmd)
            self.lbl_info.setText(
                f'Opened {os.path.basename(exe_path)} at {_fmt_ms(ms)}.')
        except OSError as e:
            QMessageBox.warning(self, 'External player',
                                f'Launch failed:\n{e}\n\n'
                                f'Command: {" ".join(cmd)}')
