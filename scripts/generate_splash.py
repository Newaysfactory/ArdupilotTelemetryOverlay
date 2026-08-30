"""Render the startup splash shown while the packaged app loads.

Why this exists at all: measured on Windows, the *first* launch of the PyInstaller
bundle takes ~17 s before any window appears (390 MB of DLLs the OS has not cached
yet, plus the antivirus reading them), against ~1 s once warm. The splash does not
make that faster -- it makes it legible, and it is the first thing a new user sees.

Why it is a baked PNG rather than PyInstaller's dynamic ``text_pos`` overlay: that
draws with Tk, one line, no control over weight or tracking. Everything here is
QPainter, so the typography and the artwork are ours -- and the artwork is literally
the icon's, imported rather than redrawn (see ``generate_icon.draw_horizon_tile``).

The version is therefore baked in at generation time, which can drift from
``__version__``. It is written into the PNG's text metadata as well as onto the
pixels, and ``.github/workflows/build.yml`` reads that chunk and refuses to build a
release whose splash advertises a different version -- the same check-don't-derive
rule the release tag gets.

Run with the project's venv: `python scripts/generate_splash.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, PngImagePlugin
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from generate_icon import ASSETS, GREEN, TILT_DEG, draw_horizon_tile  # noqa: E402

from telemetry_overlay import AUTHOR, __version__  # noqa: E402

#: PNG text key the release workflow reads back to catch a stale splash.
VERSION_METADATA_KEY = "telemetry-overlay-version"

WIDTH, HEIGHT = 720, 420

# Lifted a little off the badge's own #0d1512 so the rounded tile reads as a
# recessed chip on the panel instead of dissolving into it.
BG_TOP = QColor("#17241f")
BG_BOTTOM = QColor("#0a1119")
TITLE = QColor("#eaf4ee")
EYEBROW = QColor("#5f7a6d")
SUBTITLE = QColor("#7e938a")
HINT = QColor("#4e6259")
BORDER = QColor(255, 255, 255, 16)

BADGE_SIZE = 156
BADGE_X = 60
BADGE_Y = (HEIGHT - BADGE_SIZE) / 2 - 4
#: The instrument's own horizon sits at the middle of the badge; the panel-wide
#: line is drawn through exactly this height so it reads as the same horizon
#: continuing out of the instrument.
HORIZON_Y = BADGE_Y + BADGE_SIZE / 2
TEXT_X = BADGE_X + BADGE_SIZE + 44

#: Kept clear of the panel-wide horizon line, so it passes behind the wordmark
#: instead of striking through it. It runs to the right edge on purpose: letting
#: the line re-emerge past the text put a stray dash in the top-right corner, far
#: above the badge it is supposed to continue from, reading as an artefact.
TEXT_BLOCK = QRectF(TEXT_X - 28, 130, WIDTH - (TEXT_X - 28), 164)

#: Tried in order; the first one installed wins. Rendering happens on the machine
#: that runs this script, so the file is committed rather than regenerated per
#: platform at build time -- a substituted font would quietly change the design.
FONT_STACK = ["Segoe UI", "Inter", "Helvetica Neue", "DejaVu Sans", "Arial"]


def _font(size: float, weight: QFont.Weight, *, tracking: float = 0.0) -> QFont:
    font = QFont()
    font.setFamilies(FONT_STACK)
    font.setPixelSize(int(size))
    font.setWeight(weight)
    if tracking:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, tracking)
    return font


def _draw_backdrop(painter: QPainter) -> None:
    gradient = QLinearGradient(0, 0, WIDTH * 0.35, HEIGHT)
    gradient.setColorAt(0.0, BG_TOP)
    gradient.setColorAt(1.0, BG_BOTTOM)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRect(QRectF(0, 0, WIDTH, HEIGHT))

    # A soft glow under the badge: without it the tile is a dark square on a dark
    # panel, and the whole left half goes flat.
    glow = QRadialGradient(
        QPointF(BADGE_X + BADGE_SIZE / 2, HORIZON_Y), BADGE_SIZE * 1.5
    )
    glow.setColorAt(0.0, QColor(57, 255, 138, 26))
    glow.setColorAt(1.0, QColor(57, 255, 138, 0))
    painter.setBrush(glow)
    painter.drawRect(QRectF(0, 0, WIDTH, HEIGHT))


def _draw_horizon_motif(painter: QPainter) -> None:
    """The badge's horizon, continued across the panel at the same tilt.

    Clipped around the wordmark rather than drawn over it: the line has to read as
    passing *behind* the text, and at this opacity a strike-through would just look
    like a rendering artefact.
    """
    outside_text = QPainterPath()
    outside_text.addRect(QRectF(0, 0, WIDTH, HEIGHT))
    hole = QPainterPath()
    hole.addRect(TEXT_BLOCK)

    painter.save()
    painter.setClipPath(outside_text.subtracted(hole))
    painter.translate(BADGE_X + BADGE_SIZE / 2, HORIZON_Y)
    painter.rotate(TILT_DEG)
    painter.setOpacity(0.12)
    painter.setPen(QPen(GREEN, 1.6))
    painter.drawLine(QPointF(-WIDTH, 0), QPointF(WIDTH, 0))
    painter.restore()


def _draw_text(painter: QPainter) -> None:
    painter.setPen(EYEBROW)
    painter.setFont(_font(14, QFont.Weight.DemiBold, tracking=4.4))
    painter.drawText(QPointF(TEXT_X, 168), "ARDUPILOT")

    painter.setPen(TITLE)
    painter.setFont(_font(38, QFont.Weight.DemiBold))
    painter.drawText(QPointF(TEXT_X, 214), "Telemetry Overlay")

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GREEN)
    painter.drawRect(QRectF(TEXT_X, 234, 58, 3))

    painter.setPen(SUBTITLE)
    painter.setFont(_font(15, QFont.Weight.Normal))
    painter.drawText(QPointF(TEXT_X, 268), f"v{__version__}   ·   {AUTHOR}")

    # Full-width hairline rather than a partial bar: a static bar that stops
    # part-way reads as a progress meter that has stalled, which is the opposite
    # of what a 17-second wait needs to say.
    hairline = QLinearGradient(0, 0, WIDTH * 0.75, 0)
    hairline.setColorAt(0.0, QColor(57, 255, 138, 90))
    hairline.setColorAt(1.0, QColor(57, 255, 138, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(hairline)
    painter.drawRect(QRectF(0, HEIGHT - 2, WIDTH, 2))

    painter.setPen(HINT)
    painter.setFont(_font(13, QFont.Weight.Normal))
    painter.drawText(
        QRectF(0, HEIGHT - 46, WIDTH - BADGE_X, 20),
        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
        "Starting…",
    )


def render() -> QImage:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    _draw_backdrop(painter)
    _draw_horizon_motif(painter)
    draw_horizon_tile(painter, BADGE_SIZE, x=BADGE_X, y=BADGE_Y)
    _draw_text(painter)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(BORDER, 1.0))
    painter.drawRect(QRectF(0.5, 0.5, WIDTH - 1, HEIGHT - 1))

    painter.end()
    return image


def _qimage_to_pil(image: QImage) -> Image.Image:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = bytes(image.constBits())[: image.height() * image.bytesPerLine()]
    return Image.frombuffer(
        "RGBA",
        (image.width(), image.height()),
        data,
        "raw",
        "RGBA",
        image.bytesPerLine(),
        1,
    ).copy()


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    QGuiApplication([])

    # Flattened onto black: the splash is an opaque rectangle, and a stray alpha
    # edge would show up as a halo under Tk's transparency handling.
    pil = _qimage_to_pil(render()).convert("RGB")

    info = PngImagePlugin.PngInfo()
    info.add_text(VERSION_METADATA_KEY, __version__)
    out = ASSETS / "splash.png"
    pil.save(out, format="PNG", pnginfo=info)
    print(f"wrote {out} ({WIDTH}x{HEIGHT}, v{__version__})")


if __name__ == "__main__":
    main()
