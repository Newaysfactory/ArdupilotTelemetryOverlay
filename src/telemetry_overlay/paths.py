"""Where the program's own bundled resources and writable data directory live,
frozen (PyInstaller) or not.

PyInstaller sets ``sys.frozen`` and ``sys._MEIPASS`` (the extraction directory,
temporary for a onefile build, the app folder for a onedir build) only when
running as a packaged executable; neither exists in a source checkout. Every
place that needs a bundled resource (the default preset) or a writable
app-adjacent directory (the cache) goes through here, so the frozen/dev branch
is decided once instead of being re-derived with a different ``parents[N]`` in
each caller.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Root of bundled read-only resources: the PyInstaller extraction dir when
    frozen, the repo root in dev mode (where ``presets/`` lives)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def executable_dir() -> Path:
    """Writable directory for app-adjacent data: next to the ``.exe`` when
    frozen, the repo root in dev mode (matching the previous, pre-packaging
    behaviour of always using the repo root)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def ensure_writable_dir(path: Path) -> None:
    """Create *path* if needed and verify it is actually writable.

    Raises ``RuntimeError`` with a message a non-technical user can act on --
    this is surfaced as-is by the CLI's top-level error handler and by a
    message box in the GUI. A packaged app can land anywhere, including
    locked-down folders like ``Program Files``, where ``mkdir`` can succeed
    (inherited from a writable parent) while actually writing a file still
    fails, so both are checked.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Cannot write to '{path}': {exc}. Move the app to a folder you "
            "have write access to (e.g. your Desktop or Documents folder) and "
            "try again -- Program Files and other system folders are usually "
            "locked down."
        ) from exc
