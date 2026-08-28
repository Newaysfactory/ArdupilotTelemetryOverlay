"""GUI entry point: ``telemetry-overlay-gui [video] [log] [preset]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from ..cache import cache_root
from ..paths import ensure_writable_dir
from .main_window import MainWindow

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telemetry-overlay-gui",
        description="GUI control panel over the telemetry-overlay CLI commands.",
    )
    parser.add_argument("video", type=Path, nargs="?")
    parser.add_argument("log", type=Path, nargs="?")
    parser.add_argument("preset", type=Path, nargs="?")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="repeat for more logging"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    # Created before any OverlayRenderer: hud/renderer.py's ensure_qt_app() reuses
    # whatever QGuiApplication.instance() already exists, so the widget-capable
    # QApplication must exist first.
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    try:
        ensure_writable_dir(cache_root())
    except RuntimeError as exc:
        QMessageBox.critical(None, "Cannot start", str(exc))
        return 1

    window = MainWindow()

    if args.preset is not None:
        window.controller.set_preset_path(args.preset)
    if args.video is not None:
        window.load_video(args.video)
    if args.log is not None:
        window.load_log(args.log)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
