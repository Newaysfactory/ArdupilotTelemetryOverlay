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


def test_value_to_fraction_and_back_round_trips():
    from telemetry_overlay.gui.range_slider import fraction_to_value, value_to_fraction

    assert value_to_fraction(25.0, 0.0, 100.0) == pytest.approx(0.25)
    assert fraction_to_value(0.25, 0.0, 100.0) == pytest.approx(25.0)
    # Out-of-range values clamp instead of extrapolating.
    assert value_to_fraction(-10.0, 0.0, 100.0) == 0.0
    assert value_to_fraction(150.0, 0.0, 100.0) == 1.0
    # A degenerate span (e.g. a zero-length video) must not divide by zero.
    assert value_to_fraction(5.0, 10.0, 10.0) == 0.0


def test_range_slider_low_high_never_cross(qapp):
    from telemetry_overlay.gui.range_slider import RangeSlider

    slider = RangeSlider()
    slider.setRange(0.0, 100.0)
    slider.setLow(40.0)
    slider.setHigh(60.0)

    # Dragging the low handle past the high one clamps at the high value, and
    # vice versa -- the two handles can touch but never invert.
    slider.setLow(90.0)
    assert slider.low() == pytest.approx(60.0)
    slider.setHigh(10.0)
    assert slider.high() == pytest.approx(60.0)


def test_range_slider_set_range_clamps_existing_values(qapp):
    from telemetry_overlay.gui.range_slider import RangeSlider

    slider = RangeSlider()
    slider.setRange(0.0, 100.0)
    slider.setLowHigh(20.0, 80.0)
    slider.setPosition(50.0)

    # Loading a shorter video must not leave stale handles outside the new range.
    slider.setRange(0.0, 30.0)
    assert slider.low() == pytest.approx(20.0)
    assert slider.high() == pytest.approx(30.0)
    assert slider.position() == pytest.approx(30.0)


def test_range_slider_set_low_high_is_order_independent(qapp):
    """setLow()/setHigh() clamp against each other's *current* value, so calling
    them back to back to sync both handles at once from an external source is
    order-dependent (a low above the old high gets clamped down before the high
    update runs). setLowHigh() must set both correctly regardless of the
    previous state -- this is the bug that motivated adding it."""
    from telemetry_overlay.gui.range_slider import RangeSlider

    slider = RangeSlider()
    slider.setRange(0.0, 100.0)  # starts with the default low=0.0, high=1.0
    slider.setLowHigh(50.0, 150.0)
    assert slider.low() == pytest.approx(50.0)
    assert slider.high() == pytest.approx(100.0)  # clamped to the slider's maximum
