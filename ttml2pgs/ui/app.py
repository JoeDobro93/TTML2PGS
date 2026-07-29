"""Application bootstrap: QApplication, dark palette, main window."""

from __future__ import annotations

import os
import sys


def apply_dark_theme(app):
    from PyQt6.QtGui import QColor, QPalette
    app.setStyle('Fusion')
    p = QPalette()
    bg = QColor(45, 45, 48)
    base = QColor(30, 30, 32)
    text = QColor(224, 224, 224)
    p.setColor(QPalette.ColorRole.Window, bg)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(38, 38, 40))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(58, 58, 62))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(70, 100, 160))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 54))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
    disabled = QColor(120, 120, 120)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
               disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText,
               disabled)
    app.setPalette(p)
    app.setStyleSheet("""
        QToolTip { color: #e0e0e0; background: #323236;
                   border: 1px solid #555; }
        QSplitter::handle { background: #3a3a3e; }
        QHeaderView::section { background: #3a3a3e; color: #d0d0d0;
                               border: 0; border-right: 1px solid #2a2a2c;
                               padding: 4px; }
        QTableView { gridline-color: #3c3c40; }
        QGroupBox { border: 1px solid #4a4a4e; border-radius: 4px;
                    margin-top: 1.1em; padding-top: 0.3em; }
        QGroupBox::title { subcontrol-origin: margin; left: 8px;
                           padding: 0 3px; }
        QProgressBar { border: 1px solid #4a4a4e; border-radius: 3px;
                       text-align: center; background: #2a2a2c; }
        QProgressBar::chunk { background: #4a7ab8; }
    """)


def main() -> int:
    # Quiet the Qt Multimedia FFmpeg decoder's per-frame chatter — e.g.
    # Dolby Vision streams log "Skipping NAL unit 62/63" for every frame
    # (the DV metadata NALs FFmpeg doesn't consume). Harmless, but it
    # floods the console. A user-set QT_LOGGING_RULES is respected.
    if 'QT_LOGGING_RULES' not in os.environ:
        os.environ['QT_LOGGING_RULES'] = 'qt.multimedia.ffmpeg.*=false'

    # Windows: unique taskbar identity
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'ttml2pgs.app.2')
        except Exception:
            pass

    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName('TTML2PGS')
    apply_dark_theme(app)

    icon_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'resources', 'icon.ico')
    if os.path.exists(icon_path):
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))

    from .main_window import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()
