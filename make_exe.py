"""
Build (or refresh) the standalone executable — dist/TTML2PGS.

    python make_exe.py                # build only if sources changed
    python make_exe.py --force       # rebuild unconditionally
    python make_exe.py --check-only  # exit 0 = up to date, 1 = stale

run_gui.py spawns this in the background on every IDE launch (never
when running AS the frozen executable), so the exe your shortcuts
point at silently stays current. A fingerprint of the source tree is
stored next to the exe; unchanged sources exit in milliseconds
without invoking PyInstaller.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, 'dist', 'TTML2PGS')
STAMP = os.path.join(DIST, '.build_stamp')


def fingerprint() -> str:
    """Hash of every build input's path/mtime/size."""
    h = hashlib.sha256()
    inputs = [os.path.join(ROOT, 'run_gui.py'),
              os.path.join(ROOT, 'ttml2pgs.spec'),
              os.path.join(ROOT, 'resources', 'icon.ico')]
    for base, _dirs, files in os.walk(os.path.join(ROOT, 'ttml2pgs')):
        if '__pycache__' in base:
            continue
        for fn in sorted(files):
            if fn.endswith('.py'):
                inputs.append(os.path.join(base, fn))
    for p in sorted(inputs):
        try:
            st = os.stat(p)
        except OSError:
            continue
        h.update(f'{os.path.relpath(p, ROOT)}|{st.st_mtime_ns}|'
                 f'{st.st_size}\n'.encode())
    return h.hexdigest()


def exe_path() -> str:
    name = 'TTML2PGS.exe' if os.name == 'nt' else 'TTML2PGS'
    return os.path.join(DIST, name)


def is_current() -> bool:
    if not os.path.exists(exe_path()):
        return False
    try:
        with open(STAMP, 'r', encoding='utf-8') as f:
            return f.read().strip() == fingerprint()
    except OSError:
        return False


def build() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('make_exe: PyInstaller not installed — skipping '
              '(pip install pyinstaller to enable the standalone build).')
        return 0
    fp = fingerprint()          # taken BEFORE the build
    print('make_exe: sources changed — rebuilding dist/TTML2PGS …')
    r = subprocess.run([sys.executable, '-m', 'PyInstaller',
                        os.path.join(ROOT, 'ttml2pgs.spec'),
                        '--noconfirm'],
                       cwd=ROOT)
    if r.returncode != 0:
        print('make_exe: build FAILED (is the exe still running?) — '
              'the previous build stays in place.', file=sys.stderr)
        return r.returncode
    try:
        with open(STAMP, 'w', encoding='utf-8') as f:
            f.write(fp)
    except OSError:
        pass
    print(f'make_exe: done → {exe_path()}')
    return 0


def main() -> int:
    if getattr(sys, 'frozen', False):
        return 0                         # never self-rebuild when frozen
    force = '--force' in sys.argv
    if '--check-only' in sys.argv:
        return 0 if is_current() else 1
    if not force and is_current():
        return 0
    return build()


if __name__ == '__main__':
    raise SystemExit(main())
