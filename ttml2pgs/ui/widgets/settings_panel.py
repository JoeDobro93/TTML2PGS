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

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                             QCompleter, QDoubleSpinBox, QFormLayout,
                             QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QScrollArea, QSizePolicy,
                             QSplitter, QStyledItemDelegate, QTabWidget,
                             QToolButton, QVBoxLayout, QWidget)

from ...core.colors import parse_color, to_hex
from ...core.model import (Region, Shadow, Style, SubtitleDocument,
                           style_hints)
from ...core.overrides import LayoutOptions, OverrideSet, StyleOverrides
from ...core.units import Dim


class HintItemDelegate(QStyledItemDelegate):
    """List items as 'name  hint…' with the hint in grey italics."""

    def paint(self, painter, option, index):
        from PyQt6.QtWidgets import (QApplication, QStyle,
                                     QStyleOptionViewItem)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = index.data(Qt.ItemDataRole.DisplayRole) or ''
        hint = index.data(Qt.ItemDataRole.UserRole) or ''
        opt.text = ''
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt,
                          painter, opt.widget)
        painter.save()
        rect = opt.rect.adjusted(6, 0, -4, 0)
        painter.setFont(opt.font)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        painter.setPen(opt.palette.highlightedText().color() if selected
                       else opt.palette.text().color())
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, name)
        if hint:
            from PyQt6.QtGui import QFont, QFontMetrics
            fm = QFontMetrics(opt.font)
            used = fm.horizontalAdvance(name + '  ')
            f = QFont(opt.font)
            f.setItalic(True)
            f.setPointSizeF(max(6.0, f.pointSizeF() - 1))
            painter.setFont(f)
            painter.setPen(QColor(150, 150, 150))
            sub = rect.adjusted(used, 0, 0, 0)
            elided = QFontMetrics(f).elidedText(
                hint, Qt.TextElideMode.ElideRight, max(10, sub.width()))
            painter.drawText(sub, Qt.AlignmentFlag.AlignVCenter, elided)
        painter.restore()


# --------------------------------------------------------------------------- #
# small reusable editors
# --------------------------------------------------------------------------- #

