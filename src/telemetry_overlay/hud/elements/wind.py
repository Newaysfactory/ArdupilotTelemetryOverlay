"""Wind widget: a rotating arrow plus a speed readout.

Both come from the EKF wind estimate (``XKF2.VWN/VWE``); when the EKF has no wind
estimate the whole widget shows "no data" instead of drawing a meaningless arrow.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

from ... import units as units_mod
from ...telemetry import fields
from ...telemetry.timeline import FrameState
from ..preset import ElementConfig
from .base import HudElement, RenderContext, draw_text, parse_color
from .registry import register


@register
class WindReadout(HudElement):
    """Wind speed and direction, drawn as a label, a rotating arrow and a value.

    The arrow points where the wind is blowing *toward*, relative to the aircraft's
    nose (straight up = dead ahead) -- the same convention as a G1000's wind vector,
    and the only one that is readable on an FPV overlay where the frame is always
    nose-forward rather than north-up.

    Options: ``label`` (default "WIND"), ``show_label``, ``digits`` (default 2),
    ``decimals``, ``no_data_text`` (default "---"), ``arrow_size`` (arrow diameter as
    a fraction of the value text height, default 1.15).
    """

    type_name = "wind"
    display_name = "Wind"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        super().__init__(config, ctx)
        self.show_label = bool(self.option("show_label", True))
        self.label = str(self.option("label", "WIND"))
        self.unit = units_mod.resolve_unit(config.unit, units_mod.SPEED)
        decimals = config.decimals
        if decimals is None:
            decimals = self.option("decimals")
        self.decimals = self.unit.decimals if decimals is None else int(decimals)
        self.digits = int(self.option("digits", 2))
        self.no_data_text = str(self.option("no_data_text", "---"))
        self.arrow_size = float(self.option("arrow_size", 1.15))

    # ---- geometry ------------------------------------------------------------

    @property
    def _value_size(self) -> float:
        return self.scaled(self.theme.value_size)

    @property
    def _label_size(self) -> float:
        return self.scaled(self.theme.label_size)

    @property
    def _unit_size(self) -> float:
        return self.scaled(self.theme.unit_size)

    def _reserved_value(self) -> str:
        numeric = "0" * self.digits
        if self.decimals:
            numeric += "." + "0" * self.decimals
        return numeric if len(numeric) >= len(self.no_data_text) else self.no_data_text

    def _gap(self) -> float:
        return self.ctx.px(self._value_size) * 0.32

    def _arrow_diameter(self) -> float:
        return self.ctx.metrics(self._value_size).height() * self.arrow_size

    def size(self) -> QSizeF:
        vm = self.ctx.metrics(self._value_size)
        lm = self.ctx.metrics(self._label_size)
        um = self.ctx.metrics(self._unit_size)
        gap = self._gap()
        label_w = lm.horizontalAdvance(self.label) if self.show_label else 0.0
        value_w = vm.horizontalAdvance(self._reserved_value())
        unit_w = um.horizontalAdvance(self.unit.symbol)
        arrow_w = self._arrow_diameter()
        width = arrow_w + gap + value_w + gap * 0.6 + unit_w
        if label_w:
            width += label_w + gap
        return QSizeF(width, max(vm.height(), arrow_w))

    # ---- drawing ---------------------------------------------------------------

    def cache_key(self, state: FrameState) -> tuple:
        speed = state.value(fields.WIND_SPEED)
        direction = state.value(fields.WIND_DIR)
        if speed is None or direction is None:
            return ("no-data",)
        heading = state.value(fields.YAW)
        relative = None if heading is None else round((direction - heading) % 360.0)
        return (round(speed, 1), relative)

    def draw(self, painter: QPainter, state: FrameState) -> None:
        rect = self.rect()
        vm = self.ctx.metrics(self._value_size)
        lm = self.ctx.metrics(self._label_size)
        um = self.ctx.metrics(self._unit_size)
        gap = self._gap()
        outline = parse_color(self.theme.outline_color)
        outline_w = self.ctx.px(self._value_size) * self.theme.outline_width

        self._draw_plate(painter, rect)

        x = rect.left()
        baseline_y = rect.top() + vm.ascent()
        if self.show_label:
            draw_text(
                painter,
                QPointF(x, baseline_y),
                self.label,
                self.ctx.font(self._label_size),
                parse_color(self.theme.label_color),
                outline_color=outline,
                outline_width=outline_w * 0.8,
            )
            x += lm.horizontalAdvance(self.label) + gap

        speed = state.value(fields.WIND_SPEED)
        direction = state.value(fields.WIND_DIR)
        heading = state.value(fields.YAW)
        no_data = speed is None or direction is None
        relative = 0.0 if no_data or heading is None else (direction - heading) % 360.0

        arrow_d = self._arrow_diameter()
        arrow_rect = QRectF(x, rect.center().y() - arrow_d / 2.0, arrow_d, arrow_d)
        self._draw_arrow(painter, arrow_rect, relative, no_data)
        x += arrow_d + gap

        value_text = (
            self.no_data_text if no_data else self.unit.format(speed, self.decimals)
        )
        value_w = vm.horizontalAdvance(self._reserved_value())
        value_x = x + value_w - vm.horizontalAdvance(value_text)
        value_color = parse_color(
            self.theme.no_data_color if no_data else self.theme.value_color
        )
        draw_text(
            painter,
            QPointF(value_x, baseline_y),
            value_text,
            self.ctx.font(self._value_size),
            value_color,
            outline_color=outline,
            outline_width=outline_w,
        )
        x += value_w + gap * 0.6
        if not no_data:
            draw_text(
                painter,
                QPointF(x, baseline_y),
                self.unit.symbol,
                self.ctx.font(self._unit_size),
                parse_color(self.theme.unit_color),
                outline_color=outline,
                outline_width=outline_w * 0.7,
            )

    def _draw_plate(self, painter: QPainter, rect: QRectF) -> None:
        plate = parse_color(self.theme.plate_color)
        if not plate.alpha():
            return
        pad = self.ctx.px(self._value_size) * self.theme.plate_padding
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(plate)
        painter.drawRoundedRect(
            rect.adjusted(-pad, -pad * 0.5, pad, pad * 0.5), pad * 0.4, pad * 0.4
        )

    def _draw_arrow(
        self, painter: QPainter, rect: QRectF, relative_bearing: float, no_data: bool
    ) -> None:
        """A triangle pointing at ``relative_bearing`` degrees from straight up."""
        center = rect.center()
        radius = rect.width() / 2.0
        color = parse_color(
            self.theme.no_data_color if no_data else self.theme.value_color
        )
        outline = parse_color(self.theme.outline_color)
        outline_w = max(1.0, self.ctx.px(self._value_size) * self.theme.outline_width * 0.6)

        if no_data:
            # A static ring instead of a needle communicates "no reading" without
            # implying any particular direction.
            painter.save()
            pen = QPen(color, max(1.0, radius * 0.12))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius * 0.6, radius * 0.6)
            painter.restore()
            return

        path = QPainterPath()
        path.moveTo(0, -radius)
        path.lineTo(radius * 0.55, radius * 0.6)
        path.lineTo(0, radius * 0.2)
        path.lineTo(-radius * 0.55, radius * 0.6)
        path.closeSubpath()

        painter.save()
        painter.translate(center)
        painter.rotate(relative_bearing)
        pen = QPen(outline, outline_w)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QBrush(color))
        painter.drawPath(path)
        painter.restore()
