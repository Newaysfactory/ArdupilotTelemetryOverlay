"""Small reusable widgets shared across tabs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


def help_icon(text: str) -> QLabel:
    """A small, muted '?' badge that shows ``text`` on hover.

    A tooltip set directly on a field is invisible until the user happens to
    hover it; a little marker tells them there is something to hover -- kept
    deliberately quiet (a thin grey outline, no colour, no emoji) so it reads
    as a discoverable detail rather than a warning or a decoration.
    """
    icon = QLabel("?")
    icon.setToolTip(text)
    icon.setCursor(Qt.CursorShape.WhatsThisCursor)
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setFixedSize(14, 14)
    icon.setStyleSheet(
        "QLabel {"
        "  color: palette(mid);"
        "  border: 1px solid palette(mid);"
        "  border-radius: 7px;"
        "  font-size: 10px;"
        "}"
    )
    return icon


def field_label(text: str, help_text: str) -> QWidget:
    """A form-row label with a help icon next to it; use as the ``QFormLayout``
    label widget instead of a bare string when the field needs explaining."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(QLabel(text))
    layout.addWidget(help_icon(help_text))
    layout.addStretch(1)
    return container
