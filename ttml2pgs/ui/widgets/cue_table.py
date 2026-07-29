"""
Cue pane: sortable/filterable cue table with inline editing, add/remove,
and the time-adjustment tools (shift all / shift after selection / manual
fps conform).
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Optional

from PyQt6.QtCore import (QAbstractTableModel, QModelIndex,
                          QSortFilterProxyModel, Qt, pyqtSignal)
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMenu, QPushButton, QRadioButton,
                             QStyledItemDelegate, QTableView, QVBoxLayout,
                             QWidget, QCheckBox)

from ...core.model import Cue, SpanNode, SubtitleDocument
from ...core.timing import (COMMON_RATES, RetimePlan, format_display_time,
                            parse_display_time)

COL_ON, COL_NUM, COL_START, COL_END, COL_DUR, COL_REGION, COL_STYLE, \
    COL_TEXT = range(8)


def parse_style_refs(doc: SubtitleDocument, text: str):
    """'s1 s2' → ['s1','s2'] if every id exists; ''/'default' → [];
    None when an unknown id is mentioned."""
    text = (text or '').strip()
    if not text or text.lower() in ('default', '(default)'):
        return []
    refs = text.replace(',', ' ').split()
    for r in refs:
        if r not in doc.styles:
            return None
    return refs


def preview_text(doc: SubtitleDocument, cue: Cue) -> str:
    """
    Cue text for display/filtering, with ruby annotations rendered as
    ``base(reading)`` — v1 showed the flattened source this way and users
    filter for ruby cues by typing ``(``.
    """
    region = doc.get_region(cue)
    out: List[str] = []

    def walk(node: SpanNode, chain: list):
        for ch in node.children:
            if ch.kind == 'text':
                out.append(ch.text)
            elif ch.kind == 'br':
                out.append('\n')
            elif ch.kind == 'span':
                sub = chain + [(ch.style_refs, ch.inline_style)]
                try:
                    role = doc.resolve_style(sub, region).ruby or ''
                except Exception:
                    role = ''
                if role in ('text', 'textContainer'):
                    out.append('(')
                    walk(ch, sub)
                    out.append(')')
                elif role == 'delimiter':
                    continue
                else:
                    walk(ch, sub)

    try:
        walk(cue.root, [(cue.style_refs, cue.inline_style)])
        return ''.join(out)
    except Exception:
        return cue.plain_text()


class CueModel(QAbstractTableModel):
    HEADERS = ['', '#', 'Start', 'End', 'Dur', 'Region', 'Style', 'Text']

    cue_edited = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.doc: Optional[SubtitleDocument] = None
        self.cues: List[Cue] = []
        self._previews: dict = {}          # id(cue) -> display text
        #: optional provider of the current selection — region/style edits
        #: on a selected row apply to every selected cue
        self.bulk_targets = None

    def set_document(self, doc: Optional[SubtitleDocument]):
        self.beginResetModel()
        self.doc = doc
        self.cues = doc.sorted_cues() if doc else []
        self._previews.clear()
        self.endResetModel()

    def refresh_order(self):
        self.beginResetModel()
        if self.doc:
            self.cues = self.doc.sorted_cues()
        self._previews.clear()
        self.endResetModel()

    def preview(self, cue: Cue) -> str:
        text = self._previews.get(id(cue))
        if text is None:
            text = preview_text(self.doc, cue) if self.doc \
                else cue.plain_text()
            self._previews[id(cue)] = text
        return text

    # ------------------------------------------------------------------ #
    def rowCount(self, parent=QModelIndex()):
        return len(self.cues)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, sec, orient, role):
        if role == Qt.ItemDataRole.DisplayRole and \
                orient == Qt.Orientation.Horizontal:
            return self.HEADERS[sec]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        c = index.column()
        if c == COL_ON:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if c in (COL_START, COL_END, COL_REGION, COL_STYLE, COL_TEXT):
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        cue = self.cues[index.row()]
        c = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if c == COL_NUM:
                return str(index.row() + 1)
            if c == COL_START:
                return format_display_time(cue.begin_ms)
            if c == COL_END:
                return format_display_time(cue.end_ms)
            if c == COL_DUR:
                return f"{cue.duration_ms / 1000:.2f}s"
            if c == COL_REGION:
                return cue.region_id or '(default)'
            if c == COL_STYLE:
                if role == Qt.ItemDataRole.EditRole:
                    return ' '.join(cue.style_refs)
                txt = ' '.join(cue.style_refs) or 'default'
                if cue.inline_style is not None:
                    txt += ' ✎'
                return txt
            if c == COL_TEXT:
                if role == Qt.ItemDataRole.EditRole:
                    return cue.plain_text()
                return self.preview(cue).replace('\n', ' ⏎ ')
        if role == Qt.ItemDataRole.FontRole and c == COL_STYLE \
                and not cue.style_refs:
            from PyQt6.QtGui import QFont
            f = QFont()
            f.setItalic(True)
            return f
        if role == Qt.ItemDataRole.CheckStateRole and c == COL_ON:
            return Qt.CheckState.Checked if cue.enabled \
                else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole and c == COL_STYLE:
            bits = []
            if cue.style_refs:
                bits.append('Named styles: ' + ', '.join(cue.style_refs))
            else:
                bits.append('No named styles — defers to Initials '
                            '(document defaults)')
            if cue.inline_style is not None:
                bits.append('✎ has inline <p> style overrides — edit in '
                            'the Selected cue pane')
            bits.append('Edit: space-separated style ids, or "default".')
            return '\n'.join(bits)
        if role == Qt.ItemDataRole.ToolTipRole and c == COL_TEXT:
            return self.preview(cue)
        return None

    def _bulk(self, cue: Cue) -> List[Cue]:
        """Region/style edits on a selected row apply to the whole
        selection."""
        if self.bulk_targets is not None:
            sel = self.bulk_targets()
            if len(sel) > 1 and cue in sel:
                return sel
        return [cue]

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        cue = self.cues[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and c == COL_ON:
            cue.enabled = (value == Qt.CheckState.Checked.value or
                           value == Qt.CheckState.Checked)
            self.dataChanged.emit(index, index)
            self.cue_edited.emit()
            return True
        if role != Qt.ItemDataRole.EditRole:
            return False
        many = False
        if c == COL_START:
            ms = parse_display_time(str(value))
            if ms is None:
                return False
            cue.begin_ms = ms
        elif c == COL_END:
            ms = parse_display_time(str(value))
            if ms is None:
                return False
            cue.end_ms = ms
        elif c == COL_REGION:
            rid = str(value)
            if self.doc and (rid in self.doc.regions or rid == '(default)'):
                targets = self._bulk(cue)
                for t in targets:
                    t.region_id = None if rid == '(default)' else rid
                many = len(targets) > 1
            else:
                return False
        elif c == COL_STYLE:
            if not self.doc:
                return False
            refs = parse_style_refs(self.doc, str(value))
            if refs is None:
                return False
            targets = self._bulk(cue)
            for t in targets:
                t.style_refs = list(refs)
            many = len(targets) > 1
        elif c == COL_TEXT:
            _set_plain_text(cue, str(value))
            self._previews.pop(id(cue), None)
        else:
            return False
        if many:
            self.dataChanged.emit(
                self.index(0, c), self.index(self.rowCount() - 1, c))
        else:
            self.dataChanged.emit(index, index)
        self.cue_edited.emit()
        return True

    def refresh_cue(self, cue: Cue):
        """Repaint one cue's row (e.g. after Selected-cue pane edits)."""
        for row, c in enumerate(self.cues):
            if c is cue:
                self.dataChanged.emit(self.index(row, 0),
                                      self.index(row, COL_TEXT))
                return

    def cue_at(self, row: int) -> Optional[Cue]:
        if 0 <= row < len(self.cues):
            return self.cues[row]
        return None


def _set_plain_text(cue: Cue, text: str):
    """Replace cue content with plain text (keeps cue-level styling)."""
    root = SpanNode(kind='root')
    for i, line in enumerate(text.split('\n')):
        if i > 0:
            root.children.append(SpanNode.br())
        if line:
            root.children.append(SpanNode.text_node(line))
    cue.root = root


class RegionDelegate(QStyledItemDelegate):
    def __init__(self, model: CueModel, parent=None):
        super().__init__(parent)
        self._model = model

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItem('(default)')
        if self._model.doc:
            combo.addItems(list(self._model.doc.regions.keys()))
        return combo

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data(Qt.ItemDataRole.EditRole)
                              or '(default)')

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(),
                      Qt.ItemDataRole.EditRole)


