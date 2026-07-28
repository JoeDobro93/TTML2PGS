"""
Launcher for the TTML2PGS 2 GUI.

Exists so IDEs (PyCharm's play button, VS Code's Run) can start the v2
app as a plain script — equivalent to ``python -m ttml2pgs``. The legacy
v1 app remains at ``main.py``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ttml2pgs.__main__ import run   # noqa: E402

if __name__ == '__main__':
    run()
