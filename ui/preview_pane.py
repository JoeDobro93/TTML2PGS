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
        self.spin_ar_num.valueChanged.connect(self.update_background_layer)

        self.spin_ar_den = QDoubleSpinBox()
        self.spin_ar_den.setRange(0.01, 10000.0)
        self.spin_ar_den.setDecimals(3)
        self.spin_ar_den.setSingleStep(0.01)
        self.spin_ar_den.setValue(9.0)
        self.spin_ar_den.valueChanged.connect(self.update_background_layer)

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

        # Video frame state
        self.show_frames = False
        self.video_path = None
        self._frame_uri = None          # data URI for the current cue's frame
        self._frame_cache_key = None    # (video_path, rounded_ms) of cached frame

        # Pop-out window (created on demand)
        self.popout_window = None

        # Cached HTML so a freshly opened pop-out can be populated immediately
        # and so we can skip redundant web-view reloads (which flicker).
        self._last_fg_html = ""
        self._last_bg_html = None

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
            # Seed it with the current frame content.
            self.popout_window.update_bg_html(self._build_background_html())
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
    def _build_background_html(self):
        """
        Build the Background Layer HTML: a 16:9 black frame containing the
        'active area' (coloured matte sized by the AR controls). When video
        frames are enabled, the extracted frame is drawn inside the active
        area, behind the padding guides. Subtitles sit on the foreground
        layer above everything.
        """
        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()
        if den == 0:
            den = 1

        target_ratio = num / den
        base_ratio = 16 / 9

        if target_ratio > base_ratio:
            # Wider than 16:9 -> letterbox (bars top/bottom)
            fit_style = "width: 100%;"
        else:
            # Taller/equal -> pillarbox (bars left/right)
            fit_style = "height: 100%;"

        # Optional video frame layer (sits behind the padding guides).
        frame_html = ""
        if self.show_frames and self._frame_uri:
            frame_html = f'<img class="video-frame" src="{self._frame_uri}" />'

        # Padding boundary guides (blue = vertical edges, red = horizontal).
        pad_lines = ""
        if self.pad_use:
            v_inset = self.pad_v / 2.0
            h_inset = self.pad_h / 2.0
            pad_lines = (
                f'<div class="pad-line pad-v" style="top: {v_inset}%;"></div>'
                f'<div class="pad-line pad-v" style="bottom: {v_inset}%;"></div>'
                f'<div class="pad-line pad-h" style="left: {h_inset}%;"></div>'
                f'<div class="pad-line pad-h" style="right: {h_inset}%;"></div>'
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
            /* The 16:9 HD Frame (The "Black Bars" Generator) */
            .frame-16-9 {{
                aspect-ratio: 16/9;
                width: 100%;
                max-width: 100vw;
                max-height: 100vh;
                background-color: black;
                display: flex;
                justify-content: center;
                align-items: center;
            }}
            /* The Active Video Content Area */
            .active-area {{
                position: relative;
                background-color: {self.bg_color};
                aspect-ratio: {num} / {den};
                {fit_style}
                overflow: hidden;
            }}
            /* Video frame sits inside the active area, above the matte colour.
               object-fit: contain preserves the video's native aspect ratio,
               letter/pillarboxing against the black active area when the video
               is not the same shape as the active area (no stretching). */
            .video-frame {{
                position: absolute;
                top: 0; left: 0;
                width: 100%; height: 100%;
                object-fit: contain;
                z-index: 1;
            }}
            /* Padding boundary guides (overlaid above the frame) */
            .pad-line {{ position: absolute; pointer-events: none; z-index: 2; }}
            .pad-v {{ left: 0; right: 0; border-top: 1px dashed #4aa3ff; }}   /* blue: vertical padding */
            .pad-h {{ top: 0; bottom: 0; border-left: 1px dashed #ff4a4a; }}  /* red: horizontal padding */
        </style>
        </head>
        <body>
            <div class="frame-16-9">
                <div class="active-area">{frame_html}{pad_lines}</div>
            </div>
        </body>
        </html>
        """

    def update_background_layer(self):
        html = self._build_background_html()
        # Skip redundant reloads (avoids a flicker on every cue change when the
        # background hasn't actually changed, e.g. video frames disabled).
        if html == self._last_bg_html:
            return
        self._last_bg_html = html
        self.view_bg.setHtml(html)
        if self.popout_window is not None:
            self.popout_window.update_bg_html(html)

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

    def _refresh_frame_for_cue(self, cue):
        """Extract (and cache) the frame for the midpoint of the given cue."""
        if not (self.show_frames and self.video_path and cue is not None):
            self._frame_uri = None
            self._frame_cache_key = None
            return

        midpoint_ms = (cue.start_ms + cue.end_ms) / 2.0
        cache_key = (self.video_path, round(midpoint_ms))
        if cache_key == self._frame_cache_key and self._frame_uri:
            return  # already have this frame

        uri = self._extract_frame_data_uri(self.video_path, midpoint_ms / 1000.0)
        self._frame_uri = uri
        self._frame_cache_key = cache_key if uri else None

    def _extract_frame_data_uri(self, video_path, timestamp_seconds):
        """Grab a single frame via ffmpeg and return it as a data URI (or None)."""
        if not video_path or not os.path.exists(video_path):
            print(f"[FRAME] Video not found: {video_path}")
            return None

        tmp_path = os.path.join(
            tempfile.gettempdir(), f"ttml2pgs_preview_{os.getpid()}.jpg"
        )
        # Input seeking (-ss before -i) is fast even on large/network files.
        # -nostdin + -y prevent the interactive overwrite prompt from hanging.
        cmd = [
            "ffmpeg", "-nostdin", "-y",
            "-ss", f"{timestamp_seconds:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            tmp_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
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

        if result.returncode != 0 or not os.path.exists(tmp_path):
            print(f"[FRAME] ffmpeg returned {result.returncode}")
            return None

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

    # =========================================================================
    # PROJECT / RENDER
    # =========================================================================
    def set_project(self, project, overrides, content_res=None, viewport_res=None, video_path=None):
        print(f"[DEBUG] set_project called with overrides: {overrides.keys() if overrides else 'None'}")

        self.overrides = overrides or {}

        if video_path is not None:
            self.set_video_path(video_path)

        if viewport_res:
            self.viewport_res = viewport_res
            if self.popout_window is not None:
                self.popout_window.set_output_dimensions(*viewport_res)

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
