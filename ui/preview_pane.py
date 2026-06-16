import base64
import os
import subprocess
import tempfile
import traceback

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QColorDialog, QPushButton, QHBoxLayout, QStackedLayout,
                             QSpinBox, QDoubleSpinBox, QCheckBox, QMainWindow, QMenu, QApplication)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt, QPoint, QEvent, QTimer

from core.render import HtmlRenderer


# Hide the console window that ffmpeg would otherwise flash on Windows.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class PopOutPreviewWindow(QMainWindow):
    """
    A borderless, always-on-top window that mirrors the preview pane.

    - Sized to the exact OUTPUT pixel dimensions (the size fed to the Chrome
      windows that get saved to images), accounting for display scaling so the
      physical pixel size matches the render.
    - Left-click drag anywhere moves the window.
    - Right-click shows a menu with "Close Pop Out Preview".
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pop Out Preview")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # Destroy on close so the right-click "Close" reliably tears it down.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._drag_offset = QPoint()
        self._filtered = set()

        central = QWidget()
        self.setCentralWidget(central)

        self._stack = QStackedLayout(central)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._stack.setContentsMargins(0, 0, 0, 0)

        # Layer 1: background (letterbox / colour / video frame)
        self.view_bg = QWebEngineView()
        self.view_bg.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._stack.addWidget(self.view_bg)

        # Layer 2: foreground (subtitles, transparent)
        self.view_fg = QWebEngineView()
        self.view_fg.page().setBackgroundColor(QColor("transparent"))
        self.view_fg.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._stack.addWidget(self.view_fg)

        # The web views own native render surfaces that swallow mouse events,
        # so we filter their internal widgets to implement drag / context menu.
        self.view_bg.loadFinished.connect(lambda _: self._install_filters())
        self.view_fg.loadFinished.connect(lambda _: self._install_filters())

    # --- content mirroring ---------------------------------------------------
    def update_bg_html(self, html):
        self.view_bg.setHtml(html)

    def update_fg_html(self, html):
        self.view_fg.setHtml(html)

    def set_output_dimensions(self, width, height):
        """Resize so the window's PHYSICAL pixel size equals width x height."""
        screen = self.screen() or QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        if not dpr or dpr <= 0:
            dpr = 1.0
        logical_w = max(1, round(width / dpr))
        logical_h = max(1, round(height / dpr))
        self.setFixedSize(logical_w, logical_h)

    # --- mouse handling ------------------------------------------------------
    def _install_filters(self):
        for view in (self.view_bg, self.view_fg):
            proxy = view.focusProxy()
            if proxy is not None and proxy not in self._filtered:
                proxy.installEventFilter(self)
                self._filtered.add(proxy)

    def showEvent(self, event):
        super().showEvent(event)
        # Render widgets may not exist until shortly after show.
        QTimer.singleShot(0, self._install_filters)

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = (event.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
                return True
            if event.button() == Qt.MouseButton.RightButton:
                self._show_menu(event.globalPosition().toPoint())
                return True
        elif et == QEvent.Type.MouseMove:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
        return super().eventFilter(obj, event)

    # Fallback for any region not covered by the web views.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _show_menu(self, global_pos):
        menu = QMenu()
        close_action = menu.addAction("Close Pop Out Preview")
        chosen = menu.exec(global_pos)
        if chosen == close_action:
            self.close()


class PreviewPane(QWidget):
    def __init__(self):
        super().__init__()
        print("[DEBUG] PreviewPane initializing...")
        self.layout = QVBoxLayout(self)

        # Top Bar
        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("Preview")
        self.btn_popout = QPushButton("Pop Out Preview")
        self.btn_bg_color = QPushButton("Set BG Color")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(self.btn_popout)
        top_bar.addWidget(self.btn_bg_color)
        self.layout.addLayout(top_bar)

        # --- PREVIEW STACK (Layered HTML) ---
        # Container to hold the stack (This will be forced to 16:9)
        self.stack_container = QWidget()
        self.stack_layout = QStackedLayout(self.stack_container)
        self.stack_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)

        # WRAPPER: Lock the container to 16:9
        # The stack_container becomes a child of this wrapper.
        self.ar_wrapper = AspectRatioWidget(self.stack_container)

        # Add the wrapper to the layout instead of the raw container
        self.layout.addWidget(self.ar_wrapper)

        # Layer 1: Background (Color + Aspect Ratio Matte)
        self.view_bg = QWebEngineView()
        self.stack_layout.addWidget(self.view_bg)

        # Layer 2: Foreground (Subtitles - Transparent)
        self.view_fg = QWebEngineView()
        self.view_fg.page().setBackgroundColor(QColor("transparent"))
        self.stack_layout.addWidget(self.view_fg)

        # --- VIDEO FRAME TOGGLE ---
        opts_layout = QHBoxLayout()
        self.chk_show_frames = QCheckBox("Show video frames")
        self.chk_show_frames.setToolTip(
            "Display the matched video's frame at the midpoint of the selected\n"
            "subtitle behind the text (requires ffmpeg and a matched video).")
        self.chk_show_frames.setChecked(False)
        self.chk_show_frames.toggled.connect(self.on_show_frames_toggled)
        opts_layout.addWidget(self.chk_show_frames)

        self.chk_tone_map = QCheckBox("Tone Map HDR")
        self.chk_tone_map.setToolTip(
            "Tone map HDR/Dolby Vision frames to SDR for natural preview\n"
            "colors (fixes the purple/green cast on Dolby Vision Profile 5).\n"
            "No effect on SDR content. Slows down frame extraction slightly.")
        self.chk_tone_map.setChecked(False)
        self.chk_tone_map.toggled.connect(self.on_tone_map_toggled)
        opts_layout.addWidget(self.chk_tone_map)

        opts_layout.addStretch()
        self.layout.addLayout(opts_layout)

        # --- ASPECT RATIO CONTROLS ---
        ar_layout = QHBoxLayout()
        ar_layout.addWidget(QLabel("Aspect Ratio:"))

        self.spin_ar_num = QDoubleSpinBox()
        self.spin_ar_num.setRange(0.01, 10000.0)
        self.spin_ar_num.setDecimals(3)
        self.spin_ar_num.setSingleStep(0.01)
        self.spin_ar_num.setValue(16.0)
        self.spin_ar_num.valueChanged.connect(self._on_ar_changed)

        self.spin_ar_den = QDoubleSpinBox()
        self.spin_ar_den.setRange(0.01, 10000.0)
        self.spin_ar_den.setDecimals(3)
        self.spin_ar_den.setSingleStep(0.01)
        self.spin_ar_den.setValue(9.0)
        self.spin_ar_den.valueChanged.connect(self._on_ar_changed)

        ar_layout.addWidget(self.spin_ar_num)
        ar_layout.addWidget(QLabel(":"))
        ar_layout.addWidget(self.spin_ar_den)
        ar_layout.addStretch()

        self.layout.addLayout(ar_layout)

        # State
        self.bg_color = "#B0C4DE"

        # Padding state (mirrors the Global Overrides settings)
        self.pad_use = False
        self.pad_v = 0.0
        self.pad_h = 0.0

        # Output resolution (the size fed to the Chrome windows / popout).
        self.viewport_res = (1920, 1080)

        # Per-file preview AR memory. The AR controls only draw black-bar guides
        # in the preview; they do not affect the rendered output. Each loaded
        # file defaults to its own video aspect ratio and remembers manual edits.
        self._ar_by_key = {}            # file key -> (num, den)
        self._current_ar_key = None     # key of the file currently loaded
        self._suppress_ar_save = False  # True while setting spinboxes programmatically

        # Video frame state
        self.show_frames = False
        self.tone_map_hdr = False
        self.video_path = None
        self._frame_uri = None          # data URI for the current cue's frame
        self._frame_cache_key = None    # (video_path, rounded_ms, tone_map) of cached frame
        self._tonemap_candidates_cache = None  # probed ffmpeg tonemap chains (ordered)
        self._tonemap_vf = None         # the chain that last worked (tried first next time)
        self._vulkan_warned = False     # one-time libplacebo/Vulkan failure hint

        # Pop-out window (created on demand)
        self.popout_window = None

        # Cached HTML so a freshly opened pop-out can be populated immediately
        # and so we can skip redundant web-view reloads (which flicker).
        self._last_fg_html = ""
        self._last_bg_html = None
        self._last_popout_bg_html = None

        self.overrides = {}
        self.renderer = None
        self.current_cue = None

        self.update_background_layer()

        self.btn_bg_color.clicked.connect(self.pick_color)
        self.btn_popout.clicked.connect(self.toggle_popout)
        print("[DEBUG] PreviewPane initialized.")

    # =========================================================================
    # POP-OUT PREVIEW
    # =========================================================================
    def toggle_popout(self):
        if self.popout_window is None:
            self.popout_window = PopOutPreviewWindow(self)
            self.popout_window.destroyed.connect(self._on_popout_destroyed)
            self.popout_window.set_output_dimensions(*self.viewport_res)
            self.popout_window.show()
            # Seed it with the current content, using the true output aspect ratio.
            vw, vh = self.viewport_res
            popout_html = self._build_background_html(vw, vh)
            self._last_popout_bg_html = popout_html
            self.popout_window.update_bg_html(popout_html)
            if self._last_fg_html:
                self.popout_window.update_fg_html(self._last_fg_html)
            self.btn_popout.setText("Close Pop Out")
        else:
            self.popout_window.close()
            self.popout_window = None
            self.btn_popout.setText("Pop Out Preview")

    def _on_popout_destroyed(self, *args):
        # Window closed via its own context menu / OS — reset our state.
        self.popout_window = None
        self.btn_popout.setText("Pop Out Preview")

    def closeEvent(self, event):
        if self.popout_window is not None:
            self.popout_window.close()
            self.popout_window = None
        super().closeEvent(event)

    # =========================================================================
    # BACKGROUND LAYER
    # =========================================================================
    def _build_background_html(self, frame_num=16, frame_den=9):
        """
        Build the Background Layer HTML for an OUTPUT canvas of aspect
        frame_num:frame_den (16:9 for the main pane, the real viewport_res for
        the pop-out).

        Layering, from back to front:
          1. The output canvas itself is black (these are the "black bars").
          2. A coloured matte sized by the AR controls — purely a visual guide
             showing where the active content area sits. Mostly useful when no
             video frame is shown.
          3. The extracted video frame (when enabled) fills the WHOLE canvas
             1:1, so the AR controls never crop or reposition it.
          4. Padding boundary guides, aligned to the active area, on top.
        Subtitles sit on the separate foreground layer above all of this.
        """
        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()
        if den == 0:
            den = 1
        if frame_den == 0:
            frame_den = 1

        content_ratio = num / den
        frame_ratio = frame_num / frame_den

        # Active-area (matte) box as a percentage of the output canvas. It is
        # centred and letter/pillarboxed to fit content_ratio inside the canvas.
        if content_ratio > frame_ratio:
            aa_w = 100.0
            aa_h = (frame_ratio / content_ratio) * 100.0
        else:
            aa_h = 100.0
            aa_w = (content_ratio / frame_ratio) * 100.0
        aa_left = (100.0 - aa_w) / 2.0
        aa_top = (100.0 - aa_h) / 2.0

        matte_html = (
            f'<div class="matte" style="left:{aa_left:.4f}%;top:{aa_top:.4f}%;'
            f'width:{aa_w:.4f}%;height:{aa_h:.4f}%;"></div>'
        )

        # Video frame fills the entire canvas 1:1 (object-fit: contain keeps its
        # aspect; with Use Video Dimensions the canvas already matches it).
        frame_html = ""
        if self.show_frames and self._frame_uri:
            frame_html = f'<img class="video-frame" src="{self._frame_uri}" />'

        # Padding boundary guides, positioned relative to the active area.
        pad_lines = ""
        if self.pad_use:
            v_inset = self.pad_v / 2.0   # % of active-area height
            h_inset = self.pad_h / 2.0   # % of active-area width
            top1 = aa_top + (v_inset / 100.0) * aa_h
            bot1 = aa_top + aa_h - (v_inset / 100.0) * aa_h
            left1 = aa_left + (h_inset / 100.0) * aa_w
            right1 = aa_left + aa_w - (h_inset / 100.0) * aa_w
            pad_lines = (
                f'<div class="pad-line" style="left:{aa_left:.4f}%;width:{aa_w:.4f}%;'
                f'top:{top1:.4f}%;border-top:1px dashed #4aa3ff;"></div>'
                f'<div class="pad-line" style="left:{aa_left:.4f}%;width:{aa_w:.4f}%;'
                f'top:{bot1:.4f}%;border-top:1px dashed #4aa3ff;"></div>'
                f'<div class="pad-line" style="top:{aa_top:.4f}%;height:{aa_h:.4f}%;'
                f'left:{left1:.4f}%;border-left:1px dashed #ff4a4a;"></div>'
                f'<div class="pad-line" style="top:{aa_top:.4f}%;height:{aa_h:.4f}%;'
                f'left:{right1:.4f}%;border-left:1px dashed #ff4a4a;"></div>'
            )

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            body {{
                margin: 0; padding: 0;
                background-color: #111; /* Outer GUI void */
                height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                overflow: hidden;
            }}
            /* The Output Canvas. Its black background IS the "black bars".
               Aspect = 16:9 (main pane) or the real viewport_res (pop-out). */
            .output-frame {{
                position: relative;
                aspect-ratio: {frame_num} / {frame_den};
                width: 100%;
                max-width: 100vw;
                max-height: 100vh;
                background-color: black;
                overflow: hidden;
            }}
            /* Coloured matte guide (the active content area). Behind the frame. */
            .matte {{
                position: absolute;
                background-color: {self.bg_color};
                z-index: 1;
            }}
            /* Video frame: fills the whole canvas 1:1, on top of the matte so
               the AR guide never crops or repositions it. */
            .video-frame {{
                position: absolute;
                top: 0; left: 0;
                width: 100%; height: 100%;
                object-fit: contain;
                z-index: 2;
            }}
            /* Padding boundary guides, above the frame. */
            .pad-line {{ position: absolute; pointer-events: none; z-index: 3; }}
        </style>
        </head>
        <body>
            <div class="output-frame">{matte_html}{frame_html}{pad_lines}</div>
        </body>
        </html>
        """

    def update_background_layer(self):
        # Main pane: always the 16:9 reference frame.
        html = self._build_background_html()
        # Skip redundant reloads (avoids a flicker on every cue change when the
        # background hasn't actually changed, e.g. video frames disabled).
        if html != self._last_bg_html:
            self._last_bg_html = html
            self.view_bg.setHtml(html)

        # Pop-out: use the true output aspect ratio so it matches the render
        # exactly (no spurious letter/pillarboxing for non-16:9 output).
        if self.popout_window is not None:
            vw, vh = self.viewport_res
            popout_html = self._build_background_html(vw, vh)
            if popout_html != self._last_popout_bg_html:
                self._last_popout_bg_html = popout_html
                self.popout_window.update_bg_html(popout_html)

    def _on_ar_changed(self):
        # While setting the spinboxes programmatically, do nothing here; the
        # caller (set_project) refreshes the background once both are set.
        if self._suppress_ar_save:
            return
        # Remember the AR the user picked for the currently loaded file.
        if self._current_ar_key is not None:
            self._ar_by_key[self._current_ar_key] = (
                self.spin_ar_num.value(), self.spin_ar_den.value())
        self.update_background_layer()

    def _set_ar_spinboxes(self, num, den):
        """Set the AR controls without recording it as a manual user edit."""
        self._suppress_ar_save = True
        self.spin_ar_num.setValue(float(num))
        self.spin_ar_den.setValue(float(den))
        self._suppress_ar_save = False

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.bg_color))
        if c.isValid():
            self.bg_color = c.name()
            self.update_background_layer()

    # =========================================================================
    # VIDEO FRAMES
    # =========================================================================
    def set_video_path(self, video_path):
        new_path = video_path or None
        if new_path == self.video_path:
            return
        self.video_path = new_path
        # The cached frame belonged to the previous video.
        self._frame_uri = None
        self._frame_cache_key = None
        if self.show_frames and self.current_cue:
            self.render_cue(self.current_cue)

    def on_show_frames_toggled(self, checked):
        self.show_frames = checked
        if not checked:
            self._frame_uri = None
            self._frame_cache_key = None
        if self.current_cue:
            self.render_cue(self.current_cue)
        else:
            self.update_background_layer()

    def on_tone_map_toggled(self, checked):
        self.tone_map_hdr = checked
        # The cached frame was extracted with the old tone-map setting.
        self._frame_uri = None
        self._frame_cache_key = None
        if self.show_frames and self.current_cue:
            self.render_cue(self.current_cue)

    def _refresh_frame_for_cue(self, cue):
        """Extract (and cache) the frame for the midpoint of the given cue."""
        if not (self.show_frames and self.video_path and cue is not None):
            self._frame_uri = None
            self._frame_cache_key = None
            return

        midpoint_ms = (cue.start_ms + cue.end_ms) / 2.0
        # Tone-map state is part of the key so toggling re-extracts the frame.
        cache_key = (self.video_path, round(midpoint_ms), self.tone_map_hdr)
        if cache_key == self._frame_cache_key and self._frame_uri:
            return  # already have this frame

        uri = self._extract_frame_data_uri(self.video_path, midpoint_ms / 1000.0)
        self._frame_uri = uri
        self._frame_cache_key = cache_key if uri else None

    def _tonemap_candidates(self):
        """
        Ordered list of ffmpeg -vf tone-mapping chains to try, best first.

        Probed against the installed ffmpeg once and cached. libplacebo is
        preferred when available (it handles Dolby Vision metadata), with a
        zscale+tonemap fallback for HDR10/HLG on older builds. Both are
        transfer-aware, so SDR input passes through essentially unchanged.

        A chain can be *listed* by ffmpeg yet fail at runtime (e.g. libplacebo
        with unusual Dolby Vision streams or missing Vulkan), so the extractor
        tries them in order and an empty chain (untone-mapped) is always the
        last resort to ensure a frame is always extracted.
        """
        if self._tonemap_candidates_cache is not None:
            return self._tonemap_candidates_cache

        available = ""
        try:
            probe = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=_NO_WINDOW,
            )
            # The filter list is plain ASCII; ignore any stray bytes.
            available = probe.stdout.decode("ascii", errors="ignore")
        except Exception as e:
            print(f"[FRAME] Could not probe ffmpeg filters: {e}")

        candidates = []
        if " libplacebo " in available:
            # libplacebo is the only chain that applies the Dolby Vision RPU,
            # which is what fixes the DV Profile 5 purple/green cast. Try the
            # DV-aware form first; fall back to plain HDR tone mapping for builds
            # whose libplacebo lacks the apply_dolbyvision option.
            candidates.append(
                "libplacebo=apply_dolbyvision=true:tonemapping=bt.2390:"
                "colorspace=bt709:color_primaries=bt709:color_trc=bt709,"
                "format=yuv420p")
            candidates.append("libplacebo=format=yuv420p")
        if " zscale " in available and " tonemap " in available:
            # Fallback for HDR10/HLG when libplacebo is unavailable or fails.
            # (This cannot fix DV Profile 5, which needs the RPU.) Input transfer
            # /matrix/primaries are forced (tin/min/pin) so streams with
            # unspecified colour metadata don't error out with -22.
            candidates.append(
                "zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:t=linear:npl=100,"
                "format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:p=bt709:r=tv,format=yuv420p")
        # Last resort: no filter (frame shown untone-mapped) so the user still
        # gets a positioning reference even if no tone-mapper works.
        candidates.append("")

        self._tonemap_candidates_cache = candidates
        return candidates

    def _extract_frame_data_uri(self, video_path, timestamp_seconds):
        """Grab a single frame via ffmpeg and return it as a data URI (or None)."""
        if not video_path or not os.path.exists(video_path):
            print(f"[FRAME] Video not found: {video_path}")
            return None

        tmp_path = os.path.join(
            tempfile.gettempdir(), f"ttml2pgs_preview_{os.getpid()}.jpg"
        )

        # Build the list of -vf chains to attempt. Without tone mapping this is
        # just [None]; with it, try the known-good chain first (if one already
        # worked), then the remaining probed candidates.
        if self.tone_map_hdr:
            vf_attempts = list(self._tonemap_candidates())
            if self._tonemap_vf is not None and self._tonemap_vf in vf_attempts:
                vf_attempts.remove(self._tonemap_vf)
                vf_attempts.insert(0, self._tonemap_vf)
        else:
            vf_attempts = [None]

        for vf in vf_attempts:
            # libplacebo needs a Vulkan device; create one explicitly (global
            # option, before -i) as it is more reliable than ffmpeg's auto-init.
            pre_args = []
            if vf and "libplacebo" in vf:
                pre_args = ["-init_hw_device", "vulkan", "-filter_hw_device", "vulkan"]

            # Input seeking (-ss before -i) is fast even on large/network files.
            # -nostdin + -y prevent the interactive overwrite prompt from hanging.
            cmd = [
                "ffmpeg", "-nostdin", "-y",
                *pre_args,
                "-ss", f"{timestamp_seconds:.3f}",
                "-i", video_path,
                "-frames:v", "1",
            ]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-q:v", "2", tmp_path]

            # Capture stderr (as bytes) so a failing tone-map attempt can report
            # *why* it failed (e.g. Vulkan/libplacebo errors) for diagnosis.
            try:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    creationflags=_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                print(f"[FRAME] ffmpeg timed out for {video_path} @ {timestamp_seconds:.3f}s")
                return None
            except FileNotFoundError:
                print("[FRAME] ffmpeg not found on PATH.")
                return None
            except Exception as e:
                print(f"[FRAME] ffmpeg failed: {e}")
                return None

            if result.returncode == 0 and os.path.exists(tmp_path) \
                    and os.path.getsize(tmp_path) > 0:
                # Remember the chain that worked so later frames skip the duds.
                if self.tone_map_hdr:
                    self._tonemap_vf = vf
                    if vf:
                        print(f"[FRAME] tone-map OK via: {vf.split(',')[0]}")
                try:
                    with open(tmp_path, "rb") as fh:
                        encoded = base64.b64encode(fh.read()).decode("ascii")
                    return f"data:image/jpeg;base64,{encoded}"
                except Exception as e:
                    print(f"[FRAME] Failed reading frame: {e}")
                    return None
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

            # This chain failed; surface the ffmpeg error and try the next one.
            if vf:
                err = (result.stderr or b"").decode("utf-8", errors="ignore")
                tail = " | ".join(
                    ln.strip() for ln in err.strip().splitlines()[-4:] if ln.strip())
                print(f"[FRAME] tone-map chain failed (rc={result.returncode}): "
                      f"{vf.split(',')[0]}")
                if tail:
                    print(f"[FRAME]   ffmpeg: {tail}")
                # libplacebo is the only chain that corrects Dolby Vision P5;
                # if its Vulkan device can't be created, flag it once with a hint.
                if ("libplacebo" in vf and "Vulkan" in err
                        and not self._vulkan_warned):
                    self._vulkan_warned = True
                    print("[FRAME]   NOTE: libplacebo could not create a Vulkan "
                          "device. Dolby Vision Profile 5 colour correction "
                          "requires a Vulkan-capable GPU/driver and an ffmpeg "
                          "built with libplacebo. Update your GPU drivers / "
                          "install the Vulkan runtime, or use an ffmpeg build "
                          "with working Vulkan support. The non-libplacebo "
                          "fallbacks cannot fix the DV P5 colour cast.")
                print("[FRAME]   trying fallback.")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        print("[FRAME] All extraction attempts failed.")
        return None

    # =========================================================================
    # PROJECT / RENDER
    # =========================================================================
    def set_project(self, project, overrides, content_res=None, viewport_res=None,
                    video_res=None, video_path=None):
        print(f"[DEBUG] set_project called with overrides: {overrides.keys() if overrides else 'None'}")

        self.overrides = overrides or {}

        if video_path is not None:
            self.set_video_path(video_path)

        if viewport_res:
            self.viewport_res = viewport_res
            if self.popout_window is not None:
                self.popout_window.set_output_dimensions(*viewport_res)

        # Default the preview AR guide to this file's video aspect ratio, with
        # per-file memory of any manual edits. Only (re)apply when the loaded
        # file actually changes, so settings refreshes never clobber the user's
        # choice. Key by video path, falling back to project identity.
        ar_key = video_path or (id(project) if project is not None else None)
        if ar_key is not None and ar_key != self._current_ar_key:
            self._current_ar_key = ar_key
            if ar_key in self._ar_by_key:
                num, den = self._ar_by_key[ar_key]
            elif video_res and video_res[0] and video_res[1]:
                num, den = video_res
                self._ar_by_key[ar_key] = (num, den)
            else:
                num, den = 16.0, 9.0
            self._set_ar_spinboxes(num, den)

        # Sync padding state and refresh the boundary-line overlay
        self.pad_use = self.overrides.get('use_padding', False)
        self.pad_v = self.overrides.get('padding_v', 0.0)
        self.pad_h = self.overrides.get('padding_h', 0.0)
        self.update_background_layer()

        # Prepare arguments for the Renderer
        renderer_args = self.overrides.copy()

        keys_to_remove = [
            'window_bg',
            'auto_color_enabled',
            'auto_sdr_color', 'auto_sdr_alpha',
            'auto_hdr_color', 'auto_hdr_alpha',
            'force_16_9', 'remux_enabled',
            'override_ar_enabled', 'ar_num', 'ar_den',
            'cleanup_enabled', 'move_enabled', 'web_view',
            "use_video_dims",
            "scale_to_hd"
        ]
        for k in keys_to_remove:
            if k in renderer_args:
                renderer_args.pop(k)

        try:
            print(f"[DEBUG] Initializing HtmlRenderer with content_res: {content_res}")
            self.renderer = HtmlRenderer(
                project,
                content_resolution=content_res,
                **renderer_args)
            print("[DEBUG] Renderer initialized.")
        except Exception as e:
            print(f"[ERROR] Renderer init failed: {e}")
            traceback.print_exc()
            return

        if self.current_cue:
            self.render_cue(self.current_cue)

    def render_cue(self, cue=None):
        print("[DEBUG] render_cue called")
        if cue:
            self.current_cue = cue

        if not self.current_cue:
            print("[DEBUG] No current cue.")
            return

        if not self.renderer:
            print("[DEBUG] No renderer exists.")
            return

        try:
            # Refresh the video frame (if enabled) before rebuilding the bg.
            self._refresh_frame_for_cue(self.current_cue)
            self.update_background_layer()

            html = self.renderer.render_cue_to_html(
                self.current_cue,
                preview_bg="transparent"
            )
            self._last_fg_html = html

            print(f"[DEBUG] HTML generated ({len(html)} chars). Updating WebView.")
            self.view_fg.setHtml(html)
            if self.popout_window is not None:
                self.popout_window.update_fg_html(html)

        except Exception as e:
            print(f"[ERROR] Preview Render failed: {e}")
            traceback.print_exc()


class AspectRatioWidget(QWidget):
    """
    A container that forces its child widget to maintain a 16:9 aspect ratio,
    centering it within the available space.
    """

    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self.widget = widget
        self.widget.setParent(self)
        # Dark grey background for the empty space around the player
        self.setStyleSheet("background-color: #202020;")

    def resizeEvent(self, event):
        w = self.width()
        h = self.height()

        target_ratio = 16.0 / 9.0

        # Calculate dimensions to fit 16:9 inside the available area
        if h == 0:
            return  # Prevent division by zero

        if w / h > target_ratio:
            # Available space is wider than 16:9 -> Fit to Height
            new_h = h
            new_w = int(new_h * target_ratio)
        else:
            # Available space is taller than 16:9 -> Fit to Width
            new_w = w
            new_h = int(new_w / target_ratio)

        # Center the widget
        x = (w - new_w) // 2
        y = (h - new_h) // 2

        self.widget.setGeometry(x, y, new_w, new_h)
