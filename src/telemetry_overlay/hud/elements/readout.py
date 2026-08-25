"""Text readouts: label, value and unit.

All numeric readouts share one layout engine. The value occupies a field of fixed
character width so the digits never shift as the number changes -- the single biggest
difference between an overlay that looks built-in and one that looks bolted on.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QColor, QPainter

from ... import units as units_mod
from ...telemetry import fields
from ...telemetry.timeline import FrameState
from ..preset import ElementConfig
from .base import HudElement, RenderContext, draw_text, parse_color
from .registry import register

#: Reserved integer digits per quantity kind, used when a preset does not say.
_DEFAULT_DIGITS = {
    units_mod.SPEED: 3,
    units_mod.LENGTH: 4,
    units_mod.VERTICAL_SPEED: 2,
    units_mod.VOLTAGE: 2,
    units_mod.CURRENT: 2,
    units_mod.CHARGE: 4,
    units_mod.PERCENT: 3,
    units_mod.ANGLE: 3,
}


@dataclass(frozen=True)
class Content:
    """What a readout shows this frame. Doubles as the render cache key."""

    label: str
    value: str
    unit: str
    #: True when the channel has no usable data and ``value`` is placeholder text.
    no_data: bool = False
    #: True when the value is outside its configured safe band.
    warn: bool = False
    #: Hide the label, e.g. so "NO AGL" reads as a sentence on its own.
    hide_label: bool = False


class TextReadout(HudElement):
    """Layout and painting for a label/value/unit triple."""

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        super().__init__(config, ctx)
        self.layout: str = self.option("layout", "inline")
        if self.layout not in ("inline", "stacked"):
            raise ValueError(
                f"element {config.id}: layout must be 'inline' or 'stacked'"
            )
        self.show_label: bool = bool(self.option("show_label", True))
        self.show_unit: bool = bool(self.option("show_unit", True))

    # ---- subclass hook -----------------------------------------------------

    def content(self, state: FrameState) -> Content:
        raise NotImplementedError

    def reserved_text(self) -> str:
        """Widest string the value field must accommodate."""
        raise NotImplementedError

    def static_label(self) -> str:
        """Label used for sizing; the drawn label may differ in case only."""
        raise NotImplementedError

    # ---- geometry ----------------------------------------------------------

    @property
    def _value_size(self) -> float:
        return self.scaled(self.theme.value_size)

    @property
    def _label_size(self) -> float:
        return self.scaled(self.theme.label_size)

    @property
    def _unit_size(self) -> float:
        return self.scaled(self.theme.unit_size)

    def _gap(self) -> float:
        return self.ctx.px(self._value_size) * 0.32

    def _widths(self) -> tuple[float, float, float]:
        vm = self.ctx.metrics(self._value_size)
        lm = self.ctx.metrics(self._label_size)
        um = self.ctx.metrics(self._unit_size)
        label_w = (
            lm.horizontalAdvance(self.static_label()) if self.show_label else 0.0
        )
        value_w = vm.horizontalAdvance(self.reserved_text())
        unit_w = um.horizontalAdvance(self._unit_text()) if self.show_unit else 0.0
        return label_w, value_w, unit_w

    def _unit_text(self) -> str:
        return ""

    def size(self) -> QSizeF:
        label_w, value_w, unit_w = self._widths()
        gap = self._gap()
        vm = self.ctx.metrics(self._value_size)
        lm = self.ctx.metrics(self._label_size)
        if self.layout == "inline":
            width = value_w
            if label_w:
                width += label_w + gap
            if unit_w:
                width += unit_w + gap * 0.6
            return QSizeF(width, vm.height())
        width = max(label_w, value_w + (unit_w + gap * 0.6 if unit_w else 0.0))
        height = vm.height() + (lm.height() * 0.92 if label_w else 0.0)
        return QSizeF(width, height)

    # ---- drawing ----------------------------------------------------------

    def cache_key(self, state: FrameState) -> Content:
        return self.content(state)

    def draw(self, painter: QPainter, state: FrameState) -> None:
        content = self.content(state)
        rect = self.rect()
        vm = self.ctx.metrics(self._value_size)
        lm = self.ctx.metrics(self._label_size)
        um = self.ctx.metrics(self._unit_size)
        label_w, value_w, unit_w = self._widths()
        gap = self._gap()

        show_label = self.show_label and not content.hide_label and bool(content.label)
        value_color = self._value_color(content)
        outline = parse_color(self.theme.outline_color)
        outline_w = self.ctx.px(self._value_size) * self.theme.outline_width

        self._draw_plate(painter, rect)

        if self.layout == "inline":
            baseline_y = rect.top() + vm.ascent()
            x = rect.left()
            if label_w:
                if show_label:
                    draw_text(
                        painter,
                        QPointF(x, baseline_y),
                        content.label,
                        self.ctx.font(self._label_size),
                        parse_color(self.theme.label_color),
                        outline_color=outline,
                        outline_width=outline_w * 0.8,
                    )
                x += label_w + gap
            value_x = x + value_w - vm.horizontalAdvance(content.value)
            draw_text(
                painter,
                QPointF(value_x, baseline_y),
                content.value,
                self.ctx.font(self._value_size),
                value_color,
                outline_color=outline,
                outline_width=outline_w,
            )
            x += value_w + gap * 0.6
            if unit_w and not content.no_data:
                draw_text(
                    painter,
                    QPointF(x, baseline_y),
                    content.unit,
                    self.ctx.font(self._unit_size),
                    parse_color(self.theme.unit_color),
                    outline_color=outline,
                    outline_width=outline_w * 0.7,
                )
            return

        # stacked
        top = rect.top()
        if label_w:
            if show_label:
                draw_text(
                    painter,
                    QPointF(rect.left(), top + lm.ascent()),
                    content.label,
                    self.ctx.font(self._label_size),
                    parse_color(self.theme.label_color),
                    outline_color=outline,
                    outline_width=outline_w * 0.8,
                )
            top += lm.height() * 0.92
        baseline_y = top + vm.ascent()
        value_x = rect.left() + value_w - vm.horizontalAdvance(content.value)
        draw_text(
            painter,
            QPointF(value_x, baseline_y),
            content.value,
            self.ctx.font(self._value_size),
            value_color,
            outline_color=outline,
            outline_width=outline_w,
        )
        if unit_w and not content.no_data:
            draw_text(
                painter,
                QPointF(rect.left() + value_w + self._gap() * 0.6, baseline_y),
                content.unit,
                self.ctx.font(self._unit_size),
                parse_color(self.theme.unit_color),
                outline_color=outline,
                outline_width=outline_w * 0.7,
            )

    def _value_color(self, content: Content) -> QColor:
        if content.no_data:
            return parse_color(self.theme.no_data_color)
        if content.warn:
            return parse_color(self.theme.warn_color)
        return parse_color(self.theme.value_color)

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


@register
class ChannelReadout(TextReadout):
    """A numeric telemetry channel.

    Options: ``channel`` (required), ``label``, ``digits``, ``signed``,
    ``no_data_text``, ``no_data_hides_label``, ``warn_below``, ``warn_above``.
    Warning thresholds are in SI units (m/s, m, V) regardless of the display unit.
    """

    type_name = "readout"
    display_name = "Channel readout"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        super().__init__(config, ctx)
        self.channel: str = self.option("channel", "")
        spec = fields.CHANNELS_BY_NAME.get(self.channel)
        if spec is None:
            raise ValueError(
                f"element {config.id}: unknown channel {self.channel!r}; known: "
                f"{', '.join(fields.CHANNELS_BY_NAME)}"
            )
        self.spec = spec
        self.unit = units_mod.resolve_unit(config.unit, spec.kind)
        # Also honour "decimals" written inside options: it is an easy mistake to make
        # in a hand-edited preset, and silently ignoring it is worse than accepting it.
        decimals = config.decimals
        if decimals is None:
            decimals = self.option("decimals")
        self.decimals = self.unit.decimals if decimals is None else int(decimals)
        self.label = str(self.option("label", spec.label))
        self.signed = bool(self.option("signed", spec.name == fields.VS))
        self.digits = int(self.option("digits", _DEFAULT_DIGITS.get(spec.kind, 3)))
        self.no_data_text = str(self.option("no_data_text", "---"))
        self.no_data_hides_label = bool(self.option("no_data_hides_label", False))
        self.warn_below = self.option("warn_below")
        self.warn_above = self.option("warn_above")

    def static_label(self) -> str:
        return self.label

    def _unit_text(self) -> str:
        return self.unit.symbol

    def reserved_text(self) -> str:
        numeric = "0" * self.digits
        if self.decimals:
            numeric += "." + "0" * self.decimals
        if self.signed:
            numeric = "-" + numeric
        # The no-data placeholder must fit too, or the box would clip it.
        return numeric if len(numeric) >= len(self.no_data_text) else self.no_data_text

    def format_value(self, si_value: float) -> str:
        text = self.unit.format(si_value, self.decimals)
        if self.signed and not text.startswith("-"):
            text = "+" + text
        return text

    def content(self, state: FrameState) -> Content:
        value = state.value(self.channel)
        if value is None:
            return Content(
                self.label,
                self.no_data_text,
                self.unit.symbol,
                no_data=True,
                hide_label=self.no_data_hides_label,
            )
        warn = False
        if self.warn_below is not None and value < float(self.warn_below):
            warn = True
        if self.warn_above is not None and value > float(self.warn_above):
            warn = True
        return Content(self.label, self.format_value(value), self.unit.symbol, warn=warn)


@register
class HeadingReadout(ChannelReadout):
    """Compass heading, always three digits (001..360)."""

    type_name = "heading"
    display_name = "Heading"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        config.options.setdefault("channel", fields.YAW)
        config.options.setdefault("label", "HDG")
        config.options.setdefault("show_unit", False)
        super().__init__(config, ctx)
        self.digits = 3
        self.decimals = 0
        self.signed = False

    def format_value(self, si_value: float) -> str:
        return units_mod.format_heading(si_value)


@register
class ModeReadout(TextReadout):
    """Current ArduPilot flight mode.

    Options: ``label`` (default empty), ``width_chars`` (default 10, wide enough for
    ``LOITER_ALT_QLAND`` when set to 16).
    """

    type_name = "mode"
    display_name = "Flight mode"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        config.options.setdefault("show_label", False)
        config.options.setdefault("show_unit", False)
        super().__init__(config, ctx)
        self.label = str(self.option("label", "MODE"))
        self.width_chars = int(self.option("width_chars", 10))
        self.no_data_text = str(self.option("no_data_text", "---"))

    def static_label(self) -> str:
        return self.label

    def reserved_text(self) -> str:
        return "0" * self.width_chars

    def content(self, state: FrameState) -> Content:
        if not state.mode:
            return Content(self.label, self.no_data_text, "", no_data=True)
        return Content(self.label, state.mode[: self.width_chars], "")

    def draw(self, painter: QPainter, state: FrameState) -> None:
        # Mode names are words, not numbers: left-align them in the reserved field
        # instead of right-aligning like a digit column.
        content = self.content(state)
        rect = self.rect()
        vm = self.ctx.metrics(self._value_size)
        self._draw_plate(painter, rect)
        draw_text(
            painter,
            QPointF(rect.left(), rect.top() + vm.ascent()),
            content.value,
            self.ctx.font(self._value_size),
            self._value_color(content),
            outline_color=parse_color(self.theme.outline_color),
            outline_width=self.ctx.px(self._value_size) * self.theme.outline_width,
        )


@register
class TimerReadout(TextReadout):
    """Time since arming, as ``MM:SS``.

    Options: ``label`` (default "T"), ``source``: ``"flight"`` (since arming) or
    ``"video"`` (elapsed clip time).
    """

    type_name = "timer"
    display_name = "Flight timer"

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        config.options.setdefault("show_unit", False)
        super().__init__(config, ctx)
        self.label = str(self.option("label", "T"))
        self.source = str(self.option("source", "flight"))
        if self.source not in ("flight", "video"):
            raise ValueError(
                f"element {config.id}: timer source must be 'flight' or 'video'"
            )

    def static_label(self) -> str:
        return self.label

    def reserved_text(self) -> str:
        return "00:00"

    def content(self, state: FrameState) -> Content:
        seconds = (
            state.video_time if self.source == "video" else state.flight_time
        )
        if seconds is None:
            return Content(self.label, "--:--", "", no_data=True)
        return Content(self.label, units_mod.format_duration(seconds), "")
