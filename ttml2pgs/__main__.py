"""Entry point: GUI when launched bare, CLI when arguments are given."""

import multiprocessing
import sys


def run():
    # Required for frozen (PyInstaller) builds: the parallel renderer
    # spawns worker processes, and without this each worker would
    # re-launch the app. No-op when running from source.
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        from .cli import main
        sys.exit(main())
    from .ui.app import main as gui_main
    sys.exit(gui_main())


if __name__ == '__main__':
    run()
