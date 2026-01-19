from PyQt6.QtWidgets import (QWidget, QTabWidget, QFormLayout, QCheckBox,
                             QDoubleSpinBox, QComboBox, QPushButton, QHBoxLayout,
                             QLabel, QColorDialog, QVBoxLayout, QGroupBox,
                             QStyleFactory, QScrollArea, QListWidget, QLineEdit, QSplitter)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QColor, QPalette

from core.models import Style, Region

class SettingsPane(QWidget):
    settings_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()

        self.current_project = None
        self.editors = {}

        self.setStyleSheet("""
                    QCheckBox { color: #E0E0E0; spacing: 5px; }
                    QGroupBox { color: #E0E0E0; font-weight: bold; }
                    QLabel { color: #E0E0E0; }
                    
                    QLineEdit { 
                        background-color: #353535; color: #E0E0E0; 
                        border: 1px solid #555; padding: 2px; 
                    }
                    QListWidget {
                        background-color: #252525; color: #E0E0E0;
                        border: 1px solid #555;
                    }

                    /* FIX: Make inputs dark to match the theme */
                    QComboBox, QDoubleSpinBox, QSpinBox {
                        background-color: #353535;
                        color: #E0E0E0;
                        border: 1px solid #555;
                        padding: 2px;
                    }

                    /* FIX: Make the dropdown list dark as well */
                    QComboBox QAbstractItemView {
                        background-color: #353535;
                        color: #E0E0E0;
                        selection-background-color: #505050;
                    }
                """)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Global Overrides Tab
        self.globals_tab = QWidget()
        self.setup_globals_ui()
        self.tabs.addTab(self.globals_tab, "Global Overrides")

        # Initials Tab
        self.initials_tab = QWidget()
        self.setup_initials_ui()
        self.tabs.addTab(self.initials_tab, "Initials")

        # Styles Tab
        self.styles_tab = QWidget()
        self.setup_styles_ui()
        self.tabs.addTab(self.styles_tab, "Styles")

        # Regions Tab
        self.regions_tab = QWidget()
        self.setup_regions_ui()
        self.tabs.addTab(self.regions_tab, "Regions")

        # Initialize UI State
        self.toggle_auto_color_state(self.gb_auto.isChecked())

    def load_project(self, project):
        """Called by MainWindow when a new project is ingested."""
        self.current_project = project

        # 1. Load Initials
        # If project has an initial_style, load it. Otherwise clear.
        if project.initial_style:
            self.populate_editor_form(self.initials_form, project.initial_style)
            self.initials_tab.setEnabled(True)
        else:
            self.initials_tab.setEnabled(False)

        # 2. Load Styles List
        self.list_styles.clear()
        if project.styles:
            for style_id in sorted(project.styles.keys()):
                self.list_styles.addItem(style_id)

        # 3. Load Regions List
        self.list_regions.clear()
        if project.regions:
            for region_id in sorted(project.regions.keys()):
                self.list_regions.addItem(region_id)

    def setup_globals_ui(self):
        layout = QFormLayout(self.globals_tab)

        # --- 1. GLOBAL PRESETS ---
        self.cmb_presets = QComboBox()
        # Full Config Presets
        self.preset_map = {
            "Standard (SDR)": ("#E5E5E5", 1.0),
            "Cinema (SDR)": ("#E0E0E0", 1.0),
            "HDR (Standard)": ("#B6B6B6", 0.90),
            "HDR (OLED Safe)": ("#808080", 0.90)
        }
        self.cmb_presets.addItems(["Custom"] + list(self.preset_map.keys()))
        self.cmb_presets.currentTextChanged.connect(self.apply_preset)
        layout.addRow("Global Preset:", self.cmb_presets)

        # --- 2. AUTO-COLOR SECTION ---
        self.gb_auto = QGroupBox("Auto-Color (Override based on Video Type)")
        self.gb_auto.setCheckable(True)
        self.gb_auto.setChecked(True)
        self.gb_auto.toggled.connect(self.emit_change)
        self.gb_auto.toggled.connect(self.toggle_auto_color_state)

        auto_layout = QFormLayout(self.gb_auto)

        # Define Color Presets (Name -> (Hex, Alpha))
        self.color_presets = {
            "SDR White 01": ("#E5E5E5", 1.0),
            "SDR Yellow 01": ("#FFEE8C", 1.0),
            "HDR Grey 01": ("#A1A1A1", 0.90),
            "HDR Grey 02": ("#808080", 0.90),
            "Custom": ("#FFFFFF", 1.0)
        }
        preset_names = list(self.color_presets.keys())

        # -- Helper to build Color Rows --
        def build_auto_row(label, default_preset):
            cmb = QComboBox()
            cmb.addItems(preset_names)
            cmb.setCurrentText(default_preset)

            btn_col = QPushButton()
            btn_col.setFixedWidth(80)

            lbl_swatch = QLabel()
            lbl_swatch.setFixedSize(20, 20)
            lbl_swatch.setStyleSheet("border: 1px solid #505050;")

            # Logic: Combo Change -> Update Button/Swatch
            def on_combo_change(text):
                if text in self.color_presets and text != "Custom":
                    c, a = self.color_presets[text]
                    btn_col.setText(c)
                    self.update_swatch(lbl_swatch, c)
                # If Custom, we leave the button as is (user edits it manually)
                self.emit_change()

            # Logic: Button Click -> Pick Color -> Set Combo to Custom
            def on_btn_click():
                curr = btn_col.text()
                c = QColorDialog.getColor(QColor(curr))
                if c.isValid():
                    hex_c = c.name().upper()
                    btn_col.setText(hex_c)
                    self.update_swatch(lbl_swatch, hex_c)

                    # Force combo to Custom without triggering reset
                    cmb.blockSignals(True)
                    cmb.setCurrentText("Custom")
                    cmb.blockSignals(False)
                    self.emit_change()

            cmb.currentTextChanged.connect(on_combo_change)
            btn_col.clicked.connect(on_btn_click)

            # Init Defaults
            def_c, def_a = self.color_presets[default_preset]
            btn_col.setText(def_c)
            self.update_swatch(lbl_swatch, def_c)

            row_layout = QHBoxLayout()
            row_layout.addWidget(cmb)
            row_layout.addWidget(btn_col)
            row_layout.addWidget(lbl_swatch)
            return cmb, btn_col, row_layout

        # SDR Row
        self.cmb_auto_sdr, self.btn_auto_sdr, row_sdr = build_auto_row("SDR Default:", "SDR White 01")
        auto_layout.addRow("SDR Default:", row_sdr)

        # HDR Row
        self.cmb_auto_hdr, self.btn_auto_hdr, row_hdr = build_auto_row("HDR Default:", "HDR Grey 01")
        auto_layout.addRow("HDR Default:", row_hdr)

        layout.addRow(self.gb_auto)

        # --- 3. MANUAL OVERRIDES ---

        # --- HELPER: Force Checkbox Indicator to Grey (instead of Blue) ---
        def apply_grey_style(chk):
            # FIX: Force "Fusion" style. Windows style ignores Palette colors.
            chk.setStyle(QStyleFactory.create("Fusion"))

            p = chk.palette()
            grey = QColor("#606060")  # Dark Grey

            # Set Highlight (Checked state background)
            p.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight, grey)
            p.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, grey)

            chk.setPalette(p)

        # Font
        self.chk_font = QCheckBox("Override Font")
        self.chk_font.toggled.connect(self.update_override_visuals)
        apply_grey_style(self.chk_font)
        self.spin_font_size = QDoubleSpinBox()
        self.spin_font_size.setValue(4.5)
        self.cmb_font_unit = QComboBox()
        self.cmb_font_unit.addItems(["vh", "px", "em", "%"])
        f_layout = QHBoxLayout();
        f_layout.addWidget(self.spin_font_size);
        f_layout.addWidget(self.cmb_font_unit)
        layout.addRow(self.chk_font, f_layout)

        # Color (Manual)
        self.chk_color = QCheckBox("Override Color")
        self.chk_color.toggled.connect(self.update_override_visuals)
        apply_grey_style(self.chk_color)
        self.btn_color = QPushButton("#FFFFFF")
        self.lbl_color_swatch = QLabel();
        self.lbl_color_swatch.setFixedSize(20, 20);
        self.lbl_color_swatch.setStyleSheet("border: 1px solid #505050; background-color: #FFFFFF;")

        self.btn_color.clicked.connect(lambda: self.pick_color(self.btn_color, self.lbl_color_swatch))

        c_layout = QHBoxLayout()
        c_layout.addWidget(self.btn_color)
        c_layout.addWidget(self.lbl_color_swatch)
        layout.addRow(self.chk_color, c_layout)

        # Outline
        self.chk_outline = QCheckBox("Override Outline")
        self.chk_outline.toggled.connect(self.update_override_visuals)
        apply_grey_style(self.chk_outline)
        self.chk_outline_enable = QCheckBox("Enabled")
        self.chk_outline_enable.setChecked(True)
        apply_grey_style(self.chk_outline_enable)
        self.spin_outline_width = QDoubleSpinBox();
        self.spin_outline_width.setValue(3.0)
        self.btn_outline_color = QPushButton("#000000")
        self.lbl_outline_swatch = QLabel();
        self.lbl_outline_swatch.setFixedSize(20, 20);
        self.lbl_outline_swatch.setStyleSheet("border: 1px solid #505050; background-color: #000000;")

        self.btn_outline_color.clicked.connect(lambda: self.pick_color(self.btn_outline_color, self.lbl_outline_swatch))

        o_layout = QHBoxLayout()
        o_layout.addWidget(self.chk_outline_enable)
        o_layout.addWidget(QLabel("Width:"))
        o_layout.addWidget(self.spin_outline_width)
        o_layout.addWidget(self.btn_outline_color)
        o_layout.addWidget(self.lbl_outline_swatch)
        layout.addRow(self.chk_outline, o_layout)

        # Shadow
        self.chk_shadow = QCheckBox("Override Shadow")
        self.chk_shadow.toggled.connect(self.update_override_visuals)
        apply_grey_style(self.chk_shadow)
        self.chk_shadow_enable = QCheckBox("Enabled")
        self.chk_shadow_enable.setChecked(True)
        apply_grey_style(self.chk_shadow_enable)
        self.spin_shadow_x = QDoubleSpinBox();
        self.spin_shadow_x.setValue(4.0)
        self.spin_shadow_y = QDoubleSpinBox();
        self.spin_shadow_y.setValue(4.0)
        self.spin_shadow_blur = QDoubleSpinBox();
        self.spin_shadow_blur.setValue(2.0)
        self.btn_shadow_color = QPushButton("#000000")
        self.lbl_shadow_swatch = QLabel();
        self.lbl_shadow_swatch.setFixedSize(20, 20);
        self.lbl_shadow_swatch.setStyleSheet("border: 1px solid #505050; background-color: #000000;")

        self.btn_shadow_color.clicked.connect(lambda: self.pick_color(self.btn_shadow_color, self.lbl_shadow_swatch))

        s_layout = QHBoxLayout()
        s_layout.addWidget(self.chk_shadow_enable)
        s_layout.addWidget(QLabel("X:"))
        s_layout.addWidget(self.spin_shadow_x)
        s_layout.addWidget(QLabel("Y:"))
        s_layout.addWidget(self.spin_shadow_y)
        s_layout.addWidget(QLabel("Blur:"))
        s_layout.addWidget(self.spin_shadow_blur)
        s_layout.addWidget(self.btn_shadow_color)
        s_layout.addWidget(self.lbl_shadow_swatch)
        layout.addRow(self.chk_shadow, s_layout)

        # Alpha
        self.spin_alpha = QDoubleSpinBox()
        self.spin_alpha.setRange(0.0, 1.0)
        self.spin_alpha.setSingleStep(0.05)
        self.spin_alpha.setValue(1.0)
        layout.addRow("Global Opacity:", self.spin_alpha)

        self.chk_force_16_9 = QCheckBox("Force 16:9 Layout (Ignore Video AR)")
        self.chk_force_16_9.setToolTip(
            "If checked, treats the source as 16:9. \nIf unchecked (Default), correctly pillars 4:3 content inside the 16:9 HD frame.")
        self.chk_force_16_9.setChecked(False)  # Default OFF (Smart Mode)
        apply_grey_style(self.chk_force_16_9)

        self.chk_force_16_9.toggled.connect(self.emit_change)
        layout.addRow(self.chk_force_16_9)

        # --- Override AR ---
        self.chk_override_ar = QCheckBox("Override Content Aspect Ratio")
        self.chk_override_ar.setToolTip(
            "Manually specify the aspect ratio for the content layout.\nUseful for 2.39:1 movies to ensure subtitles sit in the active video area.")
        self.chk_override_ar.toggled.connect(self.update_override_visuals)
        apply_grey_style(self.chk_override_ar)

        self.spin_ar_num = QDoubleSpinBox()
        self.spin_ar_num.setRange(0.01, 100000.0)
        self.spin_ar_num.setDecimals(3)
        self.spin_ar_num.setValue(1920.0)

        self.spin_ar_den = QDoubleSpinBox()
        self.spin_ar_den.setRange(0.01, 100000.0)
        self.spin_ar_den.setDecimals(3)
        self.spin_ar_den.setValue(800.0)

        ar_layout = QHBoxLayout()
        ar_layout.addWidget(self.spin_ar_num)
        ar_layout.addWidget(QLabel(":"))
        ar_layout.addWidget(self.spin_ar_den)

        layout.addRow(self.chk_override_ar, ar_layout)

        self.gb_post = QGroupBox("Post-Processing")
        # We use a simple vertical layout inside the group box
        post_layout = QVBoxLayout(self.gb_post)

        self.chk_remux = QCheckBox("Remux into Video on Completion")
        self.chk_remux.setToolTip(
            "If Checked:\n"
            "1. Run Current: Remuxes the .sup into the source video immediately.\n"
            "2. Run Batch: Waits for ALL jobs to finish, then groups subtitles by video\n"
            "   and remuxes them all into their respective videos at once."
        )
        self.chk_remux.setChecked(True)
        self.chk_remux.toggled.connect(self.emit_change)

        # Apply the visual style using your existing local helper
        apply_grey_style(self.chk_remux)

        post_layout.addWidget(self.chk_remux)

        self.chk_cleanup = QCheckBox("Clean-up Temp Files")
        self.chk_cleanup.setToolTip(
            "If Checked (Default):\n"
            "Automatically deletes the 'images' folder, 'slices_for_bdn' folder,\n"
            "and 'manifest.json' after a successful export."
        )
        self.chk_cleanup.setChecked(True)  # Default ON
        self.chk_cleanup.toggled.connect(self.emit_change)
        apply_grey_style(self.chk_cleanup)

        post_layout.addWidget(self.chk_cleanup)

        self.chk_move = QCheckBox("Move Files to '/subs' Folder")
        self.chk_move.setToolTip(
            "If Checked:\n"
            "Creates a 'subs' subfolder in the file's directory and moves\n"
            "both the original subtitle and the generated .sup into it."
        )
        self.chk_move.setChecked(True)
        self.chk_move.toggled.connect(self.emit_change)
        apply_grey_style(self.chk_move)

        post_layout.addWidget(self.chk_move)

        layout.addRow(self.gb_post)

        # Connect signals for basic widgets
        for w in [self.chk_font, self.chk_color, self.chk_outline, self.chk_shadow,
                  self.spin_font_size, self.cmb_font_unit, self.chk_outline_enable,
                  self.spin_outline_width, self.chk_shadow_enable, self.spin_shadow_x,
                  self.spin_shadow_y, self.spin_shadow_blur, self.spin_alpha,
                  self.spin_ar_num, self.spin_ar_den, self.chk_override_ar]:
            try:
                w.valueChanged.connect(self.emit_change)
            except:
                try:
                    w.stateChanged.connect(self.emit_change)
                except:
                    w.currentTextChanged.connect(self.emit_change)

    def setup_initials_ui(self):
        layout = QVBoxLayout(self.initials_tab)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.initials_form = QFormLayout(container)

        # Build the form rows for a Style object
        self.build_style_form(self.initials_form)

        scroll.setWidget(container)
        layout.addWidget(scroll)

    def setup_styles_ui(self):
        layout = QHBoxLayout(self.styles_tab)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: List of Styles
        self.list_styles = QListWidget()
        self.list_styles.currentRowChanged.connect(self.on_style_selected)

        # Right: Properties Form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.styles_form = QFormLayout(container)

        self.build_style_form(self.styles_form)  # Use same builder as Initials

        scroll.setWidget(container)

        splitter.addWidget(self.list_styles)
        splitter.addWidget(scroll)
        splitter.setSizes([200, 600])

        layout.addWidget(splitter)

    def build_style_form(self, layout):
        """Generates the comprehensive list of Style attributes."""

        # Helper to add row
        def add(label, attr, kind, options=None):
            editor = AttributeEditor(label, attr, kind, options)
            editor.value_changed.connect(lambda val, a=attr: self.update_current_object(a, val))
            layout.addRow(editor)
            # Store reference in the layout's parent widget for population later?
            # Actually better to store on the layout or a dict keyed by layout
            if not hasattr(layout, "editors"): layout.editors = {}
            layout.editors[attr] = editor

        # -- Fonts --
        add("Font Family (comma sep)", "font_family", "text_list")
        add("Font Size", "font_size", "float")
        add("Font Size Unit", "font_size_unit", "combo", ["vh", "px", "em", "%", "rh"])
        add("Font Weight", "font_weight", "combo", ["normal", "bold"])
        add("Font Style", "font_style", "combo", ["normal", "italic"])

        # -- Colors --
        add("Color", "color", "color")
        add("Background Color", "background_color", "color")
        add("Opacity (0.0 - 1.0)", "opacity", "float", {"min": 0.0, "max": 1.0, "step": 0.1})
        add("Show Background", "show_background", "combo", ["always", "whenActive"])

        # -- Layout --
        add("Origin X", "origin_x", "float")
        add("Origin X Unit", "origin_x_unit", "combo", ["%", "px", "vh", "vw"])
        add("Origin Y", "origin_y", "float")
        add("Origin Y Unit", "origin_y_unit", "combo", ["%", "px", "vh", "vw"])
        add("Extent Width", "extent_width", "float")
        add("Extent Width Unit", "extent_width_unit", "combo", ["%", "px", "vh", "vw"])
        add("Extent Height", "extent_height", "float")
        add("Extent Height Unit", "extent_height_unit", "combo", ["%", "px", "vh", "vw"])

        # -- Align / Writing --
        add("Writing Mode", "writing_mode", "combo", ["lrtb", "tblr", "vertical-rl", "lr", "rl", "tb"])
        add("Text Align", "text_align", "combo", ["left", "center", "right", "start", "end"])
        add("Display Align", "display_align", "combo", ["before", "center", "after"])
        add("MultiRow Align", "multi_row_align", "combo", ["start", "center", "auto"])

        # -- Effects --
        add("Outline Enabled", "outline_enabled", "bool")
        add("Outline Color", "outline_color", "color")
        add("Outline Width", "outline_width", "float")
        add("Outline Unit", "outline_unit", "combo", ["px", "%", "em"])

        add("Shadow Enabled", "shadow_enabled", "bool")
        add("Shadow Color", "shadow_color", "color")
        add("Shadow X", "shadow_offset_x", "float")
        add("Shadow Y", "shadow_offset_y", "float")
        add("Shadow Blur", "shadow_blur", "float")
        add("Shadow Unit", "shadow_unit", "combo", ["px", "%", "em"])

        add("Shear/Skew Angle", "skew_angle", "float")

        # -- Spacing / Ruby --
        add("Line Height", "line_height", "float")
        add("Line Height Unit", "line_height_unit", "combo", ["em", "px", "%"])
        add("Padding", "padding", "text")

        add("Ruby Role", "ruby_role", "combo", ["container", "base", "text", "delimiter"])
        add("Ruby Align", "ruby_align", "combo",
            ["center", "space-around", "start", "distribute-letter", "distribute-space"])
        add("Ruby Position", "ruby_position", "combo", ["over", "under"])

        add("Text Emphasis", "text_emphasis_style", "text")
        add("Text Emphasis Pos", "text_emphasis_position", "combo", ["over", "under", "before", "after"])
        add("Text Emphasis Color", "text_emphasis_color", "color")

    def on_style_selected(self, row):
        if not self.current_project: return
        item = self.list_styles.currentItem()
        if not item: return

        style_id = item.text()
        style = self.current_project.styles.get(style_id)
        if style:
            self.active_object = style  # Track what we are editing
            self.populate_editor_form(self.styles_form, style)

    def setup_regions_ui(self):
        layout = QHBoxLayout(self.regions_tab)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: List of Regions
        self.list_regions = QListWidget()
        self.list_regions.currentRowChanged.connect(self.on_region_selected)

        # Right: Properties Form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.regions_form = QFormLayout(container)

        self.build_region_form(self.regions_form)

        scroll.setWidget(container)

        splitter.addWidget(self.list_regions)
        splitter.addWidget(scroll)
        splitter.setSizes([200, 600])

        layout.addWidget(splitter)

    def build_region_form(self, layout):
        def add(label, attr, kind, options=None):
            editor = AttributeEditor(label, attr, kind, options)
            editor.value_changed.connect(lambda val, a=attr: self.update_current_object(a, val))
            layout.addRow(editor)
            if not hasattr(layout, "editors"): layout.editors = {}
            layout.editors[attr] = editor

        # -- Position --
        add("X", "x", "float")
        add("X Unit", "x_unit", "combo", ["%", "px", "vh", "vw"])
        add("X Edge (Anchor)", "x_edge", "combo", ["left", "right", "center"])

        add("Y", "y", "float")
        add("Y Unit", "y_unit", "combo", ["%", "px", "vh", "vw"])
        add("Y Edge (Anchor)", "y_edge", "combo", ["top", "bottom", "center"])

        # -- Size --
        add("Width", "width", "float")
        add("Width Unit", "width_unit", "combo", ["%", "px", "vh", "vw"])
        add("Height", "height", "float")
        add("Height Unit", "height_unit", "combo", ["%", "px", "vh", "vw"])

        # -- Align --
        add("Align Vertical", "align_vertical", "combo", ["top", "center", "bottom", "after", "before"])
        add("Align Horizontal", "align_horizontal", "combo", ["left", "center", "right", "start", "end"])

        # -- Visuals --
        add("Z Index", "z_index", "float", {"step": 1})
        add("Background Color", "background_color", "color")
        add("Opacity", "opacity", "float", {"min": 0.0, "max": 1.0})
        add("Show Background", "show_background", "combo", ["always", "whenActive"])
        add("Writing Mode", "writing_mode", "combo", ["lrtb", "tblr", "vertical-rl"])
        add("Is Vertical Flag", "is_vertical", "bool")

    def on_region_selected(self, row):
        if not self.current_project: return
        item = self.list_regions.currentItem()
        if not item: return

        region_id = item.text()
        region = self.current_project.regions.get(region_id)
        if region:
            self.active_object = region
            self.populate_editor_form(self.regions_form, region)

    # --- HELPERS ---
    def populate_editor_form(self, layout, obj):
        """Fills the editors in the given layout with values from obj."""
        if not hasattr(layout, "editors"): return

        # We set this active object so the 'value_changed' callbacks know what to update.
        # (For the Lists, it's set in on_selected. For Initials, we set it here).
        if layout == self.initials_form:
            self.active_object = obj

        for attr, editor in layout.editors.items():
            val = getattr(obj, attr, None)
            editor.set_value(val)

    def update_current_object(self, attr, value):
        """Callback from AttributeEditor."""
        if hasattr(self, 'active_object') and self.active_object:
            # print(f"[DEBUG] Setting {attr} = {value}")
            setattr(self.active_object, attr, value)
            self.emit_change()

    def update_swatch(self, lbl, hex_color):
        lbl.setStyleSheet(f"border: 1px solid #505050; background-color: {hex_color};")

    def pick_color(self, btn, lbl):
        curr = btn.text()
        c = QColorDialog.getColor(QColor(curr))
        if c.isValid():
            hex_c = c.name().upper()
            btn.setText(hex_c)
            self.update_swatch(lbl, hex_c)
            self.emit_change()

    def update_override_visuals(self):
        def set_visual_state(active, widgets):
            # Grey (#707070) if inactive, White (#E0E0E0) if active
            c = "#E0E0E0" if active else "#707070"
            for w in widgets:
                w.setStyleSheet(f"color: {c};")
                # Special handling for Swatches (keep bg color, dim border)
                if isinstance(w, QLabel) and "border" in w.styleSheet():
                    # Parse existing bg color to preserve it
                    bg = "transparent"
                    if "background-color:" in w.styleSheet():
                        bg = w.styleSheet().split("background-color:")[1].split(";")[0]
                    bc = "#505050" if active else "#303030"
                    w.setStyleSheet(f"border: 1px solid {bc}; background-color: {bg}; color: {c};")

        set_visual_state(self.chk_font.isChecked(), [self.spin_font_size, self.cmb_font_unit])

        # Only run Color update if it's enabled (Auto Mode not blocking it)
        if self.chk_color.isEnabled():
            set_visual_state(self.chk_color.isChecked(), [self.btn_color, self.lbl_color_swatch])

        set_visual_state(self.chk_outline.isChecked(),
                         [self.chk_outline_enable, self.spin_outline_width, self.btn_outline_color,
                          self.lbl_outline_swatch])

        set_visual_state(self.chk_shadow.isChecked(),
                         [self.chk_shadow_enable, self.spin_shadow_x, self.spin_shadow_y, self.spin_shadow_blur,
                          self.btn_shadow_color, self.lbl_shadow_swatch])

        set_visual_state(self.chk_override_ar.isChecked(), [self.spin_ar_num, self.spin_ar_den])

    def toggle_auto_color_state(self, checked):
        # When Auto-Color is ON, disable manual Color/Alpha controls
        enabled = not checked
        self.chk_color.setEnabled(enabled)
        self.btn_color.setEnabled(enabled)
        self.lbl_color_swatch.setEnabled(enabled)
        self.spin_alpha.setEnabled(enabled)
        self.update_override_visuals()

    def apply_preset(self, name):
        if name == "Custom": return
        if name not in self.preset_map: return

        color, alpha = self.preset_map[name]

        # Update UI
        self.chk_color.setChecked(True)
        self.btn_color.setText(color)
        self.update_swatch(self.lbl_color_swatch, color)
        self.spin_alpha.setValue(alpha)

        self.chk_outline.setChecked(True)
        self.chk_shadow.setChecked(True)
        self.emit_change()

    def get_preset_config(self, name):
        # Still used for full presets
        if name not in self.preset_map: return {}
        color, alpha = self.preset_map[name]
        base = self.get_overrides()
        base['override_color'] = True
        base['global_color'] = color
        base['global_alpha'] = alpha
        return base

    def emit_change(self):
        self.settings_changed.emit(self.get_overrides())

    def get_overrides(self):
        # Get Auto Alpha values based on preset map or default 1.0
        # Since the auto UI allows custom colors but the combo might hold the alpha info:

        # 1. Resolve SDR Alpha
        sdr_preset = self.cmb_auto_sdr.currentText()
        sdr_alpha = 1.0
        if sdr_preset in self.color_presets:
            sdr_alpha = self.color_presets[sdr_preset][1]

        # 2. Resolve HDR Alpha
        hdr_preset = self.cmb_auto_hdr.currentText()
        hdr_alpha = 1.0
        if hdr_preset in self.color_presets:
            hdr_alpha = self.color_presets[hdr_preset][1]

        return {
            "override_font_size": self.chk_font.isChecked(),
            "global_font_size": self.spin_font_size.value(),
            "global_font_size_unit": self.cmb_font_unit.currentText(),

            "override_color": self.chk_color.isChecked(),
            "global_color": self.btn_color.text(),

            "override_outline": self.chk_outline.isChecked(),
            "global_outline_enabled": self.chk_outline_enable.isChecked(),
            "global_outline_color": self.btn_outline_color.text(),
            "global_outline_width": self.spin_outline_width.value(),

            "override_shadow": self.chk_shadow.isChecked(),
            "global_shadow_enabled": self.chk_shadow_enable.isChecked(),
            "global_shadow_color": self.btn_shadow_color.text(),
            "global_shadow_offset_x": self.spin_shadow_x.value(),
            "global_shadow_offset_y": self.spin_shadow_y.value(),
            "global_shadow_blur": self.spin_shadow_blur.value(),

            "global_alpha": self.spin_alpha.value(),

            # --- AUTO COLOR EXPORTS ---
            "auto_color_enabled": self.gb_auto.isChecked(),

            # We export the actual values on the buttons/logic so MainWindow doesn't need to look them up
            "auto_sdr_color": self.btn_auto_sdr.text(),
            "auto_sdr_alpha": sdr_alpha,

            "auto_hdr_color": self.btn_auto_hdr.text(),
            "auto_hdr_alpha": hdr_alpha,

            "force_16_9": self.chk_force_16_9.isChecked(),

            "override_ar_enabled": self.chk_override_ar.isChecked(),
            "ar_num": self.spin_ar_num.value(),
            "ar_den": self.spin_ar_den.value(),

            "remux_enabled": self.chk_remux.isChecked(),

            "cleanup_enabled": self.chk_cleanup.isChecked(),

            "move_enabled": self.chk_move.isChecked()
        }


