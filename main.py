import sys
import os
import ctypes # Required for Taskbar Icon fix on Windows
# Ensure we can find core modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow
from PyQt6.QtGui import QIcon # Required to load the icon

if __name__ == "__main__":
    # This ID makes Windows treat this as a unique app, not just "Python"
    myappid = 'mycompany.ttml2pgs.ultimate.1.0'
    if os.name == 'nt':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass
    # --------------------------------

    app = QApplication(sys.argv)

    # Optional: Set a dark theme or style here
    app.setStyle("Fusion")

    # --- SET APPLICATION ICON ---
    # Load the icon from the resources folder we just populated
    icon_path = os.path.join(os.path.dirname(__file__), "resources", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    # ----------------------------
    window = MainWindow()
    window.show()

    sys.exit(app.exec())