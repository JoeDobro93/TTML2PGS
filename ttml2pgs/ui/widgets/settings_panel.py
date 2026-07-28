"""
Settings pane.

Tabs:
* **Overrides** — per-language global overrides (font size/family, color,
  outline, shadow, opacity, line height) with a language tab strip, plus
  render-target layout options (canvas policy, content AR, safe-area
  padding) and post-processing toggles.
* **Styles / Regions / Initial** — live editors for the active document's
  named styles, regions and document defaults. Edits re-cascade into the
  preview instantly (styles stay referenced, never baked).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                             QDoubleSpinBox, QFormLayout, QGroupBox,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QPushButton, QScrollArea,
                             QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ...core.colors import parse_color, to_hex
from ...core.model import Region, Shadow, Style, SubtitleDocument
from ...core.overrides import LayoutOptions, OverrideSet, StyleOverrides
from ...core.units import Dim


# --------------------------------------------------------------------------- #
# small reusable editors
# --------------------------------------------------------------------------- #

class ColorButton(QPushButton):
    changed = pyqtSignal()

    def __init__(self, color=(255, 255, 255, 255)):
        super().__init__()
        self._color = color
        self.clicked.connect(self._pick)
        self._sync()

    def color(self):
        return self._color

    def set_color(self, c):
        if c is not None:
            self._color = c
            self._sync()

    def _sync(self):
        hexc = to_hex(self._color)
        r, g, b = self._color[:3]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        fg = '#000' if lum > 140 else '#fff'
        self.setText(hexc)
        self.setStyleSheet(
            f'background:{to_hex(self._color, False)}; color:{fg};')

    def _pick(self):
        qc = QColorDialog.getColor(
            QColor(*self._color),
            options=QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if qc.isValid():
            self._color = (qc.red(), qc.green(), qc.blue(), qc.alpha())
            self._sync()
            self.changed.emit()


class DimEdit(QWidget):
    """Value spinbox + unit combo bound to a Dim."""
    changed = pyqtSignal()

    UNITS = ['vh', 'vw', 'px', 'em', '%', 'c', '']

    def __init__(self, dim: Dim = Dim(4.5, 'vh'), units=None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-10000, 10000)
        self.spin.setDecimals(3)
        self.spin.setSingleStep(0.25)
        self.cmb = QComboBox()
        self.cmb.addItems(units or self.UNITS)
        lay.addWidget(self.spin, 1)
        lay.addWidget(self.cmb)
        self.set_dim(dim)
        self.spin.valueChanged.connect(lambda *_: self.changed.emit())
        self.cmb.currentTextChanged.connect(lambda *_: self.changed.emit())

    def set_dim(self, d: Optional[Dim]):
        if d is None:
            return
        self.spin.blockSignals(True)
        self.cmb.blockSignals(True)
        self.spin.setValue(d.value)
        if self.cmb.findText(d.unit) < 0:
            self.cmb.addItem(d.unit)
        self.cmb.setCurrentText(d.unit)
        self.spin.blockSignals(False)
        self.cmb.blockSignals(False)

    def dim(self) -> Dim:
        return Dim(self.spin.value(), self.cmb.currentText())


# --------------------------------------------------------------------------- #
# per-language override editor
# --------------------------------------------------------------------------- #

class OverrideEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, so: StyleOverrides):
        super().__init__()
        self.so = so
        form = QFormLayout(self)
        form.setContentsMargins(6, 6, 6, 6)

        # font size
        self.chk_size = QCheckBox('Override font size')
        self.ed_size = DimEdit(so.font_size)
        form.addRow(self.chk_size, self.ed_size)
        # family
        self.chk_family = QCheckBox('Override font family')
        self.ed_family = QLineEdit(', '.join(so.font_family))
        self.ed_family.setPlaceholderText('e.g. Noto Sans CJK JP, sans-serif')
        form.addRow(self.chk_family, self.ed_family)
        # color
        self.chk_color = QCheckBox('Override color')
        self.btn_color = ColorButton(so.color)
        form.addRow(self.chk_color, self.btn_color)
        # outline
        self.chk_outline = QCheckBox('Override outline')
        row_o = QHBoxLayout()
        self.chk_outline_on = QCheckBox('on')
        self.ed_outline_w = DimEdit(so.outline_width, ['px', 'em', 'vh', '%'])
        self.btn_outline_c = ColorButton(so.outline_color)
        row_o.addWidget(self.chk_outline_on)
        row_o.addWidget(QLabel('W:'))
        row_o.addWidget(self.ed_outline_w, 1)
        row_o.addWidget(self.btn_outline_c)
        w_o = QWidget()
        w_o.setLayout(row_o)
        form.addRow(self.chk_outline, w_o)
        # shadow
        self.chk_shadow = QCheckBox('Override shadow')
        row_s = QHBoxLayout()
        self.chk_shadow_on = QCheckBox('on')
        self.ed_sx = DimEdit(so.shadow_offset_x, ['px', 'em', 'vh'])
        self.ed_sy = DimEdit(so.shadow_offset_y, ['px', 'em', 'vh'])
        self.ed_sb = DimEdit(so.shadow_blur, ['px', 'em', 'vh'])
        self.btn_shadow_c = ColorButton(so.shadow_color)
        self.spin_salpha = QDoubleSpinBox()
        self.spin_salpha.setRange(0, 1)
        self.spin_salpha.setSingleStep(0.05)
        self.spin_salpha.setValue(so.shadow_alpha)
        for lbl, w in (('on', self.chk_shadow_on), ('X', self.ed_sx),
                       ('Y', self.ed_sy), ('Blur', self.ed_sb)):
            if lbl != 'on':
                row_s.addWidget(QLabel(lbl + ':'))
            row_s.addWidget(w)
        row_s.addWidget(self.btn_shadow_c)
        row_s.addWidget(QLabel('α:'))
        row_s.addWidget(self.spin_salpha)
        w_s = QWidget()
        w_s.setLayout(row_s)
        form.addRow(self.chk_shadow, w_s)
        # line height
        self.chk_lh = QCheckBox('Override line height')
        self.ed_lh = DimEdit(so.line_height, ['', 'em', 'px', 'vh', '%'])
        form.addRow(self.chk_lh, self.ed_lh)
        # global alpha
        self.spin_alpha = QDoubleSpinBox()
        self.spin_alpha.setRange(0, 1)
        self.spin_alpha.setSingleStep(0.05)
        self.spin_alpha.setValue(so.opacity_mult)
        form.addRow('Global opacity:', self.spin_alpha)

        self._load_flags()
        for w in (self.chk_size, self.chk_family, self.chk_color,
                  self.chk_outline, self.chk_outline_on, self.chk_shadow,
                  self.chk_shadow_on, self.chk_lh):
            w.toggled.connect(self._commit)
        for w in (self.ed_size, self.ed_outline_w, self.ed_sx, self.ed_sy,
                  self.ed_sb, self.ed_lh):
            w.changed.connect(self._commit)
        for w in (self.btn_color, self.btn_outline_c, self.btn_shadow_c):
            w.changed.connect(self._commit)
        self.ed_family.editingFinished.connect(self._commit)
        self.spin_alpha.valueChanged.connect(self._commit)
        self.spin_salpha.valueChanged.connect(self._commit)

    def _load_flags(self):
        so = self.so
        self.chk_size.setChecked(so.override_font_size)
        self.chk_family.setChecked(so.override_font_family)
        self.chk_color.setChecked(so.override_color)
        self.chk_outline.setChecked(so.override_outline)
        self.chk_outline_on.setChecked(so.outline_enabled)
        self.chk_shadow.setChecked(so.override_shadow)
        self.chk_shadow_on.setChecked(so.shadow_enabled)
        self.chk_lh.setChecked(so.override_line_height)

    def _commit(self, *_):
        so = self.so
        so.override_font_size = self.chk_size.isChecked()
        so.font_size = self.ed_size.dim()
        so.override_font_family = self.chk_family.isChecked()
        so.font_family = [f.strip() for f in self.ed_family.text().split(',')
                          if f.strip()] or ['sans-serif']
        so.override_color = self.chk_color.isChecked()
        so.color = self.btn_color.color()
        so.override_outline = self.chk_outline.isChecked()
        so.outline_enabled = self.chk_outline_on.isChecked()
        so.outline_width = self.ed_outline_w.dim()
        so.outline_color = self.btn_outline_c.color()
        so.override_shadow = self.chk_shadow.isChecked()
        so.shadow_enabled = self.chk_shadow_on.isChecked()
        so.shadow_offset_x = self.ed_sx.dim()
        so.shadow_offset_y = self.ed_sy.dim()
        so.shadow_blur = self.ed_sb.dim()
        so.shadow_color = self.btn_shadow_c.color()
        so.shadow_alpha = self.spin_salpha.value()
        so.override_line_height = self.chk_lh.isChecked()
        so.line_height = self.ed_lh.dim()
        so.opacity_mult = self.spin_alpha.value()
        self.changed.emit()


class LayoutOptionsEditor(QGroupBox):
    changed = pyqtSignal()

    def __init__(self, lo: LayoutOptions):
        super().__init__('Layout / canvas')
        self.lo = lo
        form = QFormLayout(self)
        self.chk_vidims = QCheckBox('Canvas = video dimensions')
        self.chk_vidims.setToolTip(
            'Output .sup canvas matches the target video instead of '
            '1920x1080.')
        self.chk_hd = QCheckBox('…scaled to fit 1920x1080')
        self.chk_169 = QCheckBox('Force 16:9 layout (ignore video AR)')
        self.chk_ar = QCheckBox('Override content aspect ratio')
        row = QHBoxLayout()
        self.spin_arw = QDoubleSpinBox()
        self.spin_arw.setRange(0.1, 10000)
        self.spin_arw.setDecimals(3)
        self.spin_arw.setValue(lo.ar_w)
        self.spin_arh = QDoubleSpinBox()
        self.spin_arh.setRange(0.1, 10000)
        self.spin_arh.setDecimals(3)
        self.spin_arh.setValue(lo.ar_h)
        row.addWidget(self.spin_arw)
        row.addWidget(QLabel(':'))
        row.addWidget(self.spin_arh)
        arw = QWidget()
        arw.setLayout(row)
        self.chk_pad = QCheckBox('Safe-area padding')
        rowp = QHBoxLayout()
        self.spin_pv = QDoubleSpinBox()
        self.spin_pv.setRange(0, 40)
        self.spin_pv.setSuffix(' %V')
        self.spin_pv.setValue(lo.padding_v)
        self.spin_ph = QDoubleSpinBox()
        self.spin_ph.setRange(0, 40)
        self.spin_ph.setSuffix(' %H')
        self.spin_ph.setValue(lo.padding_h)
        rowp.addWidget(self.spin_pv)
        rowp.addWidget(self.spin_ph)
        padw = QWidget()
        padw.setLayout(rowp)
        form.addRow(self.chk_vidims)
        form.addRow('', self.chk_hd)
        form.addRow(self.chk_169)
        form.addRow(self.chk_ar, arw)
        form.addRow(self.chk_pad, padw)
        self._load()
        for w in (self.chk_vidims, self.chk_hd, self.chk_169, self.chk_ar,
                  self.chk_pad):
            w.toggled.connect(self._commit)
        for w in (self.spin_arw, self.spin_arh, self.spin_pv, self.spin_ph):
            w.valueChanged.connect(self._commit)

    def _load(self):
        lo = self.lo
        self.chk_vidims.setChecked(lo.use_video_dims)
        self.chk_hd.setChecked(lo.scale_to_hd)
        self.chk_169.setChecked(lo.force_16_9)
        self.chk_ar.setChecked(lo.override_ar)
        self.chk_pad.setChecked(lo.use_padding)

    def _commit(self, *_):
        lo = self.lo
        lo.use_video_dims = self.chk_vidims.isChecked()
        lo.scale_to_hd = self.chk_hd.isChecked()
        lo.force_16_9 = self.chk_169.isChecked()
        lo.override_ar = self.chk_ar.isChecked()
        lo.ar_w = self.spin_arw.value()
        lo.ar_h = self.spin_arh.value()
        lo.use_padding = self.chk_pad.isChecked()
        lo.padding_v = self.spin_pv.value()
        lo.padding_h = self.spin_ph.value()
        self.changed.emit()


# --------------------------------------------------------------------------- #
# Style editor (named styles / initial)
# --------------------------------------------------------------------------- #

class StyleEditor(QWidget):
    """Property editor over the most-used Style fields. Unchecked = None."""
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.style: Optional[Style] = None
        self._loading = False
        form = QFormLayout(self)
        form.setContentsMargins(4, 4, 4, 4)

        def add_row(label: str):
            chk = QCheckBox(label)
            return chk

        # font
        self.c_family = add_row('Font family')
        self.e_family = QLineEdit()
        form.addRow(self.c_family, self.e_family)
        self.c_size = add_row('Font size')
        self.e_size = DimEdit(Dim(100, '%'))
        form.addRow(self.c_size, self.e_size)
        self.c_weight = add_row('Weight')
        self.e_weight = QComboBox()
        self.e_weight.addItems(['normal', 'bold'])
        form.addRow(self.c_weight, self.e_weight)
        self.c_style = add_row('Style')
        self.e_style = QComboBox()
        self.e_style.addItems(['normal', 'italic', 'oblique'])
        form.addRow(self.c_style, self.e_style)
        # colors
        self.c_color = add_row('Color')
        self.e_color = ColorButton()
        form.addRow(self.c_color, self.e_color)
        self.c_bg = add_row('Background')
        self.e_bg = ColorButton((0, 0, 0, 0))
        form.addRow(self.c_bg, self.e_bg)
        # outline / shadow
        self.c_outline = add_row('Outline')
        row_o = QHBoxLayout()
        self.e_outline_w = DimEdit(Dim(2.7, 'px'), ['px', 'em', 'vh', '%'])
        self.e_outline_c = ColorButton((0, 0, 0, 255))
        row_o.addWidget(self.e_outline_w, 1)
        row_o.addWidget(self.e_outline_c)
        wo = QWidget()
        wo.setLayout(row_o)
        form.addRow(self.c_outline, wo)
        self.c_shadow = add_row('Shadow')
        row_s = QHBoxLayout()
        self.e_sx = DimEdit(Dim(2, 'px'), ['px', 'em', 'vh'])
        self.e_sy = DimEdit(Dim(2, 'px'), ['px', 'em', 'vh'])
        self.e_sb = DimEdit(Dim(2, 'px'), ['px', 'em', 'vh'])
        self.e_sc = ColorButton((0, 0, 0, 255))
        for w in (self.e_sx, self.e_sy, self.e_sb):
            row_s.addWidget(w)
        row_s.addWidget(self.e_sc)
        ws = QWidget()
        ws.setLayout(row_s)
        form.addRow(self.c_shadow, ws)
        # alignment / layout
        self.c_talign = add_row('Text align')
        self.e_talign = QComboBox()
        self.e_talign.addItems(['start', 'center', 'end', 'left', 'right'])
        form.addRow(self.c_talign, self.e_talign)
        self.c_dalign = add_row('Display align')
        self.e_dalign = QComboBox()
        self.e_dalign.addItems(['before', 'center', 'after'])
        form.addRow(self.c_dalign, self.e_dalign)
        self.c_mra = add_row('Multi-row align')
        self.e_mra = QComboBox()
        self.e_mra.addItems(['start', 'center', 'end'])
        form.addRow(self.c_mra, self.e_mra)
        self.c_lh = add_row('Line height')
        self.e_lh = DimEdit(Dim(1.25, ''), ['', 'em', 'px', '%', 'vh'])
        form.addRow(self.c_lh, self.e_lh)
        self.c_wm = add_row('Writing mode')
        self.e_wm = QComboBox()
        self.e_wm.addItems(['lrtb', 'tbrl', 'tblr'])
        form.addRow(self.c_wm, self.e_wm)
        self.c_shear = add_row('Shear (deg)')
        self.e_shear = QDoubleSpinBox()
        self.e_shear.setRange(-45, 45)
        self.e_shear.setDecimals(2)
        form.addRow(self.c_shear, self.e_shear)
        # ruby / emphasis
        self.c_ruby = add_row('Ruby role')
        self.e_ruby = QComboBox()
        self.e_ruby.addItems(['container', 'base', 'text', 'baseContainer',
                              'textContainer', 'delimiter'])
        form.addRow(self.c_ruby, self.e_ruby)
        self.c_rpos = add_row('Ruby position')
        self.e_rpos = QComboBox()
        self.e_rpos.addItems(['before', 'after'])
        form.addRow(self.c_rpos, self.e_rpos)
        self.c_emph = add_row('Text emphasis')
        self.e_emph = QComboBox()
        self.e_emph.addItems(['filled dot', 'open dot', 'filled circle',
                              'open circle', 'filled sesame'])
        self.e_emph.setEditable(True)
        form.addRow(self.c_emph, self.e_emph)
        self.c_tcy = add_row('Text combine')
        self.e_tcy = QComboBox()
        self.e_tcy.addItems(['none', 'all'])
        form.addRow(self.c_tcy, self.e_tcy)

        self._rows = [
            (self.c_family, self.e_family), (self.c_size, self.e_size),
            (self.c_weight, self.e_weight), (self.c_style, self.e_style),
            (self.c_color, self.e_color), (self.c_bg, self.e_bg),
            (self.c_outline, wo), (self.c_shadow, ws),
            (self.c_talign, self.e_talign), (self.c_dalign, self.e_dalign),
            (self.c_mra, self.e_mra), (self.c_lh, self.e_lh),
            (self.c_wm, self.e_wm), (self.c_shear, self.e_shear),
            (self.c_ruby, self.e_ruby), (self.c_rpos, self.e_rpos),
            (self.c_emph, self.e_emph), (self.c_tcy, self.e_tcy),
        ]
        for chk, _ in self._rows:
            chk.toggled.connect(self._commit)
        for w in (self.e_size, self.e_outline_w, self.e_sx, self.e_sy,
                  self.e_sb, self.e_lh):
            w.changed.connect(self._commit)
        for w in (self.e_color, self.e_bg, self.e_outline_c, self.e_sc):
            w.changed.connect(self._commit)
        for w in (self.e_weight, self.e_style, self.e_talign, self.e_dalign,
                  self.e_mra, self.e_wm, self.e_ruby, self.e_rpos,
                  self.e_emph, self.e_tcy):
            w.currentTextChanged.connect(self._commit)
        self.e_family.editingFinished.connect(self._commit)
        self.e_shear.valueChanged.connect(self._commit)

    # ------------------------------------------------------------------ #
    def load(self, style: Optional[Style]):
        self._loading = True
        self.style = style
        s = style or Style()
        self.c_family.setChecked(s.font_family is not None)
        if s.font_family:
            self.e_family.setText(', '.join(s.font_family))
        self.c_size.setChecked(s.font_size is not None)
        if s.font_size:
            self.e_size.set_dim(s.font_size)
        self.c_weight.setChecked(s.font_weight is not None)
        if s.font_weight:
            self.e_weight.setCurrentText(s.font_weight)
        self.c_style.setChecked(s.font_style is not None)
        if s.font_style:
            self.e_style.setCurrentText(s.font_style)
        self.c_color.setChecked(s.color is not None)
        self.e_color.set_color(s.color)
        self.c_bg.setChecked(s.background_color is not None)
        self.e_bg.set_color(s.background_color)
        self.c_outline.setChecked(s.outline_width is not None)
        if s.outline_width:
            self.e_outline_w.set_dim(s.outline_width)
        self.e_outline_c.set_color(s.outline_color)
        self.c_shadow.setChecked(s.shadows is not None)
        if s.shadows:
            sh = s.shadows[0]
            self.e_sx.set_dim(sh.offset_x)
            self.e_sy.set_dim(sh.offset_y)
            self.e_sb.set_dim(sh.blur)
            self.e_sc.set_color(sh.color)
        self.c_talign.setChecked(s.text_align is not None)
        if s.text_align:
            self.e_talign.setCurrentText(s.text_align)
        self.c_dalign.setChecked(s.display_align is not None)
        if s.display_align:
            self.e_dalign.setCurrentText(s.display_align)
        self.c_mra.setChecked(s.multi_row_align is not None)
        if s.multi_row_align:
            self.e_mra.setCurrentText(s.multi_row_align)
        self.c_lh.setChecked(s.line_height is not None)
        if s.line_height:
            self.e_lh.set_dim(s.line_height)
        self.c_wm.setChecked(s.writing_mode is not None)
        if s.writing_mode:
            self.e_wm.setCurrentText(s.writing_mode)
        self.c_shear.setChecked(s.shear is not None)
        if s.shear is not None:
            self.e_shear.setValue(s.shear)
        self.c_ruby.setChecked(s.ruby is not None)
        if s.ruby:
            self.e_ruby.setCurrentText(s.ruby)
        self.c_rpos.setChecked(s.ruby_position is not None)
        if s.ruby_position:
            self.e_rpos.setCurrentText(s.ruby_position)
        self.c_emph.setChecked(s.text_emphasis_style is not None)
        if s.text_emphasis_style:
            self.e_emph.setCurrentText(s.text_emphasis_style)
        self.c_tcy.setChecked(s.text_combine is not None)
        if s.text_combine:
            self.e_tcy.setCurrentText(s.text_combine)
        self._loading = False

    def _commit(self, *_):
        if self._loading or self.style is None:
            return
        s = self.style
        s.font_family = [f.strip() for f in self.e_family.text().split(',')
                         if f.strip()] if self.c_family.isChecked() else None
        s.font_size = self.e_size.dim() if self.c_size.isChecked() else None
        s.font_weight = self.e_weight.currentText() \
            if self.c_weight.isChecked() else None
        s.font_style = self.e_style.currentText() \
            if self.c_style.isChecked() else None
        s.color = self.e_color.color() if self.c_color.isChecked() else None
        s.background_color = self.e_bg.color() if self.c_bg.isChecked() \
            else None
        if self.c_outline.isChecked():
            s.outline_width = self.e_outline_w.dim()
            s.outline_color = self.e_outline_c.color()
        else:
            s.outline_width = None
            s.outline_color = None
        if self.c_shadow.isChecked():
            s.shadows = [Shadow(self.e_sx.dim(), self.e_sy.dim(),
                                self.e_sb.dim(), self.e_sc.color())]
        else:
            s.shadows = None
        s.text_align = self.e_talign.currentText() \
            if self.c_talign.isChecked() else None
        s.display_align = self.e_dalign.currentText() \
            if self.c_dalign.isChecked() else None
        s.multi_row_align = self.e_mra.currentText() \
            if self.c_mra.isChecked() else None
        s.line_height = self.e_lh.dim() if self.c_lh.isChecked() else None
        s.writing_mode = self.e_wm.currentText() \
            if self.c_wm.isChecked() else None
        s.shear = self.e_shear.value() if self.c_shear.isChecked() else None
        s.ruby = self.e_ruby.currentText() if self.c_ruby.isChecked() else None
        s.ruby_position = self.e_rpos.currentText() \
            if self.c_rpos.isChecked() else None
        s.text_emphasis_style = self.e_emph.currentText() \
            if self.c_emph.isChecked() else None
        s.text_combine = self.e_tcy.currentText() \
            if self.c_tcy.isChecked() else None
        self.changed.emit()


# --------------------------------------------------------------------------- #
# Region editor
# --------------------------------------------------------------------------- #

class RegionEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.region: Optional[Region] = None
        self._loading = False
        form = QFormLayout(self)
        self.e_x = DimEdit(Dim(50, '%'), ['%', 'px', 'vw', 'vh'])
        self.e_xe = QComboBox()
        self.e_xe.addItems(['left', 'right', 'center', 'point'])
        rowx = QHBoxLayout()
        rowx.addWidget(self.e_x, 1)
        rowx.addWidget(self.e_xe)
        wx = QWidget()
        wx.setLayout(rowx)
        form.addRow('X anchor:', wx)
        self.e_y = DimEdit(Dim(90, '%'), ['%', 'px', 'vh', 'vw'])
        self.e_ye = QComboBox()
        self.e_ye.addItems(['top', 'bottom', 'center', 'point'])
        rowy = QHBoxLayout()
        rowy.addWidget(self.e_y, 1)
        rowy.addWidget(self.e_ye)
        wy = QWidget()
        wy.setLayout(rowy)
        form.addRow('Y anchor:', wy)
        self.c_w = QCheckBox('Width')
        self.e_w = DimEdit(Dim(90, '%'), ['%', 'px', 'vw'])
        form.addRow(self.c_w, self.e_w)
        self.c_h = QCheckBox('Height')
        self.e_h = DimEdit(Dim(20, '%'), ['%', 'px', 'vh'])
        form.addRow(self.c_h, self.e_h)
        self.style_editor = StyleEditor()
        box = QGroupBox('Region style (alignment, writing mode, bg…)')
        bl = QVBoxLayout(box)
        bl.addWidget(self.style_editor)
        form.addRow(box)

        for w in (self.e_x, self.e_y, self.e_w, self.e_h):
            w.changed.connect(self._commit)
        for w in (self.e_xe, self.e_ye):
            w.currentTextChanged.connect(self._commit)
        for w in (self.c_w, self.c_h):
            w.toggled.connect(self._commit)
        self.style_editor.changed.connect(self.changed.emit)

    def load(self, region: Optional[Region]):
        self._loading = True
        self.region = region
        r = region or Region()
        self.e_x.set_dim(r.x)
        self.e_xe.setCurrentText(r.x_edge)
        self.e_y.set_dim(r.y)
        self.e_ye.setCurrentText(r.y_edge)
        self.c_w.setChecked(r.width is not None)
        if r.width:
            self.e_w.set_dim(r.width)
        self.c_h.setChecked(r.height is not None)
        if r.height:
            self.e_h.set_dim(r.height)
        self.style_editor.load(region.style if region else None)
        self._loading = False

    def _commit(self, *_):
        if self._loading or self.region is None:
            return
        r = self.region
        r.x = self.e_x.dim()
        r.x_edge = self.e_xe.currentText()
        r.y = self.e_y.dim()
        r.y_edge = self.e_ye.currentText()
        r.width = self.e_w.dim() if self.c_w.isChecked() else None
        r.height = self.e_h.dim() if self.c_h.isChecked() else None
        self.changed.emit()


# --------------------------------------------------------------------------- #
# The pane
# --------------------------------------------------------------------------- #

class SettingsPane(QWidget):
    overrides_changed = pyqtSignal()
    document_changed = pyqtSignal()

    def __init__(self, overrides: OverrideSet, app_settings: dict):
        super().__init__()
        self.overrides = overrides
        self.app_settings = app_settings
        self.doc: Optional[SubtitleDocument] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)

        # ---- overrides tab ------------------------------------------- #
        ov_tab = QWidget()
        ovl = QVBoxLayout(ov_tab)
        self.lang_tabs = QTabWidget()
        self.lang_tabs.setTabsClosable(True)
        self.lang_tabs.tabCloseRequested.connect(self._close_lang_tab)
        btn_add_lang = QPushButton('+')
        btn_add_lang.setFixedWidth(28)
        btn_add_lang.setToolTip('Add a language-specific override set')
        btn_add_lang.clicked.connect(self._add_lang_tab)
        self.lang_tabs.setCornerWidget(btn_add_lang)
        ovl.addWidget(self.lang_tabs)
        self.layout_editor = LayoutOptionsEditor(self.overrides.layout)
        self.layout_editor.changed.connect(self.overrides_changed.emit)
        ovl.addWidget(self.layout_editor)

        post = QGroupBox('Post-processing')
        pl = QVBoxLayout(post)
        self.chk_remux = QCheckBox('Remux into video when its renders finish')
        self.chk_remux.setChecked(app_settings.get('remux_after_render', True))
        self.chk_replace = QCheckBox('Replace original video (else *.muxed.mkv)')
        self.chk_replace.setChecked(app_settings.get('replace_original', True))
        self.chk_move = QCheckBox("Move sources into a 'subs' subfolder after mux")
        self.chk_move.setChecked(app_settings.get('move_to_subs_folder', False))
        for w in (self.chk_remux, self.chk_replace, self.chk_move):
            pl.addWidget(w)
            w.toggled.connect(self._post_changed)
        ovl.addWidget(post)

        player = QGroupBox('External player')
        fl = QFormLayout(player)
        self.ed_player = QLineEdit(app_settings.get('external_player', ''))
        self.ed_player.setPlaceholderText(r'e.g. C:\Program Files\MPC-BE\mpc-be64.exe')
        self.ed_player_args = QLineEdit(
            app_settings.get('external_player_args', '"{file}" /start {ms}'))
        fl.addRow('Executable:', self.ed_player)
        fl.addRow('Arguments:', self.ed_player_args)
        self.ed_player.editingFinished.connect(self._post_changed)
        self.ed_player_args.editingFinished.connect(self._post_changed)
        ovl.addWidget(player)
        ovl.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(ov_tab)
        self.tabs.addTab(scroll, 'Global overrides')

        # ---- styles tab ---------------------------------------------- #
        st_tab = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(2, 2, 2, 2)
        self.style_list = QListWidget()
        ll.addWidget(self.style_list)
        rowb = QHBoxLayout()
        b_add = QPushButton('Add')
        b_del = QPushButton('Delete')
        rowb.addWidget(b_add)
        rowb.addWidget(b_del)
        ll.addLayout(rowb)
        st_tab.addWidget(left)
        self.style_editor = StyleEditor()
        sscroll = QScrollArea()
        sscroll.setWidgetResizable(True)
        sscroll.setWidget(self.style_editor)
        st_tab.addWidget(sscroll)
        st_tab.setSizes([160, 420])
        self.tabs.addTab(st_tab, 'Styles')
        b_add.clicked.connect(self._add_style)
        b_del.clicked.connect(self._del_style)
        self.style_list.currentTextChanged.connect(self._style_selected)
        self.style_editor.changed.connect(self.document_changed.emit)

        # ---- regions tab --------------------------------------------- #
        rg_tab = QSplitter(Qt.Orientation.Horizontal)
        leftr = QWidget()
        rl = QVBoxLayout(leftr)
        rl.setContentsMargins(2, 2, 2, 2)
        self.region_list = QListWidget()
        rl.addWidget(self.region_list)
        rowrb = QHBoxLayout()
        rb_add = QPushButton('Add')
        rb_del = QPushButton('Delete')
        rowrb.addWidget(rb_add)
        rowrb.addWidget(rb_del)
        rl.addLayout(rowrb)
        rg_tab.addWidget(leftr)
        self.region_editor = RegionEditor()
        rscroll = QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setWidget(self.region_editor)
        rg_tab.addWidget(rscroll)
        rg_tab.setSizes([160, 420])
        self.tabs.addTab(rg_tab, 'Regions')
        rb_add.clicked.connect(self._add_region)
        rb_del.clicked.connect(self._del_region)
        self.region_list.currentTextChanged.connect(self._region_selected)
        self.region_editor.changed.connect(self.document_changed.emit)

        # ---- initial tab --------------------------------------------- #
        self.initial_editor = StyleEditor()
        iscroll = QScrollArea()
        iscroll.setWidgetResizable(True)
        iscroll.setWidget(self.initial_editor)
        self.tabs.addTab(iscroll, 'Initial (defaults)')
        self.initial_editor.changed.connect(self.document_changed.emit)

        self._rebuild_lang_tabs()

    # ------------------------------------------------------------------ #
    def _post_changed(self, *_):
        self.app_settings['remux_after_render'] = self.chk_remux.isChecked()
        self.app_settings['replace_original'] = self.chk_replace.isChecked()
        self.app_settings['move_to_subs_folder'] = self.chk_move.isChecked()
        self.app_settings['external_player'] = self.ed_player.text().strip()
        self.app_settings['external_player_args'] = \
            self.ed_player_args.text().strip()
        self.overrides_changed.emit()

    # -- language tabs -------------------------------------------------- #
    def _rebuild_lang_tabs(self):
        self.lang_tabs.blockSignals(True)
        while self.lang_tabs.count():
            self.lang_tabs.removeTab(0)
        for lang in sorted(self.overrides.by_lang.keys(),
                           key=lambda x: (x != '', x)):
            so = self.overrides.by_lang[lang]
            ed = OverrideEditor(so)
            ed.changed.connect(self.overrides_changed.emit)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(ed)
            label = lang if lang else 'Default'
            self.lang_tabs.addTab(scroll, label)
        # Default tab not closable
        bar = self.lang_tabs.tabBar()
        for i in range(self.lang_tabs.count()):
            if self.lang_tabs.tabText(i) == 'Default':
                bar.setTabButton(i, bar.ButtonPosition.RightSide, None)
        self.lang_tabs.blockSignals(False)

    def ensure_language_tab(self, lang: str):
        if not lang:
            return
        self.overrides.ensure_language(lang)
        shown = {self.lang_tabs.tabText(i)
                 for i in range(self.lang_tabs.count())}
        if lang not in shown:
            self._rebuild_lang_tabs()

    def _add_lang_tab(self):
        lang, ok = QInputDialog.getText(
            self, 'Add language overrides',
            'Language code (e.g. ja, en, zh-Hant):')
        if ok and lang.strip():
            self.overrides.ensure_language(lang.strip())
            self._rebuild_lang_tabs()
            self.overrides_changed.emit()

    def _close_lang_tab(self, index: int):
        label = self.lang_tabs.tabText(index)
        if label == 'Default':
            return
        self.overrides.by_lang.pop(label, None)
        self._rebuild_lang_tabs()
        self.overrides_changed.emit()

    # -- document binding ----------------------------------------------- #
    def set_document(self, doc: Optional[SubtitleDocument]):
        self.doc = doc
        self.style_list.clear()
        self.region_list.clear()
        if doc:
            self.style_list.addItems(sorted(doc.styles.keys()))
            self.region_list.addItems(list(doc.regions.keys()))
            self.initial_editor.load(doc.initial)
            self.ensure_language_tab(doc.language)
        else:
            self.initial_editor.load(None)
        self.style_editor.load(None)
        self.region_editor.load(None)

    def _style_selected(self, sid: str):
        if self.doc and sid in self.doc.styles:
            self.style_editor.load(self.doc.styles[sid])
        else:
            self.style_editor.load(None)

    def _region_selected(self, rid: str):
        if self.doc and rid in self.doc.regions:
            self.region_editor.load(self.doc.regions[rid])
        else:
            self.region_editor.load(None)

    def _add_style(self):
        if not self.doc:
            return
        name, ok = QInputDialog.getText(self, 'New style', 'Style id:')
        if not ok or not name.strip():
            return
        sid = self.doc.unique_style_id(name.strip())
        self.doc.styles[sid] = Style(id=sid)
        self.style_list.addItem(sid)
        self.style_list.setCurrentRow(self.style_list.count() - 1)
        self.document_changed.emit()

    def _del_style(self):
        if not self.doc:
            return
        item = self.style_list.currentItem()
        if not item:
            return
        self.doc.styles.pop(item.text(), None)
        self.style_list.takeItem(self.style_list.row(item))
        self.document_changed.emit()

    def _add_region(self):
        if not self.doc:
            return
        name, ok = QInputDialog.getText(self, 'New region', 'Region id:')
        if not ok or not name.strip():
            return
        region = Region(id=name.strip())
        rid = self.doc.ensure_region(region)
        self.region_list.addItem(rid)
        self.region_list.setCurrentRow(self.region_list.count() - 1)
        self.document_changed.emit()

    def _del_region(self):
        if not self.doc:
            return
        item = self.region_list.currentItem()
        if not item:
            return
        self.doc.regions.pop(item.text(), None)
        self.region_list.takeItem(self.region_list.row(item))
        self.document_changed.emit()