class _NoWheelFilter(QObject):
    """Swallow wheel events on unfocused widgets so scrolling the pane
    never accidentally edits a spinbox/combo the cursor passes over."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Wheel and not obj.hasFocus():
            ev.ignore()
            return True
        return False


_no_wheel_filter: Optional[_NoWheelFilter] = None


def _wheel_filter() -> _NoWheelFilter:
    """Lazily (re)created — a module-level QObject dies with the
    QApplication, so check liveness before reuse."""
    global _no_wheel_filter
    alive = False
    if _no_wheel_filter is not None:
        try:
            from PyQt6 import sip
            alive = not sip.isdeleted(_no_wheel_filter)
        except ImportError:                            # pragma: no cover
            alive = True
    if not alive:
        _no_wheel_filter = _NoWheelFilter()
    return _no_wheel_filter


def guard_wheel(*widgets):
    f = _wheel_filter()
    for w in widgets:
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.installEventFilter(f)


def guard_wheel_children(root: QWidget):
    guard_wheel(*root.findChildren(QDoubleSpinBox),
                *root.findChildren(QComboBox))


def _installed_families() -> list:
    try:
        from ...core.fonts import FontManager
        return FontManager.instance().families_available()
    except Exception:                                  # pragma: no cover
        return []


class CollapsibleSection(QWidget):
    """A ▸/▾ header button + content. All sections share the pane's single
    outer scrollbar — no nested scrolling."""

    toggled = pyqtSignal(bool)

    def __init__(self, title: str, content: QWidget, expanded: bool = True,
                 outlined: bool = False):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)
        self.btn = QToolButton()
        # '&' is Qt's mnemonic marker — escape so titles like
        # "Outline & shadow" display the literal ampersand
        self.btn.setText(title.replace('&', '&&'))
        self.btn.setCheckable(True)
        self.btn.setChecked(expanded)
        self.btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.ArrowType.DownArrow if expanded
                              else Qt.ArrowType.RightArrow)
        self.btn.setStyleSheet(
            'QToolButton {border:none; font-weight:bold; padding:3px;}')
        self.btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)
        self.content = content
        if outlined:
            # visible frame around the section's contents
            self.content.setObjectName('secOutline')
            self.content.setStyleSheet(
                '#secOutline { border: 1px solid #4a4a4e; '
                'border-radius: 4px; }')
        self.content.setVisible(expanded)
        lay.addWidget(self.btn)
        lay.addWidget(self.content)
        self.btn.toggled.connect(self._toggle)

    def set_expanded(self, on: bool):
        if self.btn.isChecked() != on:
            self.btn.setChecked(on)      # fires _toggle + toggled

    def _toggle(self, on: bool):
        self.btn.setArrowType(Qt.ArrowType.DownArrow if on
                              else Qt.ArrowType.RightArrow)
        self.content.setVisible(on)
        self.toggled.emit(on)


def compact(*widgets, width=48):
    """Let numeric/choice widgets shrink instead of forcing pane width."""
    for w in widgets:
        w.setMinimumWidth(width)
        w.setSizePolicy(QSizePolicy.Policy.Preferred,
                        QSizePolicy.Policy.Fixed)
        if isinstance(w, QComboBox):
            w.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy
                .AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(6)


class ColorButton(QPushButton):
    changed = pyqtSignal()

    def __init__(self, color=(255, 255, 255, 255)):
        super().__init__()
        self._color = color
        self.setMinimumWidth(58)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
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
        # compact: allow shrinking well below the default hint so stacked
        # editors never force a horizontal scrollbar
        self.spin.setMinimumWidth(52)
        self.spin.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Fixed)
        self.cmb = QComboBox()
        self.cmb.addItems(units or self.UNITS)
        self.cmb.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb.setMinimumContentsLength(2)
        lay.addWidget(self.spin, 1)
        lay.addWidget(self.cmb)
        guard_wheel(self.spin, self.cmb)
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

#: v1's text color presets: name -> (RGBA, alpha)
COLOR_PRESETS = [
    ('SDR White', (229, 229, 229, 255), 0.90),
    ('SDR Yellow', (255, 238, 140, 255), 1.00),
    ('HDR Grey', (161, 161, 161, 255), 0.90),
    ('HDR Grey (OLED safe)', (128, 128, 128, 255), 0.90),
]


class OverrideEditor(QWidget):
    """Per-language overrides, grouped into collapsible sections that all
    share the pane's outer scrollbar (no nested scrolling)."""

    changed = pyqtSignal()

    def _preset_row(self, color, alpha, fallback_name):
        """Preset combo + color button + alpha spin, kept in sync: picking
        a preset fills color/alpha; manual edits flip the combo to Custom."""
        row = QHBoxLayout()
        cmb = QComboBox()
        for name, _c, _a in COLOR_PRESETS:
            cmb.addItem(name)
        cmb.addItem('Custom')
        btn = ColorButton(color)
        spin = QDoubleSpinBox()
        spin.setRange(0, 1)
        spin.setSingleStep(0.05)
        spin.setValue(alpha)
        spin.setToolTip('Text alpha (opacity)')

        def current_name():
            for name, c, a in COLOR_PRESETS:
                if tuple(btn.color()) == tuple(c) and \
                        abs(spin.value() - a) < 0.001:
                    return name
            return 'Custom'

        cmb.setCurrentText(current_name())

        def preset_picked(name):
            for pname, c, a in COLOR_PRESETS:
                if pname == name:
                    btn.blockSignals(True)
                    btn.set_color(c)
                    btn.blockSignals(False)
                    spin.blockSignals(True)
                    spin.setValue(a)
                    spin.blockSignals(False)
                    self._commit()
                    return

        def manual_change(*_):
            cmb.blockSignals(True)
            cmb.setCurrentText(current_name())
            cmb.blockSignals(False)

        cmb.currentTextChanged.connect(preset_picked)
        btn.changed.connect(manual_change)
        spin.valueChanged.connect(manual_change)

        compact(cmb, spin)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(cmb, 1)
        row.addWidget(btn)
        row.addWidget(QLabel('α:'))
        row.addWidget(spin)
        w = QWidget()
        w.setLayout(row)
        return w, cmb, btn, spin

    #: shared collapse state — every language tab shows the same
    #: sections open/closed (also keeps the tab stack's height honest:
    #: a QTabWidget sizes to its TALLEST page)
    section_toggled = pyqtSignal(str, bool)

    def __init__(self, so: StyleOverrides,
                 sec_state: Optional[Dict[str, bool]] = None,
                 is_default: bool = False):
        super().__init__()
        self.so = so
        self._sec_state = sec_state if sec_state is not None else {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 2, 4, 4)
        outer.setSpacing(0)
        self.sections: Dict[str, CollapsibleSection] = {}

        # language tabs get a master switch; while off, the language
        # follows the Default tab and everything below is greyed out
        self.chk_enabled: Optional[QCheckBox] = None
        if not is_default:
            self.chk_enabled = QCheckBox(
                'Use these overrides for this language '
                '(unchecked = follow Default)')
            self.chk_enabled.setStyleSheet('font-weight: bold;')
            self.chk_enabled.setChecked(so.enabled)
            self.chk_enabled.toggled.connect(self._enabled_toggled)
            outer.addWidget(self.chk_enabled)

        # the editor must never stretch taller than its content, or the
        # tab page keeps its expanded height when sections collapse
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Maximum)

        def section(name: str) -> QFormLayout:
            box = QWidget()
            form = QFormLayout(box)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setContentsMargins(8, 6, 8, 6)
            form.setVerticalSpacing(3)
            sec = CollapsibleSection(
                name, box, expanded=self._sec_state.get(name, True),
                outlined=True)
            sec.toggled.connect(
                lambda on, n=name: self._section_toggled(n, on))
            outer.addWidget(sec)
            self.sections[name] = sec
            return form

        families = _installed_families()

        # ---- Font ----------------------------------------------------- #
        form = section('Font')
        self.cmb_default_font = QComboBox()
        self.cmb_default_font.setEditable(True)
        self.cmb_default_font.addItem('(auto — v1/Chrome stack)')
        self.cmb_default_font.addItems(families)
        if so.default_font:
            self.cmb_default_font.setCurrentText(so.default_font)
        else:
            self.cmb_default_font.setCurrentIndex(0)
        self.cmb_default_font.setToolTip(
            'Preferred font for this language. Used for files that ask '
            'for a generic font (most subtitles) and as the first '
            'fallback — an explicit family in the file still wins. '
            '"(auto)" uses the built-in stack, matching what Chrome '
            'picked in v1 (Noto Sans CJK JP / Yu Gothic Medium…).')
        form.addRow('Default font:', self.cmb_default_font)
        self.chk_size = QCheckBox('Override font size')
        self.ed_size = DimEdit(so.font_size)
        form.addRow(self.chk_size, self.ed_size)
        self.chk_family = QCheckBox('Force font family')
        self.ed_family = QComboBox()
        self.ed_family.setEditable(True)
        self.ed_family.addItems(families)
        self.ed_family.setCurrentText(', '.join(so.font_family))
        self.ed_family.setToolTip(
            'Force this family over whatever the file specifies. Pick '
            'from installed fonts or type a comma-separated stack. '
            'Generic names (sans-serif, serif, monospace — and '
            '"Japanese", which Netflix files use) expand to the '
            'language-appropriate font stack.')
        if families:
            comp = QCompleter(families, self.ed_family)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.ed_family.setCompleter(comp)
        form.addRow(self.chk_family, self.ed_family)
        self.spin_boost = QDoubleSpinBox()
        self.spin_boost.setRange(0.0, 10.0)
        self.spin_boost.setSingleStep(0.5)
        self.spin_boost.setValue(so.weight_boost)
        self.spin_boost.setToolTip(
            'Stem darkening: thickens every glyph without switching to a '
            'bold face. 1 is the default — CJK text already picks '
            'Medium-weight faces like Chrome does, so only light '
            'darkening is needed. 0 disables; higher = heavier.')
        form.addRow('Stroke weight boost:', self.spin_boost)

        # ---- Color ---------------------------------------------------- #
        form = section('Color')
        self.chk_auto = QCheckBox('Auto color by video (SDR/HDR)')
        self.chk_auto.setToolTip(
            'Pick text color/alpha from each target video\'s dynamic '
            'range: pure white is blinding in HDR, HDR grey is dim in '
            'SDR. Detected per video (metadata + Dolby Vision scan). '
            'Wins over "Override color" when enabled.')
        form.addRow(self.chk_auto)
        (w_sdr, self.cmb_auto_sdr, self.btn_auto_sdr,
         self.spin_auto_sdr) = self._preset_row(
            so.auto_sdr_color, so.auto_sdr_alpha, 'SDR White')
        form.addRow('SDR videos:', w_sdr)
        (w_hdr, self.cmb_auto_hdr, self.btn_auto_hdr,
         self.spin_auto_hdr) = self._preset_row(
            so.auto_hdr_color, so.auto_hdr_alpha, 'HDR Grey')
        form.addRow('HDR videos:', w_hdr)
        self.chk_color = QCheckBox('Override color (fixed)')
        self.chk_color.setToolTip(
            'Force one fixed color regardless of the video\'s dynamic '
            'range. Auto color wins when both are enabled.')
        self.btn_color = ColorButton(so.color)
        form.addRow(self.chk_color, self.btn_color)

        # ---- Outline & shadow ----------------------------------------- #
        form = section('Outline & shadow')
        self.chk_outline = QCheckBox('Override outline')
        row_o = QHBoxLayout()
        row_o.setContentsMargins(0, 0, 0, 0)
        row_o.setSpacing(4)
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
        self.chk_shadow = QCheckBox('Override shadow')
        row_s1 = QHBoxLayout()
        row_s1.setContentsMargins(0, 0, 0, 0)
        row_s1.setSpacing(4)
        self.chk_shadow_on = QCheckBox('on')
        self.ed_sx = DimEdit(so.shadow_offset_x, ['px', 'em', 'vh'])
        self.ed_sy = DimEdit(so.shadow_offset_y, ['px', 'em', 'vh'])
        row_s1.addWidget(self.chk_shadow_on)
        row_s1.addWidget(QLabel('X:'))
        row_s1.addWidget(self.ed_sx, 1)
        row_s1.addWidget(QLabel('Y:'))
        row_s1.addWidget(self.ed_sy, 1)
        w_s1 = QWidget()
        w_s1.setLayout(row_s1)
        form.addRow(self.chk_shadow, w_s1)
        row_s2 = QHBoxLayout()
        row_s2.setContentsMargins(0, 0, 0, 0)
        row_s2.setSpacing(4)
        self.ed_sb = DimEdit(so.shadow_blur, ['px', 'em', 'vh'])
        self.btn_shadow_c = ColorButton(so.shadow_color)
        self.spin_salpha = QDoubleSpinBox()
        self.spin_salpha.setRange(0, 1)
        self.spin_salpha.setSingleStep(0.05)
        self.spin_salpha.setValue(so.shadow_alpha)
        row_s2.addWidget(QLabel('Blur:'))
        row_s2.addWidget(self.ed_sb, 1)
        row_s2.addWidget(self.btn_shadow_c)
        row_s2.addWidget(QLabel('α:'))
        row_s2.addWidget(self.spin_salpha)
        w_s2 = QWidget()
        w_s2.setLayout(row_s2)
        form.addRow('', w_s2)

        # ---- Spacing & opacity ---------------------------------------- #
        form = section('Spacing & opacity')
        self.chk_lh = QCheckBox('Override line height')
        self.ed_lh = DimEdit(so.line_height, ['', 'em', 'px', 'vh', '%'])
        form.addRow(self.chk_lh, self.ed_lh)
        self.spin_lspace = QDoubleSpinBox()
        self.spin_lspace.setRange(0.5, 2.0)
        self.spin_lspace.setSingleStep(0.05)
        self.spin_lspace.setValue(so.line_spacing)
        self.spin_lspace.setToolTip(
            'Multiplies the gap between a cue\'s lines (1 = default; '
            '<1 tighter, >1 wider). Lines can never overlap, and '
            'furigana always keeps its reserved space between lines — '
            'tightening squeezes the empty leading only.')
        form.addRow('Line spacing ×:', self.spin_lspace)
        self.spin_alpha = QDoubleSpinBox()
        self.spin_alpha.setRange(0, 1)
        self.spin_alpha.setSingleStep(0.05)
        self.spin_alpha.setValue(so.opacity_mult)
        form.addRow('Global opacity:', self.spin_alpha)
        self.chk_pad = QCheckBox('Safe-area padding')
        self.chk_pad.setToolTip(
            'Inset the region anchoring box (v1\'s #pad-box) for THIS '
            'language — text moves inward, never scales. Preview guide '
            'lines follow the active file\'s language set.')
        rowp = QHBoxLayout()
        rowp.setContentsMargins(0, 0, 0, 0)
        rowp.setSpacing(4)
        self.spin_pv = QDoubleSpinBox()
        self.spin_pv.setRange(0, 40)
        self.spin_pv.setSuffix(' %V')
        self.spin_pv.setValue(so.padding_v)
        self.spin_ph = QDoubleSpinBox()
        self.spin_ph.setRange(0, 40)
        self.spin_ph.setSuffix(' %H')
        self.spin_ph.setValue(so.padding_h)
        rowp.addWidget(self.spin_pv)
        rowp.addWidget(self.spin_ph)
        padw = QWidget()
        padw.setLayout(rowp)
        form.addRow(self.chk_pad, padw)

        compact(self.spin_boost, self.spin_salpha, self.spin_alpha,
                self.spin_pv, self.spin_ph, self.spin_lspace,
                self.cmb_default_font, self.ed_family)

        self._load_flags()
        for w in (self.chk_size, self.chk_family, self.chk_color,
                  self.chk_auto, self.chk_outline, self.chk_outline_on,
                  self.chk_shadow, self.chk_shadow_on, self.chk_lh,
                  self.chk_pad):
            w.toggled.connect(self._commit)
        for w in (self.ed_size, self.ed_outline_w, self.ed_sx, self.ed_sy,
                  self.ed_sb, self.ed_lh):
            w.changed.connect(self._commit)
        for w in (self.btn_color, self.btn_outline_c, self.btn_shadow_c,
                  self.btn_auto_sdr, self.btn_auto_hdr):
            w.changed.connect(self._commit)
        self.ed_family.currentTextChanged.connect(self._commit)
        self.cmb_default_font.currentTextChanged.connect(self._commit)
        self.spin_alpha.valueChanged.connect(self._commit)
        self.spin_salpha.valueChanged.connect(self._commit)
        self.spin_auto_sdr.valueChanged.connect(self._commit)
        self.spin_auto_hdr.valueChanged.connect(self._commit)
        self.spin_boost.valueChanged.connect(self._commit)
        self.spin_pv.valueChanged.connect(self._commit)
        self.spin_ph.valueChanged.connect(self._commit)
        self.spin_lspace.valueChanged.connect(self._commit)
        # when the page is forced taller than its content (the tab
        # stack fills its area), the leftover collects HERE instead of
        # spreading between the section headers
        outer.addStretch(1)
        if self.chk_enabled is not None and not so.enabled:
            for sec in self.sections.values():
                sec.setEnabled(False)
        guard_wheel_children(self)

    def _enabled_toggled(self, on: bool):
        self.so.enabled = bool(on)
        for sec in self.sections.values():
            sec.setEnabled(on)
        self.changed.emit()

    def _section_toggled(self, name: str, on: bool):
        self._sec_state[name] = on
        self.section_toggled.emit(name, on)

    def _load_flags(self):
        so = self.so
        self.chk_size.setChecked(so.override_font_size)
        self.chk_family.setChecked(so.override_font_family)
        self.chk_color.setChecked(so.override_color)
        self.chk_auto.setChecked(so.auto_color)
        self.chk_outline.setChecked(so.override_outline)
        self.chk_outline_on.setChecked(so.outline_enabled)
        self.chk_shadow.setChecked(so.override_shadow)
        self.chk_shadow_on.setChecked(so.shadow_enabled)
        self.chk_lh.setChecked(so.override_line_height)
        self.chk_pad.setChecked(so.use_padding)

    def _commit(self, *_):
        so = self.so
        so.override_font_size = self.chk_size.isChecked()
        so.font_size = self.ed_size.dim()
        so.override_font_family = self.chk_family.isChecked()
        fam_text = self.ed_family.currentText()
        so.font_family = [f.strip() for f in fam_text.split(',')
                          if f.strip()] or ['sans-serif']
        dft = self.cmb_default_font.currentText().strip()
        so.default_font = '' if (dft.startswith('(auto')
                                 or not dft) else dft
        so.override_color = self.chk_color.isChecked()
        so.color = self.btn_color.color()
        so.auto_color = self.chk_auto.isChecked()
        so.auto_sdr_color = self.btn_auto_sdr.color()
        so.auto_sdr_alpha = self.spin_auto_sdr.value()
        so.auto_hdr_color = self.btn_auto_hdr.color()
        so.auto_hdr_alpha = self.spin_auto_hdr.value()
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
        so.line_spacing = self.spin_lspace.value()
        so.opacity_mult = self.spin_alpha.value()
        so.weight_boost = self.spin_boost.value()
        so.use_padding = self.chk_pad.isChecked()
        so.padding_v = self.spin_pv.value()
        so.padding_h = self.spin_ph.value()
        self.changed.emit()


class LayoutOptionsEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, lo: LayoutOptions):
        super().__init__()
        self.lo = lo
        form = QFormLayout(self)
        form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(18, 2, 4, 6)
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
        form.addRow(self.chk_vidims)
        form.addRow('', self.chk_hd)
        form.addRow(self.chk_169)
        form.addRow(self.chk_ar, arw)
        # (safe-area padding is per language — Text style overrides →
        # Spacing & opacity)
        self._load()
        for w in (self.chk_vidims, self.chk_hd, self.chk_169, self.chk_ar):
            w.toggled.connect(self._commit)
        for w in (self.spin_arw, self.spin_arh):
            w.valueChanged.connect(self._commit)
        guard_wheel_children(self)

    def _load(self):
        lo = self.lo
        self.chk_vidims.setChecked(lo.use_video_dims)
        self.chk_hd.setChecked(lo.scale_to_hd)
        self.chk_169.setChecked(lo.force_16_9)
        self.chk_ar.setChecked(lo.override_ar)
        self._sync_enabled()

    def _sync_enabled(self):
        """Grey out options that another enabled option overrides, so
        the winner is obvious."""
        self.chk_hd.setEnabled(self.chk_vidims.isChecked())
        # a manual content-AR override wins over "force 16:9"
        ar_on = self.chk_ar.isChecked()
        self.chk_169.setEnabled(not ar_on)
        self.chk_169.setToolTip(
            'Content AR override is on — it wins over Force 16:9.'
            if ar_on else
            'Lay text out over the full canvas even when the video\'s '
            'aspect differs.')
        # "force 16:9" makes the AR override moot the other way? No —
        # override wins; but it does make the VIDEO AR moot, nothing to
        # grey. AR spins follow their checkbox:
        for w in (self.spin_arw, self.spin_arh):
            w.setEnabled(ar_on)

    def _commit(self, *_):
        lo = self.lo
        lo.use_video_dims = self.chk_vidims.isChecked()
        lo.scale_to_hd = self.chk_hd.isChecked()
        lo.force_16_9 = self.chk_169.isChecked()
        lo.override_ar = self.chk_ar.isChecked()
        lo.ar_w = self.spin_arw.value()
        lo.ar_h = self.spin_arh.value()
        self._sync_enabled()
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
        form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(4, 4, 4, 4)

        def add_row(label: str):
            chk = QCheckBox(label)
            return chk

        # font
        self.c_family = add_row('Font family')
        self.e_family = QComboBox()
        self.e_family.setEditable(True)
        fams = _installed_families()
        self.e_family.addItems(fams)
        self.e_family.setCurrentText('')
        if fams:
            comp = QCompleter(fams, self.e_family)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.e_family.setCompleter(comp)
        compact(self.e_family)
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
        row_s.addWidget(QLabel('X:'))
        row_s.addWidget(self.e_sx, 1)
        row_s.addWidget(QLabel('Y:'))
        row_s.addWidget(self.e_sy, 1)
        ws = QWidget()
        ws.setLayout(row_s)
        form.addRow(self.c_shadow, ws)
        row_s2 = QHBoxLayout()
        row_s2.addWidget(QLabel('Blur:'))
        row_s2.addWidget(self.e_sb, 1)
        row_s2.addWidget(self.e_sc)
        ws2 = QWidget()
        ws2.setLayout(row_s2)
        form.addRow('', ws2)
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
        # emphasis / combine (ruby roles are structural — edited in the
        # Selected-cue pane, not per style)
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
                  self.e_mra, self.e_wm, self.e_emph, self.e_tcy):
            w.currentTextChanged.connect(self._commit)
        self.e_family.currentTextChanged.connect(self._commit)
        self.e_shear.valueChanged.connect(self._commit)
        guard_wheel_children(self)

    # ------------------------------------------------------------------ #
    def load(self, style: Optional[Style]):
        self._loading = True
        self.style = style
        s = style or Style()
        self.c_family.setChecked(s.font_family is not None)
        self.e_family.blockSignals(True)
        self.e_family.setCurrentText(', '.join(s.font_family)
                                     if s.font_family else '')
        self.e_family.blockSignals(False)
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
        fam_text = self.e_family.currentText()
        s.font_family = [f.strip() for f in fam_text.split(',')
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
        form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapLongRows)
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
        guard_wheel_children(self)

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
        # One outer scrollbar for the whole tab; the per-language editor
        # and the option groups below are collapsible sections, never
        # nested scroll areas.
        ov_tab = QWidget()
        ovl = QVBoxLayout(ov_tab)
        ovl.setSpacing(4)
        self.lang_tabs = QTabWidget()
        # height follows the (shared) section collapse state — an
        # Expanding tab widget would keep the page tall when collapsed
        self.lang_tabs.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Maximum)
        self.lang_tabs.setTabsClosable(True)
        self.lang_tabs.tabCloseRequested.connect(self._close_lang_tab)
        btn_add_lang = QPushButton('+')
        btn_add_lang.setFixedWidth(28)
        btn_add_lang.setToolTip('Add a language-specific override set')
        btn_add_lang.clicked.connect(self._add_lang_tab)
        self.lang_tabs.setCornerWidget(btn_add_lang)
        # the collapsible wraps the WHOLE language tab widget: a stacked
        # widget reports the max size of all its pages, so a collapsible
        # inside a page could never shrink the box. This way the section
        # behaves exactly like Layout/Post-processing below.
        ovl.addWidget(CollapsibleSection('Text style overrides (per language)',
                                         self.lang_tabs, expanded=True))

        self.layout_editor = LayoutOptionsEditor(self.overrides.layout)
        self.layout_editor.changed.connect(self.overrides_changed.emit)
        ovl.addWidget(CollapsibleSection('Layout / canvas',
                                         self.layout_editor, expanded=False))

        # (post-processing + player settings live in Preferences)
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
        self.style_list.setItemDelegate(HintItemDelegate(self.style_list))
        ll.addWidget(self.style_list)
        rowb = QHBoxLayout()
        b_add = QPushButton('Add')
        b_ren = QPushButton('Rename')
        b_del = QPushButton('Delete')
        rowb.addWidget(b_add)
        rowb.addWidget(b_ren)
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
        b_ren.clicked.connect(self._rename_style)
        b_del.clicked.connect(self._del_style)
        self.style_list.currentTextChanged.connect(self._style_selected)
        self.style_editor.changed.connect(self.document_changed.emit)
        self.style_editor.changed.connect(self.refresh_style_hints)

        # ---- regions tab --------------------------------------------- #
        rg_tab = QSplitter(Qt.Orientation.Horizontal)
        leftr = QWidget()
        rl = QVBoxLayout(leftr)
        rl.setContentsMargins(2, 2, 2, 2)
        self.region_list = QListWidget()
        # grey position hints (bottom / top / vertical right …) like
        # the Styles list
        self.region_list.setItemDelegate(
            HintItemDelegate(self.region_list))
        rl.addWidget(self.region_list)
        rowrb = QHBoxLayout()
        rb_add = QPushButton('Add')
        rb_ren = QPushButton('Rename')
        rb_del = QPushButton('Delete')
        rowrb.addWidget(rb_add)
        rowrb.addWidget(rb_ren)
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
        rb_ren.clicked.connect(self._rename_region)
        rb_del.clicked.connect(self._del_region)
        self.region_list.currentTextChanged.connect(self._region_selected)
        self.region_editor.changed.connect(self.document_changed.emit)
        self.region_editor.changed.connect(self.refresh_region_hints)

        # ---- initial tab --------------------------------------------- #
        self.initial_editor = StyleEditor()
        iscroll = QScrollArea()
        iscroll.setWidgetResizable(True)
        iscroll.setWidget(self.initial_editor)
        self.tabs.addTab(iscroll, 'Initial (defaults)')
        self.initial_editor.changed.connect(self.document_changed.emit)

        self._rebuild_lang_tabs()

    # -- language tabs -------------------------------------------------- #
    def _rebuild_lang_tabs(self):
        # never steal the user's place: restore whichever tab was active
        current = self.lang_tabs.tabText(self.lang_tabs.currentIndex()) \
            if self.lang_tabs.count() else 'Default'
        # shared section collapse state across every language tab (a tab
        # stack sizes to its tallest page, so states must stay in step)
        if not hasattr(self, '_sec_state'):
            self._sec_state = {}
        self.lang_tabs.blockSignals(True)
        while self.lang_tabs.count():
            self.lang_tabs.removeTab(0)
        for lang in sorted(self.overrides.by_lang.keys(),
                           key=lambda x: (x != '', x)):
            so = self.overrides.by_lang[lang]
            ed = OverrideEditor(so, self._sec_state, is_default=(lang == ''))
            ed.changed.connect(self.overrides_changed.emit)
            ed.section_toggled.connect(self._sync_sections)
            # the editor sits directly in the tab — the whole overrides
            # tab shares ONE outer scrollbar (no nested scrolling)
            label = lang if lang else 'Default'
            self.lang_tabs.addTab(ed, label)
        # Default tab not closable
        bar = self.lang_tabs.tabBar()
        for i in range(self.lang_tabs.count()):
            if self.lang_tabs.tabText(i) == 'Default':
                bar.setTabButton(i, bar.ButtonPosition.RightSide, None)
        for i in range(self.lang_tabs.count()):
            if self.lang_tabs.tabText(i) == current:
                self.lang_tabs.setCurrentIndex(i)
                break
        self.lang_tabs.blockSignals(False)
        self._update_lang_tabs_height()

    def ensure_language_tab(self, lang: str):
        if not lang:
            return
        self.overrides.ensure_language(lang)
        shown = {self.lang_tabs.tabText(i)
                 for i in range(self.lang_tabs.count())}
        if lang not in shown:
            self._rebuild_lang_tabs()

    def ensure_language_tabs(self, langs):
        """A tab for every open subtitle language — DISABLED (following
        Default) until its 'use these overrides' toggle is turned on,
        so auto-created tabs never hijack Default edits."""
        added = False
        for lang in langs:
            lang = (lang or '').strip()
            if lang and lang not in self.overrides.by_lang:
                self.overrides.ensure_language(lang, enabled=False)
                added = True
        if added:
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
    def _style_item(self, sid: str) -> QListWidgetItem:
        it = QListWidgetItem(sid)
        if self.doc and sid in self.doc.styles:
            it.setData(Qt.ItemDataRole.UserRole,
                       style_hints(self.doc.styles[sid]))
        return it

    def _sync_sections(self, name: str, on: bool):
        """Mirror a section collapse across every language tab."""
        for i in range(self.lang_tabs.count()):
            ed = self.lang_tabs.widget(i)
            sec = getattr(ed, 'sections', {}).get(name)
            if sec is not None:
                sec.set_expanded(on)
        self._update_lang_tabs_height()

    def _update_lang_tabs_height(self):
        """Hard-cap the language tab box at its content height so
        collapsing sections visibly shrinks it (a QTabWidget left to
        its own devices keeps the tallest-ever page height)."""
        from PyQt6.QtCore import QTimer

        def apply():
            ed = self.lang_tabs.currentWidget()
            if ed is None:
                return
            h = ed.sizeHint().height() + \
                self.lang_tabs.tabBar().sizeHint().height() + 10
            self.lang_tabs.setMaximumHeight(max(60, h))
        QTimer.singleShot(0, apply)      # let layouts settle first

    def refresh_style_hints(self):
        for i in range(self.style_list.count()):
            it = self.style_list.item(i)
            sid = it.text()
            if self.doc and sid in self.doc.styles:
                it.setData(Qt.ItemDataRole.UserRole,
                           style_hints(self.doc.styles[sid]))

    def refresh_region_hints(self):
        """Recompute the position hints — a region edit (anchor, size,
        alignment) can move it to another screen area."""
        if not self.doc:
            return
        from ...core.renderer import classify_region_position
        for i in range(self.region_list.count()):
            it = self.region_list.item(i)
            rid = it.text()
            if rid in self.doc.regions:
                try:
                    it.setData(Qt.ItemDataRole.UserRole,
                               classify_region_position(
                                   self.doc, self.doc.regions[rid]))
                except Exception:
                    pass
        self.region_list.viewport().update()

    def set_document(self, doc: Optional[SubtitleDocument]):
        self.doc = doc
        self.style_list.clear()
        self.region_list.clear()
        if doc:
            for sid in sorted(doc.styles.keys()):
                self.style_list.addItem(self._style_item(sid))
            from ...core.renderer import classify_region_position
            for rid in doc.regions.keys():
                it = QListWidgetItem(rid)
                try:
                    it.setData(Qt.ItemDataRole.UserRole,
                               classify_region_position(
                                   doc, doc.regions[rid]))
                except Exception:
                    pass
                self.region_list.addItem(it)
            self.initial_editor.load(doc.initial)
            # deliberately NO ensure_language_tab here: languages follow
            # Default until the user adds a tab via '+' themselves
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
        self.style_list.addItem(self._style_item(sid))
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

    def _rename_style(self):
        if not self.doc:
            return
        item = self.style_list.currentItem()
        if not item:
            return
        old = item.text()
        new, ok = QInputDialog.getText(self, 'Rename style', 'New id:',
                                       text=old)
        new = (new or '').strip()
        if not ok or not new or new == old:
            return
        if not self.doc.rename_style(old, new):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, 'Rename style',
                                f"A style named '{new}' already exists.")
            return
        item.setText(new)
        self.document_changed.emit()

    def _add_region(self):
        if not self.doc:
            return
        name, ok = QInputDialog.getText(self, 'New region', 'Region id:')
        if not ok or not name.strip():
            return
        # sensible default: bottom-centered caption box in the safe area
        region = Region(id=name.strip(),
                        x=Dim(50, '%'), x_edge='center',
                        y=Dim(8, '%'), y_edge='bottom',
                        width=Dim(80, '%'), height=Dim(20, '%'))
        region.style.display_align = 'after'
        region.style.text_align = 'center'
        rid = self.doc.ensure_region(region)
        self.region_list.addItem(rid)
        self.region_list.setCurrentRow(self.region_list.count() - 1)
        self.document_changed.emit()

    def _rename_region(self):
        if not self.doc:
            return
        item = self.region_list.currentItem()
        if not item:
            return
        old = item.text()
        new, ok = QInputDialog.getText(self, 'Rename region', 'New id:',
                                       text=old)
        new = (new or '').strip()
        if not ok or not new or new == old:
            return
        if not self.doc.rename_region(old, new):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, 'Rename region',
                                f"A region named '{new}' already exists.")
            return
        item.setText(new)
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
