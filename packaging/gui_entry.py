"""PyInstaller entry point for the telemetry-overlay-gui executable.

A separate script (rather than pointing PyInstaller straight at
``telemetry_overlay/gui/app.py``) keeps the Analysis root out of the package
itself, so PyInstaller's module discovery does not confuse this launcher for
part of the ``telemetry_overlay`` package.
"""

from __future__ import annotations

import sys

from telemetry_overlay.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