class StyleDelegate(QStyledItemDelegate):
    """Editable combo: pick one named style or type several ids."""

    def __init__(self, model: CueModel, parent=None):
        super().__init__(parent)
        self._model = model

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        combo.addItem('default')
        if self._model.doc:
            combo.addItems(sorted(self._model.doc.styles.keys()))
        return combo

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data(Qt.ItemDataRole.EditRole)
                              or 'default')

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(),
                      Qt.ItemDataRole.EditRole)


class CueFilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ''
        self.region = ''

    def set_filters(self, text: str, region: str):
        self.text = text.lower()
        self.region = region
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        model: CueModel = self.sourceModel()
        cue = model.cue_at(row)
        if cue is None:
            return False
        if self.region and self.region != 'All regions':
            rid = cue.region_id or '(default)'
            if rid != self.region:
                return False
        # match against the preview text so "(" finds ruby cues
        if self.text and self.text not in model.preview(cue).lower():
            return False
        return True


class CuePane(QWidget):
    cue_selected = pyqtSignal(object)          # Cue | None
    cues_changed = pyqtSignal()                # timing/text/region edits

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = CueModel()
        self.proxy = CueFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.doc: Optional[SubtitleDocument] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText('Filter text…')
        self.txt_filter.setClearButtonEnabled(True)
        self.cmb_region = QComboBox()
        self.cmb_region.addItem('All regions')
        self.btn_add = QPushButton('+ Cue')
        self.btn_del = QPushButton('Delete')
        self.btn_dup = QPushButton('Duplicate')
        self.btn_time = QPushButton('Time tools…')
        self.btn_check = QPushButton('Check all')
        self.btn_uncheck = QPushButton('Uncheck all')
        for w in (self.txt_filter, self.cmb_region, self.btn_add,
                  self.btn_dup, self.btn_del, self.btn_time,
                  self.btn_check, self.btn_uncheck):
            bar.addWidget(w)
        bar.setStretch(0, 1)
        lay.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        for col, w in ((COL_ON, 28), (COL_NUM, 44), (COL_START, 100),
                       (COL_END, 100), (COL_DUR, 60), (COL_REGION, 100),
                       (COL_STYLE, 90)):
            self.table.setColumnWidth(col, w)
        self.table.setItemDelegateForColumn(COL_REGION,
                                            RegionDelegate(self.model, self))
        self.table.setItemDelegateForColumn(COL_STYLE,
                                            StyleDelegate(self.model, self))
        self.model.bulk_targets = self.selected_cues
        lay.addWidget(self.table)

        # connections
        self.txt_filter.textChanged.connect(self._filters_changed)
        self.cmb_region.currentTextChanged.connect(self._filters_changed)
        self.btn_add.clicked.connect(self.add_cue)
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_dup.clicked.connect(self.duplicate_selected)
        self.btn_time.clicked.connect(self.open_time_tools)
        self.btn_check.clicked.connect(lambda: self.set_all_checked(True))
        self.btn_uncheck.clicked.connect(lambda: self.set_all_checked(False))
        self.model.cue_edited.connect(self._on_edit)

    # ------------------------------------------------------------------ #
    def set_document(self, doc: Optional[SubtitleDocument]):
        self.doc = doc
        self.model.set_document(doc)
        self.cmb_region.blockSignals(True)
        self.cmb_region.clear()
        self.cmb_region.addItem('All regions')
        if doc:
            self.cmb_region.addItem('(default)')
            self.cmb_region.addItems(list(doc.regions.keys()))
        self.cmb_region.blockSignals(False)
        sel = self.table.selectionModel()
        if sel:
            try:
                sel.selectionChanged.disconnect(self._on_selection)
            except TypeError:
                pass
            sel.selectionChanged.connect(self._on_selection)
        if self.model.rowCount():
            self.table.selectRow(0)

    def refresh(self):
        self.model.refresh_order()

    def refresh_cue(self, cue: Cue):
        self.model.refresh_cue(cue)

    def refresh_regions(self):
        """Rebuild the region filter after regions were added/renamed/
        removed in the settings pane (the in-table editor reads the doc
        live, so only this combo needs refreshing)."""
        current = self.cmb_region.currentText()
        self.cmb_region.blockSignals(True)
        self.cmb_region.clear()
        self.cmb_region.addItem('All regions')
        if self.doc:
            self.cmb_region.addItem('(default)')
            self.cmb_region.addItems(list(self.doc.regions.keys()))
        idx = self.cmb_region.findText(current)
        self.cmb_region.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_region.blockSignals(False)
        self._filters_changed()

    def _filters_changed(self):
        self.proxy.set_filters(self.txt_filter.text(),
                               self.cmb_region.currentText())

    def _on_selection(self, *_):
        self.cue_selected.emit(self.current_cue())

    def _on_edit(self):
        self.cues_changed.emit()
        self.cue_selected.emit(self.current_cue())

    # ------------------------------------------------------------------ #
    def current_cue(self) -> Optional[Cue]:
        idx = self.table.selectionModel().currentIndex() \
            if self.table.selectionModel() else QModelIndex()
        rows = self.selected_rows()
        row = rows[0] if rows else (idx.row() if idx.isValid() else -1)
        if row < 0:
            return None
        src = self.proxy.mapToSource(self.proxy.index(row, 0))
        return self.model.cue_at(src.row())

    def selected_rows(self) -> List[int]:
        sel = self.table.selectionModel()
        if not sel:
            return []
        return sorted({i.row() for i in sel.selectedRows()})

    def selected_cues(self) -> List[Cue]:
        out = []
        for row in self.selected_rows():
            src = self.proxy.mapToSource(self.proxy.index(row, 0))
            cue = self.model.cue_at(src.row())
            if cue:
                out.append(cue)
        return out

    def select_cue(self, cue: Cue):
        for row in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(row, 0))
            if self.model.cue_at(src.row()) is cue:
                self.table.selectRow(row)
                return

    # ------------------------------------------------------------------ #
    def add_cue(self):
        if self.doc is None:
            return
        current = self.current_cue()
        start = current.end_ms if current else 0.0
        cue = Cue(begin_ms=start, end_ms=start + 2000.0)
        if current:
            cue.region_id = current.region_id
            cue.style_refs = list(current.style_refs)
            cue.lang = current.lang
        _set_plain_text(cue, 'New cue')
        self.doc.cues.append(cue)
        self.model.refresh_order()
        self.select_cue(cue)
        self.cues_changed.emit()

    def duplicate_selected(self):
        if self.doc is None:
            return
        for cue in self.selected_cues():
            self.doc.cues.append(cue.copy())
        self.model.refresh_order()
        self.cues_changed.emit()

    def delete_selected(self):
        if self.doc is None:
            return
        victims = set(id(c) for c in self.selected_cues())
        if not victims:
            return
        self.doc.cues = [c for c in self.doc.cues if id(c) not in victims]
        self.model.refresh_order()
        self.cues_changed.emit()
        self.cue_selected.emit(self.current_cue())

    def set_all_checked(self, state: bool):
        for row in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(row, 0))
            cue = self.model.cue_at(src.row())
            if cue:
                cue.enabled = state
        self.model.refresh_order()
        self.cues_changed.emit()

    # ------------------------------------------------------------------ #
    def open_time_tools(self):
        if self.doc is None:
            return
        dlg = TimeToolsDialog(self, has_selection=bool(self.selected_cues()))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        mode, amount_ms, plan = dlg.result_action()
        targets = self.doc.cues
        if mode == 'selected':
            targets = self.selected_cues()
        elif mode == 'after':
            cur = self.current_cue()
            if cur:
                targets = [c for c in self.doc.cues
                           if c.begin_ms >= cur.begin_ms]
        for cue in targets:
            if plan is not None:
                cue.begin_ms = plan.apply(cue.begin_ms)
                cue.end_ms = plan.apply(cue.end_ms)
            else:
                cue.begin_ms += amount_ms
                cue.end_ms += amount_ms
        self.model.refresh_order()
        self.cues_changed.emit()


