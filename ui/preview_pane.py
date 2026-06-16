import os
import traceback
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QColorDialog, QPushButton, QHBoxLayout, QStackedLayout,
                             QSpinBox, QDoubleSpinBox, QCheckBox)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtGui import QColor
from PyQt6.QtCore import QUrl, pyqtSignal

from core.render import HtmlRenderer
from core.video_frame import VideoFrameExtractor


# Layer draw order (bottom -> top). Later layers render on top.
LAYERS = ("bg", "video", "fg", "overlay")


def _transparent_view():
    v = QWebEngineView()
    v.page().setBackgroundColor(QColor("transparent"))
    return v


class PreviewPane(QWidget):
    def __init__(self):
        super().__init__()
        print("[DEBUG] PreviewPane initializing...")
        self.layout = QVBoxLayout(self)

        # Top Bar
        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("Preview")
        self.chk_show_frames = QCheckBox("Show video frames")
        self.chk_show_frames.setToolTip(
            "Render a still from the matched video (at the midpoint of the selected\n"
            "subtitle) behind the subtitles and padding lines. Requires ffmpeg and a\n"
            "matched video file.")
        self.chk_show_frames.setChecked(False)  # Default OFF
        self.chk_show_frames.toggled.connect(self.update_video_layer)
        self.btn_popout = QPushButton("Pop Out Preview")
        self.btn_popout.setToolTip(
            "Open a window sized to the exact output resolution that mirrors this\n"
            "preview. Useful for verifying pixel-accurate placement.")
        self.btn_popout.clicked.connect(self.open_popout)
        self.btn_bg_color = QPushButton("Set BG Color")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(self.chk_show_frames)
        top_bar.addWidget(self.btn_popout)
        top_bar.addWidget(self.btn_bg_color)
        self.layout.addLayout(top_bar)

        # --- PREVIEW STACK (Layered HTML) ---
        self.stack_container = QWidget()
        self.stack_layout = QStackedLayout(self.stack_container)
        self.stack_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)

        # WRAPPER: Lock the container to 16:9
        self.ar_wrapper = AspectRatioWidget(self.stack_container)
        self.layout.addWidget(self.ar_wrapper)

        # Layers (added bottom -> top)
        self.view_bg = QWebEngineView()           # Background: bg color + AR matte (opaque)
        self.view_video = _transparent_view()     # Video frame (optional)
        self.view_fg = _transparent_view()         # Subtitles (transparent)
        self.view_overlay = _transparent_view()    # Padding boundary lines (transparent)

        self._views = {
            "bg": self.view_bg,
            "video": self.view_video,
            "fg": self.view_fg,
            "overlay": self.view_overlay,
        }
        for name in LAYERS:
            self.stack_layout.addWidget(self._views[name])

        # --- ASPECT RATIO CONTROLS ---
        ar_layout = QHBoxLayout()
        ar_layout.addWidget(QLabel("Aspect Ratio:"))

        self.spin_ar_num = QDoubleSpinBox()
        self.spin_ar_num.setRange(0.01, 10000.0)
        self.spin_ar_num.setDecimals(3)
        self.spin_ar_num.setSingleStep(0.01)
        self.spin_ar_num.setValue(16.0)
        self.spin_ar_num.valueChanged.connect(self.refresh_layout_layers)

        self.spin_ar_den = QDoubleSpinBox()
        self.spin_ar_den.setRange(0.01, 10000.0)
        self.spin_ar_den.setDecimals(3)
        self.spin_ar_den.setSingleStep(0.01)
        self.spin_ar_den.setValue(9.0)
        self.spin_ar_den.valueChanged.connect(self.refresh_layout_layers)

        ar_layout.addWidget(self.spin_ar_num)
        ar_layout.addWidget(QLabel(":"))
        ar_layout.addWidget(self.spin_ar_den)
        ar_layout.addStretch()

        self.layout.addLayout(ar_layout)

        # State
        self.bg_color = "#B0C4DE"
        self.overrides = {}
        self.renderer = None
        self.current_cue = None

        # Padding state (mirrors the Global Overrides settings)
        self.pad_use = False
        self.pad_v = 0.0
        self.pad_h = 0.0

        # Output / video state
        self.viewport_res = (1920, 1080)
        self.video_path = None
        self.frame_path = None

        self._extractor = VideoFrameExtractor()
        self.popout = None

        # Cache of the last HTML pushed to each layer so a freshly-opened
        # popout can be brought up to date immediately. value: (html, base_url|None)
        self._last_html = {name: ("", None) for name in LAYERS}

        self.btn_bg_color.clicked.connect(self.pick_color)

        # Initial paint
        self.update_background_layer()
        self.update_video_layer()
        self.update_overlay_layer()
        print("[DEBUG] PreviewPane initialized.")

    # ------------------------------------------------------------------ #
    #  Layer plumbing
    # ------------------------------------------------------------------ #
    def _push(self, layer, html, base_url=None):
        """Set HTML on the local view and (if open) the popout's mirror view."""
        self._last_html[layer] = (html, base_url)

        local = self._views[layer]
        if base_url is not None:
            local.setHtml(html, base_url)
        else:
            local.setHtml(html)

        if self.popout:
            pv = self.popout.views[layer]
            if base_url is not None:
                pv.setHtml(html, base_url)
            else:
                pv.setHtml(html)

    def _fit_style(self, num, den):
        """CSS to fit the content (active area) box inside the 16:9 matte frame."""
        if den == 0:
            den = 1
        target_ratio = num / den
        base_ratio = 16 / 9
        if target_ratio > base_ratio:
            return "width: 100%;"  # Letterbox
        return "height: 100%;"     # Pillarbox

    # ------------------------------------------------------------------ #
    #  Layer builders
    # ------------------------------------------------------------------ #
    def update_background_layer(self):
        """Background matte: 16:9 black frame with the colored 'active area' inside."""
        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()
        fit_style = self._fit_style(num, den)

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            body {{
                margin: 0; padding: 0;
                background-color: #111;
                height: 100vh;
                display: flex; justify-content: center; align-items: center;
                overflow: hidden;
            }}
            .frame-16-9 {{
                aspect-ratio: 16/9;
                width: 100%; max-width: 100vw; max-height: 100vh;
                background-color: black;
                display: flex; justify-content: center; align-items: center;
            }}
            .active-area {{
                background-color: {self.bg_color};
                aspect-ratio: {num} / {den};
                {fit_style}
            }}
        </style>
        </head>
        <body>
            <div class="frame-16-9">
                <div class="active-area"></div>
            </div>
        </body>
        </html>
        """
        self._push("bg", html)

    def update_video_layer(self):
        """Video frame layer: a still from the matched video filling the active area."""
        # Re-extract the frame for the current cue if enabled.
        self.frame_path = None
        if self.chk_show_frames.isChecked() and self.video_path and self.current_cue:
            try:
                mid_ms = (self.current_cue.start_ms + self.current_cue.end_ms) / 2.0
                self.frame_path = self._extractor.extract(self.video_path, mid_ms)
            except Exception as e:
                print(f"[ERROR] Frame extraction failed: {e}")

        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()
        fit_style = self._fit_style(num, den)

        base_url = None
        img_html = ""
        if self.frame_path:
            base_url = QUrl.fromLocalFile(self.frame_path)
            # base_url points at the frame, so a bare filename resolves to it.
            img_html = (
                f'<img src="{os.path.basename(self.frame_path)}" '
                f'style="width:100%; height:100%; object-fit: fill; display:block;">'
            )

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            body {{
                margin: 0; padding: 0;
                background: transparent;
                height: 100vh;
                display: flex; justify-content: center; align-items: center;
                overflow: hidden;
            }}
            .frame-16-9 {{
                aspect-ratio: 16/9;
                width: 100%; max-width: 100vw; max-height: 100vh;
                background: transparent;
                display: flex; justify-content: center; align-items: center;
            }}
            .active-area {{
                aspect-ratio: {num} / {den};
                {fit_style}
                overflow: hidden;
            }}
        </style>
        </head>
        <body>
            <div class="frame-16-9">
                <div class="active-area">{img_html}</div>
            </div>
        </body>
        </html>
        """
        self._push("video", html, base_url)

    def update_overlay_layer(self):
        """Padding boundary guides (blue = vertical edges, red = horizontal edges)."""
        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()
        fit_style = self._fit_style(num, den)

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

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            body {{
                margin: 0; padding: 0;
                background: transparent;
                height: 100vh;
                display: flex; justify-content: center; align-items: center;
                overflow: hidden;
            }}
            .frame-16-9 {{
                aspect-ratio: 16/9;
                width: 100%; max-width: 100vw; max-height: 100vh;
                background: transparent;
                display: flex; justify-content: center; align-items: center;
            }}
            .active-area {{
                position: relative;
                aspect-ratio: {num} / {den};
                {fit_style}
            }}
            .pad-line {{ position: absolute; pointer-events: none; }}
            .pad-v {{ left: 0; right: 0; border-top: 1px dashed #4aa3ff; }}
            .pad-h {{ top: 0; bottom: 0; border-left: 1px dashed #ff4a4a; }}
        </style>
        </head>
        <body>
            <div class="frame-16-9">
                <div class="active-area">{pad_lines}</div>
            </div>
        </body>
        </html>
        """
        self._push("overlay", html)

    def refresh_layout_layers(self):
        """Rebuild every AR-dependent layer (matte, video, overlay)."""
        self.update_background_layer()
        self.update_video_layer()
        self.update_overlay_layer()

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.bg_color))
        if c.isValid():
            self.bg_color = c.name()
            self.update_background_layer()

    def set_project(self, project, overrides, content_res=None, viewport_res=None, video_path=None):
        print(f"[DEBUG] set_project called with overrides: {overrides.keys() if overrides else 'None'}")

        self.overrides = overrides or {}

        # Sync padding state
        self.pad_use = self.overrides.get('use_padding', False)
        self.pad_v = self.overrides.get('padding_v', 0.0)
        self.pad_h = self.overrides.get('padding_h', 0.0)

        # Output size + matched video
        if viewport_res:
            self.viewport_res = viewport_res
        self.video_path = video_path

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
            "use_video_dims", "scale_to_hd",
        ]
        for k in keys_to_remove:
            renderer_args.pop(k, None)

        try:
            print(f"[DEBUG] Initializing HtmlRenderer with content_res: {content_res}")
            self.renderer = HtmlRenderer(project, content_resolution=content_res, **renderer_args)
        except Exception as e:
            print(f"[ERROR] Renderer init failed: {e}")
            traceback.print_exc()
            return

        # Keep the popout sized to the current output resolution.
        if self.popout:
            self.popout.set_size(self.viewport_res)

        # Repaint all layers
        self.update_background_layer()
        self.update_overlay_layer()
        if self.current_cue:
            self.render_cue(self.current_cue)
        else:
            self.update_video_layer()

    def render_cue(self, cue=None):
        if cue:
            self.current_cue = cue

        if not self.current_cue or not self.renderer:
            # Still refresh the frame layer (it may need to clear).
            self.update_video_layer()
            return

        try:
            html = self.renderer.render_cue_to_html(self.current_cue, preview_bg="transparent")
            self._push("fg", html)
        except Exception as e:
            print(f"[ERROR] Preview Render failed: {e}")
            traceback.print_exc()

        # Frame depends on the selected cue's timestamp.
        self.update_video_layer()

    # ------------------------------------------------------------------ #
    #  Pop-out window
    # ------------------------------------------------------------------ #
    def open_popout(self):
        if self.popout:
            self.popout.raise_()
            self.popout.activateWindow()
            return

        self.popout = PreviewPopout(self.viewport_res)
        self.popout.closed.connect(self._on_popout_closed)

        # Bring the new window up to date with whatever the preview shows now.
        for layer in LAYERS:
            html, base_url = self._last_html[layer]
            if not html:
                continue
            pv = self.popout.views[layer]
            if base_url is not None:
                pv.setHtml(html, base_url)
            else:
                pv.setHtml(html)

        self.popout.show()

    def _on_popout_closed(self):
        self.popout = None


class PreviewPopout(QWidget):
    """A standalone window that mirrors the preview at the exact output resolution."""
    closed = pyqtSignal()

    def __init__(self, size):
        super().__init__()
        self.setStyleSheet("background-color: #202020;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.container = QWidget()
        self.stack = QStackedLayout(self.container)
        self.stack.setStackingMode(QStackedLayout.StackingMode.StackAll)

        self.view_bg = QWebEngineView()
        self.view_video = _transparent_view()
        self.view_fg = _transparent_view()
        self.view_overlay = _transparent_view()

        self.views = {
            "bg": self.view_bg,
            "video": self.view_video,
            "fg": self.view_fg,
            "overlay": self.view_overlay,
        }
        for name in LAYERS:
            self.stack.addWidget(self.views[name])

        lay.addWidget(self.container)
        self.set_size(size)

    def set_size(self, size):
        try:
            w, h = int(size[0]), int(size[1])
        except Exception:
            w, h = 1920, 1080
        self.container.setFixedSize(w, h)
        # Fix the window to the exact output size so the canvas is 1:1 with the render.
        self.setFixedSize(w, h)
        self.setWindowTitle(f"Preview — {w} × {h} (Output Size)")

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class AspectRatioWidget(QWidget):
    """
    A container that forces its child widget to maintain a 16:9 aspect ratio,
    centering it within the available space.
    """

    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self.widget = widget
        self.widget.setParent(self)
        self.setStyleSheet("background-color: #202020;")

    def resizeEvent(self, event):
        w = self.width()
        h = self.height()

        target_ratio = 16.0 / 9.0
        if h == 0:
            return

        if w / h > target_ratio:
            new_h = h
            new_w = int(new_h * target_ratio)
        else:
            new_w = w
            new_h = int(new_w / target_ratio)

        x = (w - new_w) // 2
        y = (h - new_h) // 2
        self.widget.setGeometry(x, y, new_w, new_h)
