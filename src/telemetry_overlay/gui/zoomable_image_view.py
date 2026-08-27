"""A static image view with mouse-wheel zoom and drag-to-pan.

Used for the diagnostic PNGs the CLI's own plotting functions already produce
(``save_diagnostic_plots``, ``save_fit_plot``) -- they render at a fixed size for
the file, which is too small to read comfortably inside the GUI, so this widget
lets the user zoom in without re-rendering anything.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

#: Zoom multiplier per wheel notch, and the range it is clamped to relative to
#: the initial fit-to-view scale.
_ZOOM_STEP = 1.25
_MIN_ZOOM = 0.2
_MAX_ZOOM = 20.0


class ZoomableImageView(QGraphicsView):
    """Shows one image, zoomable with the mouse wheel and pannable by dragging.

    Double-click resets the view back to fit-to-window.
    """

    def __init__(self, placeholder: str, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._placeholder = placeholder
        self._base_scale = 1.0

        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMinimumSize(320, 200)
        # Painted in the window's own base colour rather than a fixed dark grey: the
        # plots shown here are pinned to a light matplotlib style (see autosync.py),
        # so a dark pane framed them in a black box that matched nothing else on the
        # tab. "color" is deliberately not set here: QGraphicsTextItem (the
        # placeholder, added in _show_placeholder) is painted directly into the scene
        # and does not pick up the view's stylesheet -- its colour has to be set on
        # the item itself.
        self.setStyleSheet(
            "background-color: palette(base); border: 1px solid palette(mid);"
        )
        self.setToolTip("Scroll to zoom, drag to pan, double-click to reset")
        self._show_placeholder(placeholder)

    # ---- content -----------------------------------------------------------

    def show_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._show_placeholder(f"could not load {path.name}")
            return
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self._fit_to_view()

    def clear_image(self) -> None:
        self._show_placeholder(self._placeholder)

    def _show_placeholder(self, text: str) -> None:
        self._scene.clear()
        self._pixmap_item = None
        # QGraphicsTextItem ignores the view's stylesheet -- it is painted directly
        # into the scene, not styled by Qt's CSS cascade -- so its colour has to be
        # set on the item. Taken from the palette rather than hardcoded so it stays
        # readable against whichever base colour the pane is painted in.
        item = self._scene.addText(text)
        item.setDefaultTextColor(QColor(self.palette().mid().color()))
        self.resetTransform()
        # Centre on the text explicitly: a placeholder shown right after a real,
        # zoomed/panned image (e.g. switching to a video with nothing cached yet)
        # would otherwise keep the old scroll position, which can leave this small
        # placeholder entirely outside the viewport -- indistinguishable from a
        # blank dark rectangle.
        self.centerOn(item)

    # ---- zoom/pan ------------------------------------------------------

    def _fit_to_view(self) -> None:
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._base_scale = self.transform().m11()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._pixmap_item is None:
            super().wheelEvent(event)
            return
        factor = _ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / _ZOOM_STEP
        current = self.transform().m11()
        relative = (current * factor) / self._base_scale
        if relative < _MIN_ZOOM or relative > _MAX_ZOOM:
            return
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._fit_to_view()
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Keep content fitted until the user has actually zoomed by hand.
        if self._pixmap_item is not None and self.transform().m11() == self._base_scale:
            self._fit_to_view()