class TimeToolsDialog(QDialog):
    """Shift by amount (all / selected / after selected) or fps conform."""

    def __init__(self, parent=None, has_selection: bool = False):
        super().__init__(parent)
        self.setWindowTitle('Time tools')
        lay = QVBoxLayout(self)

        # -- shift group ------------------------------------------------ #
        self.rb_shift = QRadioButton('Shift by amount (ms)')
        self.rb_shift.setChecked(True)
        lay.addWidget(self.rb_shift)
        form = QFormLayout()
        self.spin_ms = QDoubleSpinBox()
        self.spin_ms.setRange(-3_600_000, 3_600_000)
        self.spin_ms.setDecimals(0)
        self.spin_ms.setSingleStep(100)
        form.addRow('Amount:', self.spin_ms)
        self.cmb_scope = QComboBox()
        self.cmb_scope.addItems(['All cues', 'Selected cues',
                                 'Selected + all after'])
        if not has_selection:
            self.cmb_scope.setCurrentIndex(0)
        form.addRow('Apply to:', self.cmb_scope)
        lay.addLayout(form)

        # -- conform group ---------------------------------------------- #
        self.rb_conform = QRadioButton(
            'Frame-rate conform (subtitle master fps → video fps)')
        lay.addWidget(self.rb_conform)
        form2 = QFormLayout()
        self.cmb_src = QComboBox()
        self.cmb_dst = QComboBox()
        for label, frac in COMMON_RATES:
            self.cmb_src.addItem(label, frac)
            self.cmb_dst.addItem(label, frac)
        self.cmb_src.setCurrentIndex(0)
        self.cmb_dst.setCurrentIndex(1)
        form2.addRow('Source fps:', self.cmb_src)
        form2.addRow('Target fps:', self.cmb_dst)
        self.lbl_note = QLabel(
            'Rescales every timestamp by src/dst. Use when the subtitle '
            'was authored against a different frame rate than the video '
            '(e.g. 25 fps PAL subs on a 23.976 video). 29.97 telecine of '
            'a 23.976 master keeps real time — no conform needed.')
        self.lbl_note.setWordWrap(True)
        form2.addRow(self.lbl_note)
        lay.addLayout(form2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def result_action(self):
        if self.rb_conform.isChecked():
            src: Fraction = self.cmb_src.currentData()
            dst: Fraction = self.cmb_dst.currentData()
            return 'all', 0.0, RetimePlan.conform(src, dst)
        scope = ['all', 'selected', 'after'][self.cmb_scope.currentIndex()]
        return scope, self.spin_ms.value(), None
