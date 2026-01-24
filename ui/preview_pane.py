import traceback
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QColorDialog, QPushButton, QHBoxLayout, QStackedLayout,
                             QSpinBox, QDoubleSpinBox)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtGui import QColor

from core.render import HtmlRenderer


class PreviewPane(QWidget):
    def __init__(self):
        super().__init__()
        print("[DEBUG] PreviewPane initializing...")
        self.layout = QVBoxLayout(self)

        # Top Bar
        top_bar = QHBoxLayout()
        self.lbl_title = QLabel("Preview")
        self.btn_bg_color = QPushButton("Set BG Color")
        top_bar.addWidget(self.lbl_title)
        top_bar.addStretch()
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
        self.update_background_layer()

        # --- FIX 1: Initialize overrides so it exists ---
        self.overrides = {}

        self.btn_bg_color.clicked.connect(self.pick_color)
        self.renderer = None
        self.current_cue = None
        print("[DEBUG] PreviewPane initialized.")

    def update_background_layer(self):
        """
        Generates the Background Layer HTML.
        This draws a 16:9 Black container, and inside it,
        the 'Active Area' colored box based on the AR settings.
        """
        num = self.spin_ar_num.value()
        den = self.spin_ar_den.value()

        # Avoid division by zero
        if den == 0: den = 1

        target_ratio = num / den
        base_ratio = 16 / 9

        # Determine CSS to fit the Colored Box inside the 16:9 Black Box
        if target_ratio > base_ratio:
            # Wider than 16:9 -> Letterbox (Bars Top/Bottom)
            # Width is 100%, Height is auto (constrained by aspect-ratio)
            fit_style = "width: 100%;"
        else:
            # Taller/Same as 16:9 -> Pillarbox (Bars Left/Right)
            # Height is 100%, Width is auto
            fit_style = "height: 100%;"

        html = f"""
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
        self.view_bg.setHtml(html)

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.bg_color))
        if c.isValid():
            self.bg_color = c.name()
            # Update the HTML layer instead of the widget stylesheet
            self.update_background_layer()

    def set_project(self, project, overrides, content_res=None):
        print(f"[DEBUG] set_project called with overrides: {overrides.keys() if overrides else 'None'}")

        # --- FIX 2: Store overrides for later use in render_cue ---
        self.overrides = overrides or {}

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

        # We must remove 'window_bg' AND the new Auto-Color keys,
        # otherwise HtmlRenderer will crash with a TypeError.
        for k in keys_to_remove:
            if k in renderer_args:
                renderer_args.pop(k)

        # if 'global_alpha' in renderer_args:
        #     alpha = renderer_args.pop('global_alpha')
        #     self.view_fg.setWindowOpacity(alpha)

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
        if cue: self.current_cue = cue

        if not self.current_cue:
            print("[DEBUG] No current cue.")
            return

        if not self.renderer:
            print("[DEBUG] No renderer exists.")
            return

        try:
            bg_color = self.bg_color
            print(f"[DEBUG] Using preview_bg: {bg_color}")

            # 2. Call render_cue_to_html
            # core/render.py MUST have the updated signature for this to work
            html = self.renderer.render_cue_to_html(
                self.current_cue,
                preview_bg="transparent"
            )

            print(f"[DEBUG] HTML generated ({len(html)} chars). Updating WebView.")
            self.view_fg.setHtml(html)

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
        if h == 0: return  # Prevent division by zero

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