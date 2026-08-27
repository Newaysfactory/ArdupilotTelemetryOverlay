"""A horizontal timeline with a scrub position and a draggable From/To range.

PySide6 has no built-in multi-handle slider, so this is a small custom ``QWidget``:
it paints a groove, a highlighted band between the From/To handles, and three
handles (From, To, and the scrub position on top), and turns mouse drags into
value changes. Used on the preview tab so the From/To range shared across every
tab (:attr:`ProjectController.range_start`/``range_end``) can be set by dragging
directly on the same timeline the scrub position lives on, not only through the
numeric fields duplicated on the autosync/manualsync/export tabs.

Values are plain floats in video-time seconds, not slider ticks: the widget maps
pixel positions to/against ``[minimum, maximum]`` continuously, since there is no
fixed step size shared with the numeric fields' own precision.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

#: Radius of a handle's drawn circle/hit target, in pixels.
_HANDLE_RADIUS = 6.0
_GROOVE_HEIGHT = 4.0
#: How close (pixels) a press must land to a handle to grab it, rather than
#: moving the scrub position straight to the click.
_HIT_MARGIN = 10.0


def value_to_fraction(value: float, minimum: float, maximum: float) -> float:
    """Where ``value`` sits between ``minimum`` and ``maximum``, clamped to [0, 1]."""
    span = maximum - minimum
    if span <= 0:
        return 0.0
    return min(max((value - minimum) / span, 0.0), 1.0)


def fraction_to_value(fraction: float, minimum: float, maximum: float) -> float:
    fraction = min(max(fraction, 0.0), 1.0)
    return minimum + fraction * (maximum - minimum)


class RangeSlider(QWidget):
    """One timeline, three handles: From, To, and the current scrub position."""

    #: Emitted continuously while the scrub handle is dragged, or right after a
    #: click sets it directly (video time, seconds).
    positionChanged = Signal(float)
    #: Emitted once the scrub drag ends -- mirrors QSlider.sliderReleased, the
    #: signal preview_tab.py uses to trigger the (debounced-until-now) decode.
    positionCommitted = Signal(float)
    #: Emitted continuously while a From/To handle is dragged.
    rangeChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minimum = 0.0
        self._maximum = 1.0
        self._position = 0.0
        self._low = 0.0
        self._high = 1.0
        self._dragging: str | None = None  # "position" | "low" | "high"
        self.setMinimumHeight(28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    # ---- value access ----------------------------------------------------

    def setRange(self, minimum: float, maximum: float) -> None:
        self._minimum = minimum
        self._maximum = max(maximum, minimum + 1e-6)
        self._position = min(max(self._position, self._minimum), self._maximum)
        self._low = min(max(self._low, self._minimum), self._maximum)
        self._high = min(max(self._high, self._minimum), self._maximum)
        self.update()

    def setPosition(self, value: float) -> None:
        self._position = min(max(value, self._minimum), self._maximum)
        self.update()

    def position(self) -> float:
        return self._position

    def setLow(self, value: float) -> None:
        """Move the From handle alone, clamped against the *current* To handle.

        Meant for interactive dragging, where the other handle is not moving in
        the same gesture. Setting both handles from an external source (e.g. the
        shared controller state) must go through :meth:`setLowHigh` instead --
        calling this and :meth:`setHigh` back to back is order-dependent: if the
        new low exceeds the *old* high, this clamps low down to it before the
        high update ever runs.
        """
        self._low = min(max(value, self._minimum), self._high)
        self.update()

    def setHigh(self, value: float) -> None:
        """Move the To handle alone. See :meth:`setLow` for why this is unsafe
        to pair with ``setLow`` for a simultaneous external update."""
        self._high = max(min(value, self._maximum), self._low)
        self.update()

    def setLowHigh(self, low: float, high: float) -> None:
        """Set both handles atomically, order-independent -- for syncing both
        values at once from an external source (see :meth:`setLow`)."""
        low = min(max(low, self._minimum), self._maximum)
        high = min(max(high, self._minimum), self._maximum)
        if low > high:
            low = high = (low + high) / 2
        self._low = low
        self._high = high
        self.update()

    def low(self) -> float:
        return self._low

    def high(self) -> float:
        return self._high

    # ---- painting ----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        rect = self.rect()
        mid_y = float(rect.center().y())
        left, right = self._track_bounds()

        groove = QRectF(left, mid_y - _GROOVE_HEIGHT / 2, right - left, _GROOVE_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().mid())
        painter.drawRoundedRect(groove, 2, 2)

        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        selected = QRectF(low_x, mid_y - _GROOVE_HEIGHT / 2, high_x - low_x, _GROOVE_HEIGHT)
        painter.setBrush(self.palette().highlight())
        painter.drawRoundedRect(selected, 2, 2)

        painter.setBrush(self.palette().highlight())
        painter.drawEllipse(QPointF(low_x, mid_y), _HANDLE_RADIUS, _HANDLE_RADIUS)
        painter.drawEllipse(QPointF(high_x, mid_y), _HANDLE_RADIUS, _HANDLE_RADIUS)

        # The scrub position is drawn last, as a needle through the groove plus a
        # small head, so it stays visible even where it lands on a range handle.
        pos_x = self._value_to_x(self._position)
        text_color = QColor(self.palette().text().color())
        painter.setPen(text_color)
        painter.drawLine(QPointF(pos_x, rect.top() + 2), QPointF(pos_x, rect.bottom() - 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(text_color)
        painter.drawEllipse(QPointF(pos_x, mid_y), _HANDLE_RADIUS * 0.6, _HANDLE_RADIUS * 0.6)

    # ---- geometry <-> value -------------------------------------------------

    def _track_bounds(self) -> tuple[float, float]:
        return _HANDLE_RADIUS, max(_HANDLE_RADIUS, self.rect().width() - _HANDLE_RADIUS)

    def _value_to_x(self, value: float) -> float:
        left, right = self._track_bounds()
        return left + value_to_fraction(value, self._minimum, self._maximum) * (right - left)

    def _x_to_value(self, x: float) -> float:
        left, right = self._track_bounds()
        span = right - left
        fraction = (x - left) / span if span else 0.0
        return fraction_to_value(fraction, self._minimum, self._maximum)

    # ---- mouse interaction ---------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        handles = {
            "position": self._value_to_x(self._position),
            "low": self._value_to_x(self._low),
            "high": self._value_to_x(self._high),
        }
        nearest = min(handles, key=lambda name: abs(handles[name] - x))
        if abs(handles[nearest] - x) <= _HIT_MARGIN:
            self._dragging = nearest
        else:
            # A click on empty groove moves the scrub position there directly,
            # matching a plain QSlider's click-to-jump behaviour.
            self._dragging = "position"
            self._position = self._x_to_value(x)
            self.positionChanged.emit(self._position)
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._dragging is None:
            return
        value = self._x_to_value(event.position().x())
        if self._dragging == "position":
            self._position = value
            self.positionChanged.emit(self._position)
        elif self._dragging == "low":
            self._low = min(value, self._high)
            self.rangeChanged.emit(self._low, self._high)
        elif self._dragging == "high":
            self._high = max(value, self._low)
            self.rangeChanged.emit(self._low, self._high)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._dragging == "position":
            self.positionCommitted.emit(self._position)
        self._dragging = None
