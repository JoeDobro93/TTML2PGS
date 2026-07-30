"""
libmpv-based embedded player backend.

QtMultimedia's FFmpeg backend does no HDR tone mapping, so HDR10/DV
sources render with purple/green casts. mpv is the engine SubtitleEdit
& co. embed for exactly this reason: proper tone mapping, robust codec
support, and an overlay API. This widget embeds libmpv via ``wid`` and:

* draws the rendered subtitle cues with ``overlay-add`` (BGRA,
  premultiplied), mapped from canvas space to mpv's on-screen video
  rect via the ``osd-dimensions`` property (rescaled on every resize);
* reports time/duration/pause through Qt signals (mpv's event thread →
  queued signal delivery);
* tone-maps HDR to SDR out of the box (bt.2390).

Requires the ``python-mpv`` package plus the libmpv shared library
(``libmpv.so.2`` from your distro, or ``libmpv-2.dll`` next to the app /
in the configured folder on Windows). When either is missing,
``mpv_available()`` is False and the preview falls back to QtMultimedia.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

_import_tried = False
_import_ok = False


def _try_import(extra_dirs: Optional[List[str]] = None) -> bool:
    global _import_tried, _import_ok
    if _import_ok:
        return True
    for d in (extra_dirs or []):
        if d and os.path.isdir(d):
            os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
            if hasattr(os, 'add_dll_directory') and sys.platform == 'win32':
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass
    try:
        import mpv                                    # noqa: F401
        _import_ok = True
    except Exception:
        _import_ok = False
    _import_tried = True
    return _import_ok


def mpv_available(extra_dirs: Optional[List[str]] = None) -> bool:
    """True when python-mpv + libmpv can actually be loaded."""
    return _try_import(extra_dirs)


#: overlay item: (stable key, canvas x, canvas y, HxWx4 straight RGBA)
OverlayItem = Tuple[object, int, int, np.ndarray]


class MpvPlayerWidget(QWidget):
    """Embedded mpv surface with canvas-space subtitle overlays."""

    position_changed = pyqtSignal(float)      # ms
    duration_changed = pyqtSignal(float)      # ms
    pause_changed = pyqtSignal(bool)
    load_failed = pyqtSignal(str)
    _osd_resized = pyqtSignal()               # internal, thread hop

    def __init__(self, parent=None):
        super().__init__(parent)
        # mpv renders into this widget's native window
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setStyleSheet('background-color:#000;')
        self.setMinimumSize(160, 90)
        self._m = None
        self._canvas = (1920, 1080)
        self._content = (0.0, 0.0, 1920.0, 1080.0)
        self._items: List[OverlayItem] = []
        self._alive: Dict[int, np.ndarray] = {}     # id -> buffer keepalive
        self._scaled: Dict[tuple, np.ndarray] = {}  # (key,w,h) -> bgra
        self._last_ids: List[int] = []
        self._duration_ms = 0.0
        self._got_media = False
        self._osd_resized.connect(self._apply_overlays)
        self._load_check = QTimer(self)
        self._load_check.setSingleShot(True)
        self._load_check.setInterval(6000)
        self._load_check.timeout.connect(self._check_loaded)

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self._m is not None:
            return True
        if not _try_import():
            return False
        import mpv
        try:
            self._m = mpv.MPV(
                wid=str(int(self.winId())),
                vo='gpu', hwdec='auto-safe',
                keep_open='yes', pause=True, mute=False,
                osc=False, input_default_bindings=False,
                input_vo_keyboard=False, input_cursor=False,
                tone_mapping='bt.2390')
        except Exception as e:
            self._m = None
            self.load_failed.emit(f'mpv init failed: {e}')
            return False
        self._m.observe_property('time-pos', self._on_time)
        self._m.observe_property('duration', self._on_duration)
        self._m.observe_property('pause', self._on_pause)
        self._m.observe_property('osd-dimensions', self._on_osd)
        return True

    def shutdown(self):
        if self._m is not None:
            try:
                self._m.terminate()
            except Exception:
                pass
            self._m = None
        self._alive.clear()
        self._scaled.clear()

    def ok(self) -> bool:
        return self._m is not None

    # -- mpv event-thread callbacks (emit queued signals only) ---------- #
    def _on_time(self, _name, value):
        if value is not None:
            self.position_changed.emit(float(value) * 1000.0)

    def _on_duration(self, _name, value):
        if value:
            self._duration_ms = float(value) * 1000.0
            self._got_media = True
            self.duration_changed.emit(self._duration_ms)

    def _on_pause(self, _name, value):
        if value is not None:
            self.pause_changed.emit(bool(value))

    def _on_osd(self, _name, _value):
        self._osd_resized.emit()

    # ------------------------------------------------------------------ #
    def load(self, path: str):
        if self._m is None:
            return
        self._got_media = False
        try:
            self._m.loadfile(path)
            self._m.pause = True
        except Exception as e:
            self.load_failed.emit(str(e))
            return
        self._load_check.start()

    def _check_loaded(self):
        if not self._got_media and self._m is not None:
            self.load_failed.emit('mpv could not open this file')

    def unload(self):
        """Drop the current file, releasing its OS handle (e.g. so a
        remux can replace the video). The player stays alive."""
        self._load_check.stop()
        self._got_media = False
        if self._m is not None:
            try:
                self._m.command('stop')
            except Exception:
                pass

    # -- transport ------------------------------------------------------ #
    def set_pause(self, paused: bool):
        if self._m is not None:
            try:
                self._m.pause = paused
            except Exception:
                pass

    def is_paused(self) -> bool:
        try:
            return bool(self._m.pause) if self._m is not None else True
        except Exception:
            return True

    def seek_ms(self, ms: float):
        if self._m is None:
            return
        try:
            self._m.command('seek', ms / 1000.0, 'absolute+exact')
        except Exception:
            pass

    def position_ms(self) -> float:
        try:
            t = self._m.time_pos if self._m is not None else None
            return float(t) * 1000.0 if t is not None else 0.0
        except Exception:
            return 0.0

    def duration_ms(self) -> float:
        return self._duration_ms

    def set_mute(self, muted: bool):
        if self._m is not None:
            try:
                self._m.mute = muted
            except Exception:
                pass

    def set_volume(self, vol: float):
        if self._m is not None:
            try:
                self._m.volume = max(0.0, min(130.0, vol * 100.0))
            except Exception:
                pass

    # -- overlays -------------------------------------------------------- #
    def set_canvas(self, w: int, h: int,
                   content: Tuple[float, float, float, float]):
        self._canvas = (w, h)
        self._content = content
        self._scaled.clear()
        self._apply_overlays()

    def set_overlays(self, items: List[OverlayItem]):
        self._items = items
        self._apply_overlays()

    def _video_rect(self) -> Optional[Tuple[float, float, float, float]]:
        try:
            dims = self._m.osd_dimensions if self._m is not None else None
        except Exception:
            dims = None
        if not dims:
            return None
        try:
            ml, mt = float(dims['ml']), float(dims['mt'])
            vw = float(dims['w']) - ml - float(dims['mr'])
            vh = float(dims['h']) - mt - float(dims['mb'])
        except (KeyError, TypeError):
            return None
        if vw <= 1 or vh <= 1:
            return None
        return ml, mt, vw, vh

    def _apply_overlays(self):
        if self._m is None:
            return
        rect = self._video_rect()
        used: List[int] = []
        if rect is not None:
            ml, mt, vw, vh = rect
            cx, cy, cw, ch = self._content
            scale = vw / cw if cw else 1.0
            oid = 1
            for key, x, y, bitmap in self._items:
                sw = max(1, int(round(bitmap.shape[1] * scale)))
                sh = max(1, int(round(bitmap.shape[0] * scale)))
                ck = (key, sw, sh)
                arr = self._scaled.get(ck)
                if arr is None:
                    arr = _to_bgra_scaled(bitmap, sw, sh)
                    if len(self._scaled) > 64:
                        self._scaled.clear()
                    self._scaled[ck] = arr
                ox = int(round(ml + (x - cx) * scale))
                oy = int(round(mt + (y - cy) * scale))
                try:
                    self._m.overlay_add(
                        oid, ox, oy, '&' + str(arr.ctypes.data), 0,
                        'bgra', sw, sh, sw * 4)
                except Exception:
                    break
                self._alive[oid] = arr
                used.append(oid)
                oid += 1
                if oid > 60:                     # mpv overlay id limit
                    break
        for oid in self._last_ids:
            if oid not in used:
                try:
                    self._m.overlay_remove(oid)
                except Exception:
                    pass
                self._alive.pop(oid, None)
        self._last_ids = used


def _to_bgra_scaled(rgba: np.ndarray, w: int, h: int) -> np.ndarray:
    """Straight-alpha RGBA → scaled, premultiplied BGRA (contiguous)."""
    from PIL import Image
    img = Image.fromarray(rgba, 'RGBA')
    if (img.width, img.height) != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    a = np.asarray(img, np.float32)
    alpha = a[..., 3:4] / 255.0
    out = np.empty((h, w, 4), np.float32)
    out[..., 0] = a[..., 2] * alpha[..., 0]      # B premultiplied
    out[..., 1] = a[..., 1] * alpha[..., 0]      # G
    out[..., 2] = a[..., 0] * alpha[..., 0]      # R
    out[..., 3] = a[..., 3]
    return np.ascontiguousarray(out.astype(np.uint8))
