import traceback
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableView,
                             QLineEdit, QCheckBox, QPushButton, QHeaderView, QComboBox,
                             QStyledItemDelegate, QAbstractItemView, QLabel)
from PyQt6.QtCore import Qt, QAbstractTableModel, QSortFilterProxyModel, pyqtSignal

from .utils import format_cue_text


# --- NEW: Custom Proxy for Multi-Column Filtering ---
class MultiColumnFilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.region_filter = "All Regions"
        self.text_filter = ""

    def set_region_filter(self, region_name):
        self.region_filter = region_name
        self.invalidateFilter()  # Trigger re-check

    def set_text_filter(self, text):
        self.text_filter = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()

        # 1. Check Region (Column 2)
        if self.region_filter != "All Regions":
            idx_region = model.index(source_row, 2, source_parent)
            region_data = model.data(idx_region, Qt.ItemDataRole.DisplayRole)
            # Handle potential None or mismatch
            r_str = str(region_data) if region_data else "(Default)"
            if r_str != self.region_filter:
                return False

        # 2. Check Text (Column 5)
        if self.text_filter:
            idx_text = model.index(source_row, 5, source_parent)
            text_data = model.data(idx_text, Qt.ItemDataRole.DisplayRole)
            if not text_data or self.text_filter not in str(text_data).lower():
                return False

        return True


class RegionDelegate(QStyledItemDelegate):
    def __init__(self, region_names, parent=None):
        super().__init__(parent)
        self.region_names = region_names

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        editor.addItems(self.region_names)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.ItemDataRole.EditRole)
        if value:
            editor.setCurrentText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class CuesModel(QAbstractTableModel):
    def __init__(self, cues, region_map):
        super().__init__()
        self._cues = cues
        self._region_map = region_map
        # REMOVED self._checked = [True] * len(cues)
        self._headers = ["", "#", "Region", "Start", "End", "Text"]

    def rowCount(self, parent=None):
        return len(self._cues)

    def columnCount(self, parent=None):
        return len(self._headers)

    def data(self, index, role):
        cue = self._cues[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1: return str(index.row() + 1)
            if col == 2: return cue.region.id if cue.region else "(Default)"
            if col == 3: return self._ms_to_tc(cue.start_ms)
            if col == 4: return self._ms_to_tc(cue.end_ms)
            if col == 5: return format_cue_text(cue.fragments)

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if cue.active else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.EditRole and col == 2:
            return cue.region.id if cue.region else ""

        return None

    def setData(self, index, value, role):
        # CHANGED: Write to cue.active
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            # Handle both integer (from view) and Enum (manual) inputs
            val_int = value.value if hasattr(value, 'value') else value
            self._cues[index.row()].active = (val_int == Qt.CheckState.Checked.value)
            self.dataChanged.emit(index, index)
            return True

        if role == Qt.ItemDataRole.EditRole and index.column() == 2:
            if value in self._region_map:
                self._cues[index.row()].region = self._region_map[value]
                self.dataChanged.emit(index, index)
                return True

        return False

    def headerData(self, section, orientation, role):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() == 2:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def _ms_to_tc(self, ms):
        s = ms / 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        frac = int((ms % 1000))
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{frac:03d}"


class CuesPane(QWidget):
    cue_selected = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout(self)

        # --- NEW CONTROL BAR ---
        controls = QHBoxLayout()

        # Checkbox Buttons
        self.btn_check_all = QPushButton("Check All")
        self.btn_uncheck_all = QPushButton("Uncheck All")

        # Filters
        self.cmb_filter_region = QComboBox()
        self.cmb_filter_region.addItem("All Regions")
        self.txt_filter_text = QLineEdit()
        self.txt_filter_text.setPlaceholderText("Search Text...")

        controls.addWidget(self.btn_check_all)
        controls.addWidget(self.btn_uncheck_all)
        controls.addWidget(QLabel("  Region:"))
        controls.addWidget(self.cmb_filter_region)
        controls.addWidget(QLabel("  Text:"))
        controls.addWidget(self.txt_filter_text)

        self.layout.addLayout(controls)

        # Table
        self.table = QTableView()
        self.table.setStyleSheet("""
            QTableView {
                selection-background-color: #505050;
                selection-color: white;
                outline: none;
            }
            QTableView::item:focus {
                background-color: transparent; 
                border: none;
            }
            QTableView::item:selected {
                background-color: #505050;
                color: white;
            }
            QTableView::item:selected:focus {
                background-color: #505050;
                color: white;
            }
        """)

        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)

        self.layout.addWidget(self.table)

        # Connections
        self.btn_check_all.clicked.connect(lambda: self.set_checked_visible(True))
        self.btn_uncheck_all.clicked.connect(lambda: self.set_checked_visible(False))

        # Filter Connections
        self.cmb_filter_region.currentTextChanged.connect(self.on_region_filter_changed)
        self.txt_filter_text.textChanged.connect(self.on_text_filter_changed)

        # Use Custom Proxy
        self.proxy = MultiColumnFilterProxy()

    def load_project(self, project):
        try:
            self.project = project

            if hasattr(project, 'regions'):
                region_map = project.regions
            elif hasattr(project, 'head') and hasattr(project.head, 'layout'):
                region_map = project.head.layout.regions
            else:
                region_map = {}

            region_names = list(region_map.keys())

            # Populate Filter Dropdown
            self.cmb_filter_region.blockSignals(True)  # Prevent triggering filter during load
            self.cmb_filter_region.clear()
            self.cmb_filter_region.addItem("All Regions")
            self.cmb_filter_region.addItems(region_names)
            self.cmb_filter_region.blockSignals(False)

            self.model = CuesModel(project.body.cues, region_map)
            self.proxy.setSourceModel(self.model)

            self.table.setModel(self.proxy)

            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            # Connect the NEW selection model
            # No need to disconnect; this is a fresh object.
            if self.table.selectionModel():
                self.table.selectionModel().selectionChanged.connect(self.on_selection_change)

            delegate = RegionDelegate(region_names, self.table)
            self.table.setItemDelegateForColumn(2, delegate)

            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            self.table.setColumnWidth(0, 30)

            if self.model.rowCount() > 0:
                self.table.selectRow(0)

        except Exception as e:
            print(f"[ERROR] CuesPane.load_project crash: {e}")
            traceback.print_exc()

    def on_region_filter_changed(self, text):
        self.proxy.set_region_filter(text)

    def on_text_filter_changed(self, text):
        self.proxy.set_text_filter(text)

    def set_checked_visible(self, state):
        # Calculate the integer value for the signal
        val = Qt.CheckState.Checked.value if state else Qt.CheckState.Unchecked.value

        # Update the model data (which now updates cue.active)
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            src_idx = self.proxy.mapToSource(idx)
            self.model.setData(src_idx, val, Qt.ItemDataRole.CheckStateRole)

    def on_selection_change(self, selected, deselected):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes: return

        indexes.sort(key=lambda i: i.row())
        proxy_idx = indexes[0]

        src_idx = self.proxy.mapToSource(proxy_idx)
        if src_idx.isValid():
            cue = self.model._cues[src_idx.row()]
            self.cue_selected.emit(cue)

    def get_checked_cue_ids(self):
        ids = set()
        for i, checked in enumerate(self.model._checked):
            if checked:
                ids.add(self.model._cues[i].start_ms)
        return ids