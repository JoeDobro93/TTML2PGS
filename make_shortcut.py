"""
Create a launcher shortcut for TTML2PGS 2 — so the app opens like any
installed program, no IDE needed.

    python make_shortcut.py                # Desktop shortcut
    python make_shortcut.py --start-menu   # + Start Menu entry (Windows)

The shortcut points at THIS Python environment's windowed interpreter
(pythonw.exe on Windows — no console window) running run_gui.py, so a
plain `git pull` updates the app with no rebuild: the shortcut keeps
working. Run this script from the same interpreter/venv you use in
PyCharm (PyCharm terminal: `python make_shortcut.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, 'run_gui.py')
ICON = os.path.join(ROOT, 'resources', 'icon.ico')
NAME = 'TTML2PGS 2'


def _windowed_python() -> str:
    """pythonw.exe next to the current interpreter (no console box)."""
    exe = sys.executable
    if os.name == 'nt':
        cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(cand):
            return cand
    return exe


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def make_windows(start_menu: bool) -> int:
    target = _windowed_python()
    lines = ["$W = New-Object -ComObject WScript.Shell",
             f"$dirs = @($W.SpecialFolders('Desktop'))"]
    if start_menu:
        lines.append("$dirs += Join-Path $W.SpecialFolders('Programs') ''")
    lines += [
        "foreach ($d in $dirs) {",
        f"  $S = $W.CreateShortcut((Join-Path $d '{NAME}.lnk'))",
        f"  $S.TargetPath = '{_ps_quote(target)}'",
        f"  $S.Arguments = '\"{_ps_quote(SCRIPT)}\"'",
        f"  $S.WorkingDirectory = '{_ps_quote(ROOT)}'",
        f"  $S.IconLocation = '{_ps_quote(ICON)}'",
        "  $S.Description = 'TTML2PGS 2 - subtitle to PGS renderer'",
        "  $S.Save()",
        "  Write-Output ('Created ' + (Join-Path $d '" + NAME + ".lnk'))",
        "}",
    ]
    r = subprocess.run(['powershell', '-NoProfile', '-Command',
                        '; '.join(lines)],
                       capture_output=True, text=True)
    out = (r.stdout or '') + (r.stderr or '')
    print(out.strip())
    return r.returncode


def make_linux() -> int:
    apps = os.path.expanduser('~/.local/share/applications')
    os.makedirs(apps, exist_ok=True)
    # .desktop icons want png — convert the .ico once via Qt if possible
    icon_png = os.path.join(ROOT, 'resources', 'icon.png')
    if not os.path.exists(icon_png) and os.path.exists(ICON):
        try:
            os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
            from PyQt6.QtGui import QImage
            from PyQt6.QtWidgets import QApplication
            _app = QApplication.instance() or QApplication([])
            QImage(ICON).save(icon_png)
        except Exception:
            icon_png = ''
    path = os.path.join(apps, 'ttml2pgs.desktop')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"""[Desktop Entry]
Type=Application
Name={NAME}
Comment=Subtitle → PGS renderer
Exec={sys.executable} "{SCRIPT}"
Path={ROOT}
{f'Icon={icon_png}' if icon_png and os.path.exists(icon_png) else ''}
Terminal=false
Categories=AudioVideo;Video;
""")
    os.chmod(path, 0o755)
    print(f'Created {path} (shows up in your app launcher; '
          f'copy it to ~/Desktop if you want a desktop icon)')
    return 0


def main() -> int:
    start_menu = '--start-menu' in sys.argv
    if not os.path.exists(SCRIPT):
        print('run_gui.py not found next to this script', file=sys.stderr)
        return 1
    if os.name == 'nt':
        return make_windows(start_menu)
    if sys.platform == 'darwin':
        print('macOS: drag run_gui.py onto Automator ("Run Shell Script" '
              f'with: {sys.executable} "{SCRIPT}") and save as an '
              'Application, or use a PyInstaller build (see README).')
        return 0
    return make_linux()


if __name__ == '__main__':
    raise SystemExit(main())
