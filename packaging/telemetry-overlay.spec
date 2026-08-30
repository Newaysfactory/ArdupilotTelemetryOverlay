# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the telemetry-overlay-gui executable.

Builds one onedir bundle (fast startup; unlike onefile it does not re-extract
several hundred MB of PySide6/OpenCV/FFmpeg/matplotlib to a temp directory on
every launch) per OS. The GitHub Actions workflow zips the resulting folder
for distribution. Run locally from the repo root with the project's venv
active:

    pyinstaller packaging/telemetry-overlay.spec --noconfirm

Output lands in ``dist/telemetry-overlay-gui/``.
"""

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 -- injected by PyInstaller
SRC = REPO_ROOT / "src"
ASSETS = SRC / "telemetry_overlay" / "gui" / "assets"
SAMPLE_DATA = REPO_ROOT / "sample_data"

datas = [
    (str(REPO_ROOT / "presets" / "default.json"), "presets"),
    (str(ASSETS), "telemetry_overlay/gui/assets"),
]
binaries = []
hiddenimports = []

# av (bundles its own FFmpeg shared libs), cv2 and matplotlib all carry data
# and/or binary files PyInstaller's static import analysis cannot see on its
# own -- collect_all pulls in what each package's own PyInstaller hook (or,
# for av, its extension module layout) declares.
for pkg in ("av", "cv2", "matplotlib"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

if sys.platform == "win32":
    icon_file = str(ASSETS / "icon.ico")
elif sys.platform == "darwin":
    icon_file = str(ASSETS / "icon.icns")
else:
    icon_file = None

a = Analysis(  # noqa: F821
    [str(REPO_ROOT / "packaging" / "gui_entry.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

# Startup splash. Measured on Windows: the *first* launch after extracting the
# package takes ~17 s before any window appears (the OS has none of the ~390 MB of
# DLLs cached yet, and the antivirus reads them all), against ~1 s once warm. This
# is PyInstaller's bootloader splash, drawn by the C bootloader *before* Python
# starts -- a QSplashScreen could not help here, because it cannot exist until
# PySide6 itself has been imported, which is most of the wait. The app closes it
# through pyi_splash as soon as the real window is up (see gui/app.py).
#
# PyInstaller does not support the splash on macOS; there the Dock's bouncing icon
# is the only startup feedback, and it comes from the OS.
splash = None
if sys.platform != "darwin":
    splash = Splash(  # noqa: F821
        str(ASSETS / "splash.png"),
        binaries=a.binaries,
        datas=a.datas,
        # The artwork already carries the name, version and author, rendered with
        # QPainter; Tk's own text layer would only add a second, worse-looking
        # typeface on top of it.
        text_pos=None,
        always_on_top=True,
    )

exe_args = [pyz, a.scripts]
if splash is not None:
    exe_args.append(splash)
exe_args.append([])

exe = EXE(  # noqa: F821
    *exe_args,
    exclude_binaries=True,
    name="telemetry-overlay-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon_file,
)

coll_args = [exe]
if splash is not None:
    coll_args.append(splash.binaries)
coll_args += [a.binaries, a.datas]

coll = COLLECT(  # noqa: F821
    *coll_args,
    strip=False,
    upx=False,
    name="telemetry-overlay-gui",
)

# Plain filesystem copy, not a `datas`/COLLECT TOC entry: PyInstaller 6's
# onedir layout redirects every TOC-collected file into _internal/
# regardless of the destination path given, but the sample video+log need
# to sit next to the exe (not buried in _internal) so a non-technical user
# can find and try them right away. Runs on every spec invocation, since
# this is plain script code, not a cached PyInstaller build step.
sample_data_dest = Path(coll.name) / "sample_data"
if sample_data_dest.exists():
    shutil.rmtree(sample_data_dest)
shutil.copytree(SAMPLE_DATA, sample_data_dest)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="TelemetryOverlay.app",
        icon=icon_file,
        bundle_identifier="community.ardupilot.telemetry-overlay",
    )
