"""Headless tests for the GUI's pure-ish logic: no window is shown."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="session")
def qapp():
    """A QApplication, needed for any QWidget (QGuiApplication alone is not enough).

    Skips rather than crashes if some other test already created a bare
    QGuiApplication in this session (a QApplication can't be layered on top of
    one after the fact) -- see the note in gui/app.py about creation order.
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is not None:
        return existing
    if QCoreApplication.instance() is not None:
        pytest.skip("a non-QApplication QCoreApplication already exists in this session")
    return QApplication([])


def test_terminal_plain_text_appends_lines(qapp):
    from telemetry_overlay.gui.terminal import TerminalWidget

    widget = TerminalWidget()
    widget._on_append("first line\n")
    widget._on_append("second line\n")
    assert widget.toPlainText() == "first line\nsecond line\n"


def test_terminal_carriage_return_overwrites_current_line(qapp):
    """Mirrors cli.py's _progress_bar: repeated '\\r...' updates, no newline."""
    from telemetry_overlay.gui.terminal import TerminalWidget

    widget = TerminalWidget()
    widget._on_append("prefix [----]   0%")
    widget._on_append("\rprefix [##--]  50%")
    widget._on_append("\rprefix [####] 100%")
    assert widget.toPlainText() == "prefix [####] 100%"


def test_terminal_carriage_return_then_newline_finalises_line(qapp):
    from telemetry_overlay.gui.terminal import TerminalWidget

    widget = TerminalWidget()
    widget._on_append("prefix [----]   0%")
    widget._on_append("\rprefix [####] 100%\n")
    widget._on_append("next command started\n")
    assert widget.toPlainText() == "prefix [####] 100%\nnext command started\n"
