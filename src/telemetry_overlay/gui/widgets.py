"""Small reusable widgets shared across tabs.

These exist so every tab has the same anatomy: a step header, a compact form
whose numeric fields keep their natural width, one accent-coloured primary
button per tab, and section labels above each output area.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

#: The single accent colour for primary actions, shared by every tab.
ACCENT = "#1f8f5f"


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


def compact_form() -> QFormLayout:
    """A form whose fields keep their natural width instead of filling the row.

    Qt's default growth policy stretches any field whose size policy allows it,
    which is every spin box and combo box -- producing a window-wide box around
    three digits. ``ExpandingFieldsGrow`` only stretches fields that actively ask
    for it (a ``QLineEdit`` holding a path), so no per-field override is needed.
    """
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(8)
    return form


def primary_button(text: str) -> QPushButton:
    """The one accent-coloured action per tab, sized to its label.

    A bare ``QPushButton`` in a vertical layout stretches to the full container
    width; pinning the size policy keeps it a button rather than a bar.
    """
    button = QPushButton(text)
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.setStyleSheet(
        "QPushButton {"
        f"  background: {ACCENT};"
        "  color: white;"
        "  border: none;"
        "  border-radius: 4px;"
        "  padding: 7px 18px;"
        "  font-weight: 600;"
        "}"
        "QPushButton:disabled { background: palette(mid); color: palette(base); }"
    )
    return button


def section_label(text: str) -> QLabel:
    """Heading for an output area (a plot, an image, the preview frame)."""
    label = QLabel(text)
    label.setStyleSheet("QLabel { font-weight: 600; }")
    return label


def step_header(title: str, subtitle: str) -> QWidget:
    """A tab's title plus one line saying what this step does.

    Every tab starts with one: the tabs are steps in a workflow, and the bar
    alone ("Align", "Export") does not say what the step is for.
    """
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)

    heading = QLabel(title)
    heading.setStyleSheet("QLabel { font-size: 16px; font-weight: 600; }")
    caption = QLabel(subtitle)
    caption.setWordWrap(True)
    caption.setStyleSheet("QLabel { color: palette(mid); }")
    layout.addWidget(heading)
    layout.addWidget(caption)
    return container
