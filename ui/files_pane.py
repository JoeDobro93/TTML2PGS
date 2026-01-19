import os
import traceback
import subprocess
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
                             QPushButton, QFileDialog, QHeaderView, QHBoxLayout,
                             QRadioButton, QButtonGroup, QCheckBox, QAbstractItemView,
                             QStyledItemDelegate, QStyle, QMenu)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPen, QColor, QBrush

from core.ingest import TTMLIngester, WebVTTIngester
from .utils import get_video_metadata


# --- Custom Header to hide specific sections ---
class SelectiveHeaderView(QHeaderView):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        # We don't use a stylesheet here so we can control painting manually if needed,
        # OR we can just use a stylesheet that makes everything flat and rely on the
        # empty text to sell the effect.

        # Actually, to get "Total Transparency" for some and "Default Look" for others
        # is very hard with standard Stylesheets.
        # Strategy: Style the whole thing to look minimal and flat (Dark Grey),
        # but make the border color match the background for the empty ones.
        pass

    def paintSection(self, painter, rect, logicalIndex):
        if logicalIndex in [0, 1, 2, 12, 13]:
            painter.save()
            painter.fillRect(rect, QColor("#353535"))
            painter.restore()
        else:
            super().paintSection(painter, rect, logicalIndex)

#
# Delegate for Data Columns: Draws a border to look like a grid
class DataBorderDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)

        painter.save()
        # Draw border around data cells
        pen = QPen(QColor("#505050"))
        pen.setWidth(1)
        painter.setPen(pen)

        # Adjust rect slightly to prevent overlap clipping
        r = option.rect
        painter.drawRect(r.x(), r.y(), r.width(), r.height())
        painter.restore()


# Delegate for Control Columns: Prevents Blue Highlight
class ControlDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # Create a copy of the style option
        opt = option
        # Remove the 'Selected' state so it never highlights blue
        opt.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, index)


