"""Render the app icon (the "Horizon" design) and write the icon assets.

One-off/dev build tool, not part of the installed package. Draws the artwork
procedurally with QPainter at high resolution (rather than shipping/parsing an
SVG at runtime) and downsamples with Pillow for the smaller raster sizes, then
assembles the multi-resolution .ico/.icns bundles Qt and OS packaging pick up.

Run with the project's venv (needs PySide6 + Pillow, both already project
dependencies/available): `python scripts/generate_icon.py`.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen

ASSETS = Path(__file__).resolve().parent.parent / "src/telemetry_overlay/gui/assets"

# Colours match the approved "Horizon" preview tile exactly.
TILE_BG = QColor("#0d1512")
RING = QColor("#2a3a32")
SKY = QColor("#123a52")
GROUND = QColor("#1f2e1a")
GREEN = QColor("#39ff8a")

MASTER_SIZE = 1024
# Content (the 64x64-viewBox artwork) occupies the same fraction of the tile
# as in the approved preview (62px icon inside a 96px tile).
CONTENT_FRACTION = 62 / 96
TILT_DEG = -8.0


def _draw_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded-square tile filling the whole canvas (radius matches the 22%
    # corner radius used for the preview tiles).
    tile_radius = size * 0.22
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(TILE_BG)
    painter.drawRoundedRect(QRectF(0, 0, size, size), tile_radius, tile_radius)

    content = size * CONTENT_FRACTION
    scale = content / 64.0
    offset = (size - content) / 2.0
    painter.translate(offset, offset)
    painter.scale(scale, scale)

    center = QPointF(32, 32)

    # Faint outer ring.
    pen = QPen(RING, 1.5)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, 27, 27)

    # Sky/ground disc, clipped to the inner circle and tilted like a real
    # artificial horizon.
    painter.save()
    clip_path = QPainterPath()
    clip_path.addEllipse(center, 24, 24)
    painter.setClipPath(clip_path)
    painter.translate(center)
    painter.rotate(TILT_DEG)
    painter.translate(-center)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(SKY)
    painter.drawRect(QRectF(0, 0, 64, 30))
    painter.setBrush(GROUND)
    painter.drawRect(QRectF(0, 30, 64, 34))

    painter.setPen(QPen(GREEN, 1.6))
    painter.drawLine(QPointF(-4, 30), QPointF(68, 30))

    ladder_pen = QPen(GREEN, 1.3)
    painter.setPen(ladder_pen)
    painter.setOpacity(0.85)
    painter.drawLine(QPointF(22, 20), QPointF(42, 20))
    painter.drawLine(QPointF(26, 40), QPointF(38, 40))
    painter.setOpacity(1.0)
    painter.restore()

    # Fixed aircraft reference: wings + center dot, not tilted.
    wing_pen = QPen(GREEN, 2.4)
    wing_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(wing_pen)
    painter.drawLine(QPointF(12, 32), QPointF(24, 32))
    painter.drawLine(QPointF(40, 32), QPointF(52, 32))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GREEN)
    painter.drawEllipse(center, 2.2, 2.2)

    painter.end()
    return image


def _qimage_to_pil(image: QImage) -> Image.Image:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = image.constBits()
    data = bytes(buf)[: image.height() * image.bytesPerLine()]
    return Image.frombuffer(
        "RGBA", (image.width(), image.height()), data, "raw", "RGBA", image.bytesPerLine(), 1
    ).copy()


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    QGuiApplication([])

    master = _qimage_to_pil(_draw_icon(MASTER_SIZE))
    master.save(ASSETS / "icon_1024.png")

    png_sizes = [16, 24, 32, 48, 64, 128, 256, 512]
    resized = {
        size: master.resize((size, size), Image.LANCZOS) for size in png_sizes
    }
    for size, img in resized.items():
        img.save(ASSETS / f"icon_{size}.png")

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    resized[256].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
    )

    try:
        resized[512].save(ASSETS / "icon.icns", format="ICNS")
    except (ValueError, OSError) as exc:  # pragma: no cover - platform dependent
        print(f"skipped .icns (Pillow ICNS support unavailable here): {exc}")

    print(f"wrote assets to {ASSETS}")


if __name__ == "__main__":
    main()
