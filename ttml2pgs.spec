# PyInstaller build for TTML2PGS 2 (one-folder, windowed).
#
#   pip install pyinstaller
#   pyinstaller ttml2pgs.spec
#
# → dist/TTML2PGS/TTML2PGS(.exe) — a fully standalone app folder you
# can move anywhere and pin a shortcut to. ffmpeg/ffprobe and mkvmerge
# are still expected on PATH (or drop them into the dist folder).
# libmpv-2.dll stays optional (Preferences → Player → libmpv folder).
#
# Rebuild after every update; while you're still iterating, the
# no-build route (`python make_shortcut.py`) is usually more practical.

import importlib.util
import os

block_cipher = None

# 'mpv' is imported lazily inside the player widget — tell PyInstaller
# about it, but only if it's installed in the build environment.
hidden = [m for m in ('mpv',) if importlib.util.find_spec(m)]

a = Analysis(
    ['run_gui.py'],
    pathex=[],
    binaries=[],
    datas=[(os.path.join('resources', 'icon.ico'), 'resources')],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TTML2PGS',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join('resources', 'icon.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='TTML2PGS',
)
