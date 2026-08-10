"""
Launcher for the TTML2PGS GUI.

Exists so IDEs (PyCharm's play button, VS Code's Run) can start the
app as a plain script — equivalent to ``python -m ttml2pgs``.

Running from SOURCE also refreshes the standalone executable in the
background (make_exe.py — fingerprint-gated, so unchanged sources cost
nothing and PyInstaller is only invoked after edits). The frozen
executable itself never tries to rebuild anything.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from ttml2pgs.__main__ import run   # noqa: E402


def _refresh_exe_in_background():
    """Keep dist/TTML2PGS current with the source tree (IDE runs only)."""
    if getattr(sys, 'frozen', False):
        return
    script = os.path.join(ROOT, 'make_exe.py')
    if not os.path.exists(script):
        return
    try:
        os.makedirs(os.path.join(ROOT, 'build'), exist_ok=True)
        log = open(os.path.join(ROOT, 'build', 'auto_build.log'), 'w')
        flags = 0x08000000 if os.name == 'nt' else 0   # no console box
        subprocess.Popen([sys.executable, script],
                         cwd=ROOT, stdout=log, stderr=log,
                         creationflags=flags)
    except OSError:
        pass                        # never block the app on the builder


if __name__ == '__main__':
    _refresh_exe_in_background()
    run()
