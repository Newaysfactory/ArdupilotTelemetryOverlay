"""Artificial horizon with pitch ladder and roll indication."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QRadialGradient

from ...telemetry import fields
from ...telemetry.timeline import FrameState
from ..preset import ElementConfig
from .base import HudElement, RenderContext, draw_text, parse_color
from .registry import register


@register
class HorizonElement(HudElement):
    """Attitude indicator: the horizon and ladder move, the aircraft symbol is fixed.

    Options:
      ``size``            width as a fraction of frame width (default 0.34)
      ``aspect``          height/width of the element box (default 0.5)
      ``degrees_visible`` pitch degrees spanned by the box height (default 40)
      ``ladder_step``     degrees between ladder rungs (default 10)
      ``ladder_range``    highest ladder rung drawn, in degrees (default 40)
      ``rung_width``      rung half-length as a fraction of the box width
      ``pitch_offset``    camera mount trim in degrees; negative lifts the drawn
                          horizon, to line it up with the real one when the camera
                          points below the flight path
      ``roll_offset``     camera roll trim in degrees
      ``show_ladder_labels``, ``show_reference``, ``show_roll_arc``  (bool)
    """

    type_name = "horizon"
    display_name = "Artificial horizon"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        super().__init__(config, ctx)
        self.size_fraction = float(self.option("size", 0.34)) * config.scale
        self.aspect = float(self.option("aspect", 0.5))
        self.degrees_visible = float(self.option("degrees_visible", 40.0))
        self.ladder_step = float(self.option("ladder_step", 10.0))
        self.ladder_range = float(self.option("ladder_range", 40.0))
        self.rung_width = float(self.option("rung_width", 0.16))
        self.pitch_offset = float(self.option("pitch_offset", 0.0))
        self.roll_offset = float(self.option("roll_offset", 0.0))
        self.show_ladder_labels = bool(self.option("show_ladder_labels", True))
        self.show_reference = bool(self.option("show_reference", True))
        self.show_roll_arc = bool(self.option("show_roll_arc", True))
        if self.degrees_visible <= 0 or self.ladder_step <= 0:
            raise ValueError(
                f"element {config.id}: degrees_visible and ladder_step must be > 0"
            )

    # ---- geometry ----------------------------------------------------------

    def size(self) -> QSizeF:
        width = self.size_fraction * self.ctx.width
        return QSizeF(width, width * self.aspect)

    def _pixels_per_degree(self, rect: QRectF) -> float:
        return rect.height() / self.degrees_visible

    def _line_width(self) -> float:
        return max(1.0, self.ctx.px(self.theme.horizon_line_width) * self.config.scale)

    def bounds(self) -> QRectF:
        """Widen the compositing region to include the roll arc.

        Its ticks sit at ``rect.height() * 0.52`` from the centre -- already past the
        box's own top edge -- plus tick length and outline bleed. Left to the base
        class's box-sized bounds, that part is drawn but then cropped out of the band
        handed to the exporter, so it never reaches the final frame.
        """
        base = super().bounds()
        if not self.show_roll_arc:
            return base
        rect = self.rect()
        radius = rect.height() * 0.52 * 1.15
        center = rect.center()
        arc_box = QRectF(
            center.x() - radius, center.y() - radius, radius * 2.0, radius * 2.0
        )
        frame = QRectF(0, 0, self.ctx.width, self.ctx.height)
        return base.united(arc_box).intersected(frame)

    # ---- drawing ----------------------------------------------------------

    def cache_key(self, state: FrameState) -> tuple:
        roll = state.value(fields.ROLL)
        pitch = state.value(fields.PITCH)
        if roll is None or pitch is None:
            return ("no-att",)
        # Quantised so a still attitude does not force a repaint every frame.
        return (round(roll, 1), round(pitch, 1))

    def draw(self, painter: QPainter, state: FrameState) -> None:
        rect = self.rect()
        roll = state.value(fields.ROLL)
        pitch = state.value(fields.PITCH)
        outline = parse_color(self.theme.outline_color)

        if roll is None or pitch is None:
            self._draw_reference(painter, rect, failed=True)
            draw_text(
                painter,
                QPointF(rect.left(), rect.center().y() - self.ctx.px(0.02)),
                "NO ATT",
                self.ctx.font(self.scaled(self.theme.label_size)),
                parse_color(self.theme.no_data_color),
                outline_color=outline,
                outline_width=self.ctx.px(self.theme.label_size)
                * self.theme.outline_width,
            )
            return

        roll += self.roll_offset
        pitch += self.pitch_offset

        self._draw_ladder_window(painter, rect, roll, pitch, outline)

        if self.show_roll_arc:
            self._draw_roll_arc(painter, rect, roll)
        if self.show_reference:
            self._draw_reference(painter, rect, failed=False)

    def _draw_ladder_window(
        self,
        painter: QPainter,
        rect: QRectF,
        roll: float,
        pitch: float,
        outline: QColor,
    ) -> None:
        """Sky/ground/ladder, masked to an ellipse that fades at its own edge.

        A rotated ladder clipped to the element's plain rectangle leaves a visible
        straight edge wherever a rung exits the box -- at a steep bank the box itself
        becomes visible, which reads as the HUD being "cut off" rather than as a
        deliberate instrument window. Rendering into an offscreen image and multiplying
        it by a radial (elliptical) alpha gradient removes that hard edge: content
        fades out before it would otherwise be sliced by a straight boundary.
        """
        size = QSize(math.ceil(rect.width()), math.ceil(rect.height()))
        if size.width() <= 0 or size.height() <= 0:
            return
        local_rect = QRectF(0.0, 0.0, size.width(), size.height())

        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        local = QPainter(image)
        try:
            local.setRenderHints(painter.renderHints())
            local.translate(local_rect.center())
            local.rotate(-roll)
            ppd = self._pixels_per_degree(local_rect)
            # Nose up moves the horizon down the screen.
            local.translate(0.0, pitch * ppd)
            self._draw_sky_ground(local, local_rect)
            self._draw_horizon_line(local, local_rect, outline)
            self._draw_ladder(local, local_rect, ppd, outline)
        finally:
            local.end()

        self._apply_edge_fade(image, local_rect)
        painter.drawImage(rect.topLeft(), image)

    def _apply_edge_fade(self, image: QImage, local_rect: QRectF) -> None:
        """Multiply ``image``'s alpha by a radial gradient inscribed in its box.

        Both the elliptical cutoff and the soft fade come from this one gradient:
        solid out to ``fade_start`` of the radius, transparent from the radius
        outward (``QRadialGradient`` pads the last stop past its radius).
        ``mask_radius`` scales the circle itself relative to the box (1.0 touches
        the box's mid-edges exactly; >1.0 lets the circle reach further, e.g. closer
        to the corners).
        """
        mask_radius = 1.15
        fade_start = 0.85
        painter = QPainter(image)
        try:
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn
            )
            painter.translate(local_rect.center())
            painter.scale(
                local_rect.width() * 0.5 * mask_radius,
                local_rect.height() * 0.5 * mask_radius,
            )
            gradient = QRadialGradient(QPointF(0.0, 0.0), 1.0)
            gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
            gradient.setColorAt(fade_start, QColor(255, 255, 255, 255))
            gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRect(QRectF(-1.0, -1.0, 2.0, 2.0))
        finally:
            painter.end()

    def _draw_sky_ground(self, painter: QPainter, rect: QRectF) -> None:
        sky = parse_color(self.theme.horizon_sky_color)
        ground = parse_color(self.theme.horizon_ground_color)
        if not sky.alpha() and not ground.alpha():
            return
        # Oversized so the fills still cover the box after rotation and pitch offset.
        span = max(rect.width(), rect.height()) * 4.0
        painter.setPen(QColor(0, 0, 0, 0))
        if sky.alpha():
            painter.setBrush(sky)
            painter.drawRect(QRectF(-span, -span, span * 2, span))
        if ground.alpha():
            painter.setBrush(ground)
            painter.drawRect(QRectF(-span, 0, span * 2, span))

    def _pen(self, color: QColor, width: float) -> QPen:
        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _draw_horizon_line(
        self, painter: QPainter, rect: QRectF, outline: QColor
    ) -> None:
        half = rect.width() * 0.5
        lw = self._line_width()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if outline.alpha():
            painter.setPen(self._pen(outline, lw * 2.6))
            painter.drawLine(QPointF(-half, 0), QPointF(half, 0))
        painter.setPen(self._pen(parse_color(self.theme.horizon_color), lw))
        painter.drawLine(QPointF(-half, 0), QPointF(half, 0))

    def _draw_ladder(
        self, painter: QPainter, rect: QRectF, ppd: float, outline: QColor
    ) -> None:
        color = parse_color(self.theme.horizon_ladder_color)
        lw = self._line_width() * 0.8
        half = rect.width() * self.rung_width
        font = self.ctx.font(self.scaled(self.theme.label_size) * 0.8)
        metrics = self.ctx.metrics(self.scaled(self.theme.label_size) * 0.8)

        step = self.ladder_step
        count = int(self.ladder_range // step)
        for i in range(1, count + 1):
            degrees = i * step
            for sign in (1, -1):
                y = -sign * degrees * ppd
                # Rungs above the horizon are solid, below dashed: the standard cue
                # for which way is up when the horizon itself is off-screen.
                pen = self._pen(color, lw)
                if sign < 0:
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setDashPattern([4.0, 3.0])
                if outline.alpha():
                    outline_pen = self._pen(outline, lw * 2.4)
                    outline_pen.setStyle(pen.style())
                    if pen.style() != Qt.PenStyle.SolidLine:
                        outline_pen.setDashPattern([4.0, 3.0])
                    painter.setPen(outline_pen)
                    painter.drawLine(QPointF(-half, y), QPointF(half, y))
                painter.setPen(pen)
                painter.drawLine(QPointF(-half, y), QPointF(half, y))

                if self.show_ladder_labels:
                    text = f"{int(degrees)}"
                    width = metrics.horizontalAdvance(text)
                    baseline = y + metrics.ascent() * 0.4
                    gap = half + metrics.horizontalAdvance("0") * 0.6
                    for x in (-gap - width, gap):
                        draw_text(
                            painter,
                            QPointF(x, baseline),
                            text,
                            font,
                            color,
                            outline_color=outline,
                            outline_width=lw * 0.9,
                        )

    def _draw_roll_arc(self, painter: QPainter, rect: QRectF, roll: float) -> None:
        """Fixed tick scale at the top with a pointer that banks with the aircraft."""
        color = parse_color(self.theme.horizon_color)
        outline = parse_color(self.theme.outline_color)
        lw = self._line_width()
        radius = rect.height() * 0.52
        center = rect.center()
        painter.save()
        painter.translate(center)
        for angle in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
            length = radius * (0.10 if angle % 30 == 0 else 0.06)
            painter.save()
            painter.rotate(angle)
            p1 = QPointF(0, -radius)
            p2 = QPointF(0, -radius + length)
            if outline.alpha():
                painter.setPen(self._pen(outline, lw * 2.4))
                painter.drawLine(p1, p2)
            painter.setPen(self._pen(color, lw))
            painter.drawLine(p1, p2)
            painter.restore()
        # Bank pointer: a small triangle riding the scale.
        painter.rotate(-roll)
        size = radius * 0.075
        pointer = [
            QPointF(0, -radius + size * 0.3),
            QPointF(-size, -radius + size * 2.0),
            QPointF(size, -radius + size * 2.0),
        ]
        painter.setPen(self._pen(outline, lw * 2.0) if outline.alpha() else Qt.NoPen)
        painter.setBrush(parse_color(self.theme.horizon_reference_color))
        painter.drawPolygon(pointer)
        painter.restore()

    def _draw_reference(
        self, painter: QPainter, rect: QRectF, *, failed: bool
    ) -> None:
        """Aircraft symbol: two wings and a centre dot, fixed to the frame."""
        color = parse_color(
            self.theme.no_data_color if failed else self.theme.horizon_reference_color
        )
        outline = parse_color(self.theme.outline_color)
        lw = self._line_width() * 1.3
        center = rect.center()
        wing = rect.width() * 0.11
        gap = rect.width() * 0.03
        segments = [
            (QPointF(center.x() - gap - wing, center.y()), QPointF(center.x() - gap, center.y())),
            (QPointF(center.x() + gap, center.y()), QPointF(center.x() + gap + wing, center.y())),
        ]
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for width, col in ((lw * 2.4, outline), (lw, color)):
            if not col.alpha():
                continue
            painter.setPen(self._pen(col, width))
            for p1, p2 in segments:
                painter.drawLine(p1, p2)
            painter.drawPoint(center)
