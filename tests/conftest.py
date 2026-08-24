import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(scope="session")
def qt_app():
    """Qt needs an application object before any font or QPainter work."""
    from telemetry_overlay.hud.renderer import ensure_qt_app

    return ensure_qt_app()
