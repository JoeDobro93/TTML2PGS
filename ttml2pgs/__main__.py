"""Entry point: GUI when launched bare, CLI when arguments are given."""

import sys


def run():
    if len(sys.argv) > 1:
        from .cli import main
        sys.exit(main())
    from .ui.app import main as gui_main
    sys.exit(gui_main())


if __name__ == '__main__':
    run()
