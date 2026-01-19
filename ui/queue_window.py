from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QProgressBar, QLabel, QListWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush
import os

class QueueWindow(QDialog):
    def __init__(self, parent=None):
        print("[DEBUG] QueueWindow.__init__ started")
        super().__init__(parent)
        self.setWindowTitle("Render Queue")
        self.resize(400, 300)

        layout = QVBoxLayout(self)

        self.lbl_status = QLabel("Queue Status:")
        layout.addWidget(self.lbl_status)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        print("[DEBUG] QueueWindow.__init__ finished")

    def update_queue(self, completed_jobs, current_job_config, pending_jobs):
        # print(f"[DEBUG] QueueWindow.update_queue called. Queue size: {len(pending_jobs)}")
        try:
            v_bar = self.list_widget.verticalScrollBar()
            # Check if we are currently at the bottom (allow a tiny margin of error)
            was_at_bottom = v_bar.value() >= (v_bar.maximum() - 5)
            saved_value = v_bar.value()

            self.list_widget.clear()

            # 1. COMPLETED (Green)
            for job in completed_jobs:
                name = os.path.basename(job.get('out_filename', 'Unknown'))
                item = QListWidgetItem(f"✔ COMPLETED: {name}")
                item.setForeground(QBrush(QColor("#00AA00")))  # Dark Green
                self.list_widget.addItem(item)

            # 2. PROCESSING (Blue/Bold)
            if current_job_config:
                name = os.path.basename(current_job_config.get('out_filename', 'Unknown'))
                item = QListWidgetItem(f"▶ PROCESSING: {name}")
                item.setForeground(QBrush(QColor("#0000FF")))  # Blue
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                self.list_widget.addItem(item)

            # 3. PENDING (Gray)
            for job in pending_jobs:
                name = os.path.basename(job.get('out_filename', 'Unknown'))
                item = QListWidgetItem(f"• PENDING: {name}")
                item.setForeground(QBrush(QColor("#666666")))  # Gray
                self.list_widget.addItem(item)

            # Auto-scroll to bottom to show latest activity if list is long
            if was_at_bottom:
                self.list_widget.scrollToBottom()
            else:
                v_bar.setValue(saved_value)
            # print("[DEBUG] QueueWindow.update_queue finished")
        except Exception as e:
            print(f"[ERROR] QueueWindow.update_queue crash: {e}")

    def update_progress(self, val):
        try:
            self.progress_bar.setValue(val)
        except Exception as e:
            print(f"[ERROR] QueueWindow.update_progress crash: {e}")

    def closeEvent(self, event):
        print("[DEBUG] QueueWindow.closeEvent")
        event.ignore()
        self.hide()