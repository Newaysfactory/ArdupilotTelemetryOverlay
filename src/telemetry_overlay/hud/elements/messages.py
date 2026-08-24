"""ArduPilot status messages, stacked bottom-centre.

Visibility windows (including the one-second minimum) are computed once by
:class:`~telemetry_overlay.telemetry.events.MessageTrack`; this element only draws
whatever is visible at the current frame.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QColor, QPainter

from ...telemetry.timeline import FrameState
from ..preset import ElementConfig
from .base import HudElement, RenderContext, draw_text, parse_color
from .registry import register


@register
class MessagesElement(HudElement):
    """Status message log.

    Options:
      ``lines``   rows reserved, must match the preset's ``messages.max_lines``
      ``width``   box width as a fraction of frame width (default 0.7)
      ``align``   ``center`` (default), ``left`` or ``right``
      ``newest``  ``bottom`` (default) or ``top``
    """

    type_name = "messages"
    display_name = "Status messages"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        super().__init__(config, ctx)
        self.lines = max(1, int(self.option("lines", 3)))
        self.width_fraction = float(self.option("width", 0.7))
        self.align = str(self.option("align", "center"))
        if self.align not in ("left", "center", "right"):
            raise ValueError(
                f"element {config.id}: align must be left, center or right"
            )
        self.newest = str(self.option("newest", "bottom"))
        if self.newest not in ("top", "bottom"):
            raise ValueError(f"element {config.id}: newest must be top or bottom")

    @property
    def _size_fraction(self) -> float:
        return self.scaled(self.theme.message_size)

    def _line_height(self) -> float:
        return self.ctx.metrics(self._size_fraction).height() * 1.08

    def size(self) -> QSizeF:
        return QSizeF(
            self.width_fraction * self.ctx.width, self._line_height() * self.lines
        )

    def cache_key(self, state: FrameState) -> tuple:
        return state.messages

    def draw(self, painter: QPainter, state: FrameState) -> None:
        texts = state.messages
        if not texts:
            return
        rect = self.rect()
        metrics = self.ctx.metrics(self._size_fraction)
        font = self.ctx.font(self._size_fraction)
        color = parse_color(self.theme.message_color)
        outline = parse_color(self.theme.outline_color)
        outline_w = self.ctx.px(self._size_fraction) * self.theme.outline_width
        line_h = self._line_height()

        # Keep only what fits, preferring the newest.
        visible = list(texts[-self.lines :])
        if self.newest == "top":
            visible.reverse()
        # Fill from the bottom of the box so a single message sits on the last row.
        first_row = self.lines - len(visible)

        plate = parse_color(self.theme.plate_color)
        for row, text in enumerate(visible, start=first_row):
            width = metrics.horizontalAdvance(text)
            if self.align == "center":
                x = rect.center().x() - width / 2.0
            elif self.align == "right":
                x = rect.right() - width
            else:
                x = rect.left()
            baseline = rect.top() + row * line_h + metrics.ascent()
            if plate.alpha():
                pad = self.ctx.px(self._size_fraction) * 0.3
                painter.setPen(QColor(0, 0, 0, 0))
                painter.setBrush(plate)
                painter.drawRoundedRect(
                    QRectF(
                        x - pad,
                        rect.top() + row * line_h,
                        width + pad * 2,
                        line_h,
                    ),
                    pad * 0.4,
                    pad * 0.4,
                )
            draw_text(
                painter,
                QPointF(x, baseline),
                text,
                font,
                color,
                outline_color=outline,
                outline_width=outline_w,
            )
