"""QPainter-based overlay renderer.

One renderer serves both the interactive preview and the export, which is the only way
to guarantee that what the layout editor shows is what the encoder writes.

The export never composites a full 4K RGBA frame. Element boxes have fixed sizes, so
the regions the overlay can touch are known before the first frame: they are merged
into a handful of *bands* and only those are drawn and handed to the compositor.
On a typical layout that is well under a fifth of the frame area.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication, QImage, QPainter

from ..telemetry.timeline import FrameState
from .elements import build_element
from .elements.base import HudElement, RenderContext
from .preset import Preset

log = logging.getLogger(__name__)

#: Kept alive for the process: destroying it would invalidate every cached font.
_app: QGuiApplication | None = None


def ensure_qt_app(headless: bool = True) -> QGuiApplication:
    """Create the QGuiApplication that font loading and QPainter require.

    Note that ``headless`` does *not* select Qt's "offscreen" platform plugin. On
    Windows that plugin ships with an empty font database, so every glyph renders as a
    .notdef box -- the overlay would come out as rows of filled rectangles. Rendering
    into a QImage needs no window anyway, so the native platform is used and only an
    explicit ``QT_QPA_PLATFORM`` in the environment overrides it.
    """
    global _app
    existing = QGuiApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    _app = QGuiApplication([])
    if not QFontDatabase.families():
        log.warning(
            "Qt reports no available fonts (platform plugin %r): text will render as "
            "empty boxes. Unset QT_QPA_PLATFORM, or point the theme's font_file at a "
            "font to load.",
            _app.platformName(),
        )
    return _app


@dataclass
class Band:
    """A rectangular region of the frame that the overlay may paint."""

    rect: QRect
    elements: list[HudElement] = field(default_factory=list)
    _key: tuple | None = field(default=None, repr=False)
    _image: QImage | None = field(default=None, repr=False)

    @property
    def area(self) -> int:
        return self.rect.width() * self.rect.height()


def _align_even(rect: QRect) -> QRect:
    """Snap a rect to even coordinates.

    Chroma-subsampled compositing wants even offsets; rounding out (never in) also
    guarantees no painted pixel falls outside its band.
    """
    left = rect.left() - (rect.left() % 2)
    top = rect.top() - (rect.top() % 2)
    right = rect.right() + 1
    bottom = rect.bottom() + 1
    right += right % 2
    bottom += bottom % 2
    return QRect(left, top, right - left, bottom - top)


def compute_bands(
    elements: list[HudElement],
    width: int,
    height: int,
    *,
    max_bands: int = 6,
    merge_gap: int = 8,
) -> list[Band]:
    """Group element bounding boxes into at most ``max_bands`` disjoint regions."""
    frame = QRect(0, 0, width, height)
    boxes: list[tuple[QRect, list[HudElement]]] = []
    for element in elements:
        rect = _align_even(element.bounds().toAlignedRect()).intersected(frame)
        if rect.isEmpty():
            continue
        boxes.append((rect, [element]))

    # Merge anything overlapping or nearly touching.
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i][0], boxes[j][0]
                if a.adjusted(-merge_gap, -merge_gap, merge_gap, merge_gap).intersects(
                    b
                ):
                    boxes[i] = (a.united(b), boxes[i][1] + boxes[j][1])
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break

    # Too many regions costs one compositor input each; merge the cheapest pairs.
    while len(boxes) > max_bands:
        best: tuple[int, int, int] | None = None
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i][0], boxes[j][0]
                waste = (
                    a.united(b).width() * a.united(b).height()
                    - a.width() * a.height()
                    - b.width() * b.height()
                )
                if best is None or waste < best[0]:
                    best = (waste, i, j)
        assert best is not None
        _, i, j = best
        boxes[i] = (boxes[i][0].united(boxes[j][0]), boxes[i][1] + boxes[j][1])
        del boxes[j]

    boxes.sort(key=lambda item: (item[0].top(), item[0].left()))
    return [
        Band(rect=_align_even(rect).intersected(frame), elements=els)
        for rect, els in boxes
    ]


def qimage_to_ndarray(image: QImage) -> np.ndarray:
    """Zero-copy-ish view of an ``RGBA8888`` image as ``(h, w, 4)`` uint8.

    Qt pads scanlines, so a padded image is compacted into a contiguous copy, which is
    what the compositor needs anyway.
    """
    if image.format() != QImage.Format.Format_RGBA8888:
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    height, width = image.height(), image.width()
    stride = image.bytesPerLine()
    buffer = np.frombuffer(memoryview(image.constBits()), dtype=np.uint8)
    if stride == width * 4:
        return buffer.reshape(height, width, 4)
    return np.ascontiguousarray(
        buffer.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
    )


class OverlayRenderer:
    """Draws HUD overlay images for a given preset and frame size."""

    def __init__(
        self,
        preset: Preset,
        width: int,
        height: int,
        *,
        max_bands: int = 6,
        headless: bool = True,
    ) -> None:
        ensure_qt_app(headless=headless)
        self.preset = preset
        self.width = int(width)
        self.height = int(height)
        self.context = RenderContext(self.width, self.height, preset.theme)
        self.elements = [
            build_element(config, self.context)
            for config in preset.visible_elements()
        ]
        self.bands = compute_bands(
            self.elements, self.width, self.height, max_bands=max_bands
        )
        self._full: QImage | None = None
        log.debug(
            "renderer %dx%d: %d elements, %d bands, %.1f%% of frame area",
            self.width,
            self.height,
            len(self.elements),
            len(self.bands),
            self.coverage * 100,
        )

    @property
    def coverage(self) -> float:
        """Fraction of the frame the bands cover: the compositing cost."""
        total = self.width * self.height
        return sum(band.area for band in self.bands) / total if total else 0.0

    # ---- rendering ---------------------------------------------------------

    def _paint(
        self, image: QImage, elements: list[HudElement], state: FrameState, offset: QRect
    ) -> None:
        painter = QPainter(image)
        try:
            painter.setRenderHints(
                QPainter.RenderHint.Antialiasing
                | QPainter.RenderHint.TextAntialiasing
                | QPainter.RenderHint.SmoothPixmapTransform
            )
            painter.translate(-offset.left(), -offset.top())
            for element in elements:
                element.draw(painter, state)
        finally:
            painter.end()

    def render_band(self, band: Band, state: FrameState) -> QImage:
        """Overlay image for one band, reusing the previous one when unchanged."""
        key = tuple(element.cache_key(state) for element in band.elements)
        if band._image is not None and band._key == key:
            return band._image
        image = QImage(
            band.rect.width(), band.rect.height(), QImage.Format.Format_RGBA8888
        )
        image.fill(Qt.GlobalColor.transparent)
        self._paint(image, band.elements, state, band.rect)
        band._image = image
        band._key = key
        return image

    def render_frame(self, state: FrameState) -> QImage:
        """Full-frame transparent overlay, for the preview and single-frame export."""
        image = QImage(self.width, self.height, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        self._paint(image, self.elements, state, QRect(0, 0, self.width, self.height))
        self._full = image
        return image
