"""Element base class, render context and text drawing helpers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)

from ...telemetry.timeline import FrameState
from ..preset import ElementConfig
from ..theme import Theme

log = logging.getLogger(__name__)


def parse_color(value: str | None) -> QColor:
    """``#RRGGBB``/``#RRGGBBAA`` to a colour; empty or invalid means transparent."""
    if not value:
        return QColor(0, 0, 0, 0)
    text = value.strip()
    if text.startswith("#") and len(text) == 9:
        r, g, b, a = (int(text[i : i + 2], 16) for i in (1, 3, 5, 7))
        return QColor(r, g, b, a)
    color = QColor(text)
    return color if color.isValid() else QColor(0, 0, 0, 0)


#: Tried in order when the theme's family is not installed. All are monospaced.
FONT_FALLBACKS = (
    "Consolas",
    "Cascadia Mono",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Menlo",
    "Courier New",
)


@dataclass
class RenderContext:
    """Frame geometry plus cached fonts, shared by every element of one render."""

    width: int
    height: int
    theme: Theme
    _fonts: dict[tuple[float, int], QFont] = field(default_factory=dict, repr=False)
    _metrics: dict[tuple[float, int], QFontMetricsF] = field(
        default_factory=dict, repr=False
    )
    _family: str | None = field(default=None, repr=False)

    def px(self, fraction: float) -> float:
        """Convert a fraction of frame height into pixels."""
        return fraction * self.height

    def family(self) -> str:
        """Resolve the theme's font family once, falling back if it is not installed.

        A missing family would otherwise be silently substituted by Qt with a
        proportional face, which reintroduces the digit jitter the monospace choice
        exists to prevent.
        """
        if self._family is not None:
            return self._family
        installed = set(QFontDatabase.families())
        if self.theme.font_file:
            path = Path(self.theme.font_file)
            font_id = QFontDatabase.addApplicationFont(str(path))
            families = (
                QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            )
            if families:
                self._family = families[0]
                return self._family
            log.warning("could not load font file %s; falling back", path)
        for candidate in (self.theme.font_family, *FONT_FALLBACKS):
            if candidate and candidate in installed:
                if candidate != self.theme.font_family:
                    log.warning(
                        "font %r is not installed; using %r instead",
                        self.theme.font_family,
                        candidate,
                    )
                self._family = candidate
                return self._family
        log.warning(
            "no monospaced font from the fallback list is installed; using %r",
            self.theme.font_family,
        )
        self._family = self.theme.font_family
        return self._family

    def font(self, size_fraction: float, weight: int | None = None) -> QFont:
        weight = self.theme.font_weight if weight is None else weight
        key = (round(self.px(size_fraction), 2), weight)
        font = self._fonts.get(key)
        if font is None:
            font = QFont(self.family())
            font.setPixelSize(max(1, int(round(key[0]))))
            font.setWeight(QFont.Weight(weight))
            # Integer advances keep a monospace column grid exact at any size.
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            self._fonts[key] = font
        return font

    def metrics(self, size_fraction: float, weight: int | None = None) -> QFontMetricsF:
        weight = self.theme.font_weight if weight is None else weight
        key = (round(self.px(size_fraction), 2), weight)
        m = self._metrics.get(key)
        if m is None:
            m = QFontMetricsF(self.font(size_fraction, weight))
            self._metrics[key] = m
        return m


def draw_text(
    painter: QPainter,
    baseline: QPointF,
    text: str,
    font: QFont,
    color: QColor,
    *,
    outline_color: QColor | None = None,
    outline_width: float = 0.0,
) -> None:
    """Draw text with an outline so it stays legible over any footage.

    A stroked glyph path costs more than ``drawText`` but is the only way to keep white
    numbers readable over a bright sky, and at HUD sizes the cost is irrelevant.
    """
    if not text:
        return
    if outline_color is not None and outline_width > 0 and outline_color.alpha():
        path = QPainterPath()
        path.addText(baseline, font, text)
        # Stroke first, fill second. A single drawPath() would stroke *over* the fill,
        # and since half of a pen's width falls inside the glyph outline that eats the
        # stems -- at HUD sizes the text comes out solid black.
        pen = QPen(outline_color, outline_width * 2.0)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.strokePath(path, pen)
        painter.fillPath(path, color)
        return
    painter.setPen(color)
    painter.setFont(font)
    painter.drawText(baseline, text)


def anchor_rect(x: float, y: float, size: QSizeF, anchor: str) -> QRectF:
    """Place a box of ``size`` so that its ``anchor`` point sits at ``(x, y)``."""
    vertical, _, horizontal = anchor.partition("-")
    if not horizontal:  # the bare "center" anchor centres on both axes
        horizontal = vertical
    left = {
        "left": x,
        "center": x - size.width() / 2.0,
        "right": x - size.width(),
    }[horizontal]
    top = {
        "top": y,
        "center": y - size.height() / 2.0,
        "bottom": y - size.height(),
    }[vertical]
    return QRectF(left, top, size.width(), size.height())


class HudElement(ABC):
    """One drawable overlay item.

    Elements report a **fixed** box size that does not depend on the current telemetry
    value. That is deliberate twice over: the readout does not shuffle sideways as
    digits change, and the exporter can compute the overlay's dirty regions once for
    the whole render instead of per frame.
    """

    #: Registry key used in preset files.
    type_name: str = ""
    #: Human-readable name for the GUI's element list.
    display_name: str = ""

    def __init__(self, config: ElementConfig, ctx: RenderContext) -> None:
        self.config = config
        self.ctx = ctx
        self.theme = ctx.theme

    # ---- geometry ----------------------------------------------------------

    @abstractmethod
    def size(self) -> QSizeF:
        """Box size in pixels, independent of the telemetry value."""

    def rect(self) -> QRectF:
        cfg = self.config
        return anchor_rect(
            cfg.x * self.ctx.width, cfg.y * self.ctx.height, self.size(), cfg.anchor
        )

    def bounds(self) -> QRectF:
        """Region the element may paint, clipped to the frame.

        Padded by the outline width so stroked glyph edges are never clipped away by
        the dirty-band optimisation.
        """
        pad = self.ctx.px(self.theme.value_size) * self.theme.outline_width + 2.0
        r = self.rect().adjusted(-pad, -pad, pad, pad)
        return r.intersected(QRectF(0, 0, self.ctx.width, self.ctx.height))

    # ---- drawing ----------------------------------------------------------

    @abstractmethod
    def draw(self, painter: QPainter, state: FrameState) -> None:
        """Paint the element, in frame coordinates."""

    def cache_key(self, state: FrameState) -> Any:
        """Value identifying the drawn appearance.

        The renderer skips repainting a region whose key is unchanged. Return
        ``state.frame`` (the default) for anything that must be redrawn every frame.
        """
        return state.frame

    # ---- helpers ----------------------------------------------------------

    def scaled(self, size_fraction: float) -> float:
        return size_fraction * self.config.scale

    def option(self, name: str, default: Any = None) -> Any:
        return self.config.options.get(name, default)