class FilesPane(QWidget):
    project_loaded = pyqtSignal(object, tuple, bool, tuple)
    run_current = pyqtSignal(dict)
    run_batch = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout(self)

        # Toolbar
        tb = QHBoxLayout()
        btn_add = QPushButton("Add Subtitle")
        btn_add_folder = QPushButton("Add Folder")
        tb.addWidget(btn_add)
        tb.addWidget(btn_add_folder)
        tb.addStretch()
        self.layout.addLayout(tb)

        # --- TABLE SETUP ---
        # 0: Active | 1: Input | 2: Gap | 3-7: Data | 8: Gap | 9: Del
        self.table = QTableWidget()
        self.table.setColumnCount(14)

        # Install Custom Header
        self.header = SelectiveHeaderView(Qt.Orientation.Horizontal, self.table)
        self.table.setHorizontalHeader(self.header)

        headers = ["", "", "", "Subtitle File", "Video File", "HDR", "Src FPS", "Tgt FPS",
                   "Tgt W", "Tgt H", "Offset (ms)", "Output Name", "", ""]
        self.table.setHorizontalHeaderLabels(headers)

        # --- STYLE: Data Headers look like buttons, Control Headers are flat ---
        # We apply a generic style that looks good for the Data columns.
        # The Custom Header class (SelectiveHeaderView) handles "hiding" the others by painting over them.
        self.table.horizontalHeader().setStyleSheet("""
                    QHeaderView::section { 
                        background-color: #454545; 
                        color: #e0e0e0; 
                        border: 1px solid #505050;
                        border-top: 0px;
                        padding: 4px;
                        font-weight: bold;
                    }
                """)

        # Table Properties
        # Set background to match the header "transparent" color so they blend
        self.table.setStyleSheet("QTableWidget { background-color: #353535; border: none; }")

        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)

        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)  # HDR
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)  # Src
        h.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)  # Tgt FPS
        h.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)  # Tgt W
        h.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)  # Tgt H
        h.setSectionResizeMode(10, QHeaderView.ResizeMode.Fixed)  # Offset (Shifted)
        h.setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)  # Output (Shifted)
        h.setSectionResizeMode(12, QHeaderView.ResizeMode.Fixed)  # Gap (Shifted)
        h.setSectionResizeMode(13, QHeaderView.ResizeMode.Fixed)  # Del (Shifted)

        # Widths
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 20)
        self.table.setColumnWidth(5, 50)
        self.table.setColumnWidth(8, 60)  # W
        self.table.setColumnWidth(9, 60)  # H
        self.table.setColumnWidth(10, 80)  # Offset
        self.table.setColumnWidth(12, 20)
        self.table.setColumnWidth(13, 40)

        # --- DELEGATES ---
        data_delegate = DataBorderDelegate(self.table)
        control_delegate = ControlDelegate(self.table)

        # Control Columns: 0, 1, 2, 9, 10
        for c in [0, 1, 2, 12, 13]:
            self.table.setItemDelegateForColumn(c, control_delegate)
        # Data: 3..9
        for c in range(3, 12):
            self.table.setItemDelegateForColumn(c, data_delegate)

        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.layout.addWidget(self.table)

        self.radio_group = QButtonGroup()
        self.radio_group.buttonToggled.connect(self.on_active_changed)

        # --- NEW: Context Menu for "Fill Down" --- MAKE SURE THIS IS IN RIGHT PLACE TODO:
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)

        # --- NEW: Detect Edits ---
        self.table.itemChanged.connect(self.on_data_changed)

        # Bottom Bar
        bb = QHBoxLayout()
        self.btn_run_curr = QPushButton("Run (Current)")
        self.btn_run_batch = QPushButton("Run (Batch)")
        self.chk_sel_only = QCheckBox("Render Only Selected Cues")
        bb.addWidget(self.btn_run_curr)
        bb.addWidget(self.btn_run_batch)
        bb.addWidget(self.chk_sel_only)
        self.layout.addLayout(bb)

        # Connections
        btn_add.clicked.connect(self.add_file)
        btn_add_folder.clicked.connect(self.add_folder)
        self.btn_run_curr.clicked.connect(self.emit_run_current)
        self.btn_run_batch.clicked.connect(self.emit_run_batch)

    def add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Subtitle", "", "Subtitles (*.ttml *.vtt)")
        if path: self._ingest_row(path, auto_match=True)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            for f in os.listdir(folder):
                if f.lower().endswith((".ttml", ".vtt")):
                    self._ingest_row(os.path.join(folder, f), auto_match=True)

    def _ingest_row(self, sub_path, auto_match=False):
        try:
            if sub_path.lower().endswith(".vtt"):
                project = WebVTTIngester().parse(sub_path)
            else:
                project = TTMLIngester().parse(sub_path)
        except Exception as e:
            print(f"[ERROR] Failed to load {sub_path}: {e}")
            traceback.print_exc()
            return

        row = self.table.rowCount()
        self.table.insertRow(row)

        # --- DATA ---
        target_fps = (project.fps_num, project.fps_den)
        src_fps_str = f"{project.fps_num / project.fps_den:.3f}"

        vid_path = None
        if auto_match:
            try:
                folder = os.path.dirname(sub_path)
                sub_name = os.path.basename(sub_path)
                # Robust Stem Logic
                if '.' in sub_name:
                    stem = sub_name.split('.', 1)[0]
                else:
                    stem = os.path.splitext(sub_name)[0]

                for f in os.listdir(folder):
                    if f.lower().endswith((".mp4", ".mkv")):
                        if f.startswith(f"{stem}.") or os.path.splitext(f)[0] == stem:
                            # CHANGE: normpath ensures correct OS separators (fixes ffprobe issues)
                            vid_path = os.path.normpath(os.path.join(folder, f))
                            print(f"[DEBUG] Auto-matched video: {vid_path}")
                            break
            except Exception as e:
                print(f"[ERROR] Auto-match failed: {e}")

        out_filename, out_dir = self._calc_output(sub_path, vid_path, project)

        row_data = {
            "project": project,
            "sub_path": sub_path,
            "video_path": vid_path,
            "out_dir": out_dir,
            "out_filename": out_filename,
            "target_fps": target_fps,
            "target_res": (1920, 1080), #default resolution
            "is_hdr": False,
            "offset_ms": 0  # Default Offset
        }

        # --- Helper to create non-selectable blank items ---
        def create_blank_item():
            it = QTableWidgetItem("")
            # Disable selection flag so logic matches visual
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            return it

        # --- FILL COLUMNS ---

        # 0: Active (Radio)
        self.table.setItem(row, 0, create_blank_item())
        container = QWidget()
        lay = QHBoxLayout(container);
        lay.setContentsMargins(0, 0, 0, 0);
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rb = QRadioButton()
        lay.addWidget(rb)
        self.table.setCellWidget(row, 0, container)
        self.radio_group.addButton(rb)

        # 1: Video Button
        self.table.setItem(row, 1, create_blank_item())
        btn_vid = QPushButton("Load Video...")
        btn_vid.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_vid.clicked.connect(lambda: self.select_video_by_widget(btn_vid))
        self.table.setCellWidget(row, 1, btn_vid)

        # 2: Spacer
        self.table.setItem(row, 2, create_blank_item())

        # 3: Subtitle File (DATA)
        item_sub = QTableWidgetItem(os.path.basename(sub_path))
        item_sub.setData(Qt.ItemDataRole.UserRole, row_data)
        item_sub.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 3, item_sub)

        # 4: Video File
        item_vid = QTableWidgetItem(os.path.basename(vid_path) if vid_path else "")
        item_vid.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 4, item_vid)

        # 5: HDR (New)
        item_hdr = QTableWidgetItem("-")
        item_hdr.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 5, item_hdr)

        item_src = QTableWidgetItem(src_fps_str)
        item_src.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 6, item_src)

        # 8 & 9: (Resolution)
        item_w = QTableWidgetItem("1920")
        item_w.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 8, item_w)

        item_h = QTableWidgetItem("1080")
        item_h.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 9, item_h)

        # 7 & 9: Tgt FPS & Output
        # out of order as to be able to overwrite the default resolution
        if vid_path:
            self._probe_and_update(row, vid_path, row_data)
        else:
            item_tgt = QTableWidgetItem(src_fps_str)
            item_tgt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 7, item_tgt)

            # FIX: Output Name must be at Index 9 (was 8)
            item_out = QTableWidgetItem(out_filename)
            item_out.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 11, item_out)

        # 10: Offset (NEW)
        item_offset = QTableWidgetItem("0")
        item_offset.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 10, item_offset)

        # 12: Spacer (Shifted)
        self.table.setItem(row, 12, create_blank_item())

        # 13: Delete (X) - Rightmost
        self.table.setItem(row, 13, create_blank_item())
        btn_del = QPushButton("X")

        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setFixedWidth(30)
        btn_del.setStyleSheet(
            "QPushButton { color: red; font-weight: bold; border: 1px solid #ccc; border-radius: 4px; background: white; } QPushButton:hover { background: #ffe6e6; }")
        btn_del.clicked.connect(lambda: self.delete_row_by_widget(btn_del))
        self.table.setCellWidget(row, 13, btn_del)

        # Init active if first
        if self.table.rowCount() == 1:
            rb.setChecked(True)

            self.project_loaded.emit(project, row_data['target_fps'], row_data.get('is_hdr', False), row_data['target_res'])

    def _calc_output(self, sub_path, vid_path, project):
        source = vid_path if vid_path else sub_path
        base = os.path.splitext(os.path.basename(source))[0]
        lang = project.language or "und"
        if base.endswith(f".{lang}"):
            base = base.rsplit(f".{lang}", 1)[0]

        filename = f"{base}.{lang}.sup"
        directory = os.path.dirname(vid_path) if vid_path else os.path.dirname(sub_path)
        return filename, directory

    def get_row_data(self, row):
        item = self.table.item(row, 3)
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def set_row_data(self, row, data):
        item = self.table.item(row, 3)
        if item:
            item.setData(Qt.ItemDataRole.UserRole, data)

    def delete_row_by_widget(self, widget):
        index = self.table.indexAt(widget.pos())
        if index.isValid():
            self.table.removeRow(index.row())

    def select_video_by_widget(self, widget):
        index = self.table.indexAt(widget.pos())
        if not index.isValid(): return
        row = index.row()

        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video (*.mp4 *.mkv)")
        if path:
            data = self.get_row_data(row)
            if not data: return

            data['video_path'] = path
            data['out_filename'], data['out_dir'] = self._calc_output(data['sub_path'], path, data['project'])

            self.table.setItem(row, 4, QTableWidgetItem(os.path.basename(path)))
            self.table.setItem(row, 11, QTableWidgetItem(data['out_filename']))

            self._probe_and_update(row, path, data)
            self.set_row_data(row, data)

    # --- HDR DETECTION HELPER ---
    def _detect_hdr(self, path):
        try:
            # We request color info AND side data (for Dolby Vision)
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=color_transfer,color_primaries,color_space,codec_tag_string,side_data_list",
                "-of", "json",
                path
            ]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            import json
            result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
            data = json.loads(result.stdout)

            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]

                # 1. Standard Metadata Check (PQ / HLG / BT.2020)
                transfer = stream.get('color_transfer', '').lower()
                primaries = stream.get('color_primaries', '').lower()

                hdr_markers = ['smpte2084', 'arib-std-b67', 'bt2020']
                for m in hdr_markers:
                    if m in transfer or m in primaries:
                        return True

                # 2. Side Data Check (Mastering Display Metadata / DoVi RPU)
                side_data = stream.get('side_data_list', [])
                for sd in side_data:
                    sd_type = sd.get('side_data_type', '').lower()
                    if any(x in sd_type for x in ['dovi', 'dolby', 'hdr10', 'mastering', 'content light']):
                        return True

                # 3. Codec Tag Check (Explicit DoVi tags)
                tag = stream.get('codec_tag_string', '').lower()
                if 'dvh1' in tag or 'dvhe' in tag:
                    return True

        except Exception as e:
            print(f"[WARN] HDR Detection Error: {e}")
            return False

        # --- METHOD 2: BINARY SCAN (The "MediaInfo" Strategy) ---
        # If FFProbe failed (blank metadata), we scan the raw file header.
        # Dolby Vision Profile 5 stores configuration in a 'dvcC' or 'dvvC' atom.
        # This is strictly robust: SDR files never contain this atom.
        try:
            with open(path, 'rb') as f:
                # Read the first 256KB (headers are usually at the start for MP4/MKV)
                # This is extremely fast (milliseconds) and low-memory.
                header_bytes = f.read(262144)

                # Search for Dolby Vision Configuration Box signatures
                # dvcC = Dolby Vision Config (Profiles 5, 7, etc.)
                # dvvC = Dolby Vision Config (Profiles 8, 9)
                if b'dvcC' in header_bytes or b'dvvC' in header_bytes:
                    print("[HDR DETECT] Binary Scan: Found Dolby Vision Atom (dvcC/dvvC)")
                    return True

        except Exception as e:
            print(f"[WARN] HDR Detection (Binary) Error: {e}")

        return False

    def _probe_and_update(self, row, vid_path, data):
        meta = get_video_metadata(vid_path)
        if meta:
            w, h, num, den = meta
            fps_str = f"{num / den:.3f}"

            num_i = int(num)
            den_i = int(den)
            data['target_fps'] = (num_i, den_i)
            data['target_res'] = (w, h)

            item_w = QTableWidgetItem(str(w))
            item_w.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 8, item_w)

            item_h = QTableWidgetItem(str(h))
            item_h.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 9, item_h)

            # 1. Detect HDR
            is_hdr = self._detect_hdr(vid_path)
            data['is_hdr'] = is_hdr

            item_hdr = self.table.item(row, 5)
            if item_hdr:
                item_hdr.setText("YES" if is_hdr else "NO")
                if is_hdr:
                    item_hdr.setForeground(QBrush(QColor("#00FF00")))
                else:
                    item_hdr.setForeground(QBrush(QColor("#888888")))

            # 2. VTT Logic
            if data['sub_path'].lower().endswith('.vtt'):
                data['project'].fps_num = num_i
                data['project'].fps_den = den_i
                item_src = self.table.item(row, 6)  # FIX 11: Correct Index 6
                if item_src: item_src.setText(fps_str)

            self.set_row_data(row, data)

            item_fps = QTableWidgetItem(fps_str)
            item_fps.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 7, item_fps)  # FIX 12: Correct Index 7

            item_out = QTableWidgetItem(data['out_filename'])
            item_out.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 11, item_out)

            # Update Main
            container = self.table.cellWidget(row, 0)
            if container:
                rb = container.findChild(QRadioButton)
                if rb and rb.isChecked():
                    # FIX 14: Emit is_hdr
                    self.project_loaded.emit(data['project'], data['target_fps'], is_hdr, data['target_res'])

    def on_active_changed(self, btn, checked):
        if checked:
            for row in range(self.table.rowCount()):
                container = self.table.cellWidget(row, 0)
                if container:
                    rb = container.findChild(QRadioButton)
                    if rb == btn:
                        data = self.get_row_data(row)
                        if data:
                            # FIX 15: Emit is_hdr
                            self.project_loaded.emit(data['project'], data['target_fps'], data.get('is_hdr', False), data['target_res'])
                        break

    def emit_run_current(self):
        for row in range(self.table.rowCount()):
            container = self.table.cellWidget(row, 0)
            if container:
                rb = container.findChild(QRadioButton)
                if rb and rb.isChecked():
                    data = self.get_row_data(row)
                    if data:
                        cfg = data.copy()
                        cfg['selected_only'] = self.chk_sel_only.isChecked()
                        self.run_current.emit(cfg)
                    return

    def emit_run_batch(self):
        batch_list = []
        for row in range(self.table.rowCount()):
            data = self.get_row_data(row)
            if data:
                cfg = data.copy()
                cfg['selected_only'] = False
                batch_list.append(cfg)
        self.run_batch.emit(batch_list)

    def on_data_changed(self, item):
        row = item.row()
        col = item.column()

        data = self.get_row_data(row)  # Helper call needed here if not already present in method scope
        if not data: return

        if col == 8 or col == 9:
            try:
                val = int(item.text())
                curr_w, curr_h = data.get('target_res', (1920, 1080))
                if col == 8:
                    data['target_res'] = (val, curr_h)
                else:
                    data['target_res'] = (curr_w, val)
                self.set_row_data(row, data)
            except ValueError:
                pass  # Optionally revert to old value

        # Column 10 is Offset
        if col == 10:
            try:
                val = int(item.text())
            except ValueError:
                val = 0
                # Block signals to prevent recursion when resetting invalid text
                self.table.blockSignals(True)
                item.setText("0")
                self.table.blockSignals(False)

            # Update row_data
            data = self.get_row_data(row)
            if data:
                data['offset_ms'] = val
                self.set_row_data(row, data)

    def show_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid(): return

        # Only allow context menu on Offset column (8)
        if index.column() != 10: return

        menu = QMenu()
        fill_action = menu.addAction("Fill Down to Selected")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))

        if action == fill_action:
            self.fill_offset_selection()

    def fill_offset_selection(self):
        selected = self.table.selectedItems()
        # Filter only items in Offset column
        offsets = [i for i in selected if i.column() == 10]

        if not offsets: return

        # Sort by row to find the "top" one
        offsets.sort(key=lambda i: i.row())

        # Top value is the source
        source_val = offsets[0].text()

        # Apply to others
        self.table.blockSignals(True)  # Optimization
        for item in offsets[1:]:
            item.setText(source_val)
            # Manually trigger data update since we blocked signals
            row = item.row()
            data = self.get_row_data(row)
            if data:
                try:
                    data['offset_ms'] = int(source_val)
                    self.set_row_data(row, data)
                except:
                    pass
        self.table.blockSignals(False)