# =============================================================================
# HELPER: GENERIC ATTRIBUTE EDITOR
# =============================================================================
class AttributeEditor(QWidget):
    """
    A unified widget row that handles:
    1. A Checkbox to 'Set' the value (Handle None vs Value)
    2. Label
    3. The appropriate Editor Widget (SpinBox, Combo, ColorPicker, etc)
    """
    value_changed = pyqtSignal(object)

    def __init__(self, label_text, attr_name, kind, options=None):
        super().__init__()
        self.attr_name = attr_name
        self.kind = kind
        self.options = options
        self.block_updates = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 1. Checkbox ("Set")
        self.chk = QCheckBox(label_text)
        self.chk.setToolTip("Uncheck to inherit (None)")
        self.chk.stateChanged.connect(self._on_check_changed)
        layout.addWidget(self.chk, stretch=1)

        # 2. Editor Widget
        self.editor_widget = None

        if kind == "float" or kind == "float_0_1":
            self.editor_widget = QDoubleSpinBox()
            self.editor_widget.setRange(-9999.0, 9999.0)
            if kind == "float_0_1": self.editor_widget.setRange(0.0, 1.0); self.editor_widget.setSingleStep(0.1)

            # Apply options if any
            if options:
                if 'min' in options: self.editor_widget.setMinimum(options['min'])
                if 'max' in options: self.editor_widget.setMaximum(options['max'])
                if 'step' in options: self.editor_widget.setSingleStep(options['step'])

            self.editor_widget.valueChanged.connect(self._emit_val)

        elif kind == "combo":
            self.editor_widget = QComboBox()
            if isinstance(options, list):
                self.editor_widget.addItems(options)
            self.editor_widget.currentTextChanged.connect(self._emit_val)

        elif kind == "text":
            self.editor_widget = QLineEdit()
            self.editor_widget.textChanged.connect(self._emit_val)

        elif kind == "text_list":
            self.editor_widget = QLineEdit()
            self.editor_widget.setPlaceholderText("Arial, sans-serif")
            self.editor_widget.textChanged.connect(self._emit_val)

        elif kind == "color":
            self.editor_widget = QPushButton("#FFFFFF")
            self.editor_widget.clicked.connect(self._pick_color)

        elif kind == "bool":
            self.editor_widget = QCheckBox("True")
            self.editor_widget.toggled.connect(self._emit_val)

        if self.editor_widget:
            layout.addWidget(self.editor_widget, stretch=1)

        self.setLayout(layout)

    def set_value(self, val):
        self.block_updates = True
        if val is None:
            self.chk.setChecked(False)
            self.editor_widget.setEnabled(False)
        else:
            self.chk.setChecked(True)
            self.editor_widget.setEnabled(True)

            if self.kind == "float" or self.kind == "float_0_1":
                self.editor_widget.setValue(float(val))
            elif self.kind == "combo":
                self.editor_widget.setCurrentText(str(val))
            elif self.kind == "text":
                self.editor_widget.setText(str(val))
            elif self.kind == "text_list":
                if isinstance(val, list):
                    self.editor_widget.setText(", ".join(val))
                else:
                    self.editor_widget.setText(str(val))
            elif self.kind == "color":
                self.editor_widget.setText(str(val))
                self.editor_widget.setStyleSheet(f"background-color: {val}; color: #000000;")
            elif self.kind == "bool":
                self.editor_widget.setChecked(bool(val))

        self.block_updates = False

    def _on_check_changed(self, state):
        enabled = (state == Qt.CheckState.Checked.value)
        self.editor_widget.setEnabled(enabled)

        if not self.block_updates:
            if not enabled:
                self.value_changed.emit(None)
            else:
                # Emit current value of the widget
                self._emit_val()

    def _pick_color(self):
        curr = self.editor_widget.text()
        c = QColorDialog.getColor(QColor(curr))
        if c.isValid():
            hex_c = c.name().upper()
            self.editor_widget.setText(hex_c)
            self.editor_widget.setStyleSheet(f"background-color: {hex_c}; color: #000000;")
            self._emit_val()

    def _emit_val(self, *args):
        if self.block_updates: return
        if not self.chk.isChecked(): return  # Should be blocked by None check, but safety

        val = None
        if self.kind in ["float", "float_0_1"]:
            val = self.editor_widget.value()
        elif self.kind == "combo":
            val = self.editor_widget.currentText()
        elif self.kind == "text":
            val = self.editor_widget.text()
        elif self.kind == "text_list":
            txt = self.editor_widget.text()
            val = [x.strip() for x in txt.split(',') if x.strip()]
        elif self.kind == "color":
            val = self.editor_widget.text()
        elif self.kind == "bool":
            val = self.editor_widget.isChecked()

        self.value_changed.emit(val)