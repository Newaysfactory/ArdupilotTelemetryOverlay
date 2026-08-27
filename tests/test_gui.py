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


def test_format_drift_translates_the_factor_into_seconds():
    """A bare factor cannot be judged; the hint next to the field is what makes
    it readable, so its wording is worth pinning."""
    from telemetry_overlay.gui.align_tab import format_drift

    assert format_drift(1.0) == "= no drift"
    assert format_drift(1.001467) == "= +1.47 s every 1000 s"
    # A camera clock running fast relative to the autopilot reads as negative.
    assert format_drift(0.999) == "= -1.00 s every 1000 s"


def test_alignment_is_not_persisted_until_save_sync(qapp, tmp_path, monkeypatch):
    """Auto align and the plot drag write the working alignment, never the file.

    The split is the whole point of the Save alignment button: the preview and
    the export follow along immediately, but nothing reaches sync.json until the
    user asks for it.
    """
    monkeypatch.setenv("TELEMETRY_OVERLAY_CACHE", str(tmp_path / "cache"))
    from telemetry_overlay.gui.controller import ProjectController
    from telemetry_overlay.sync import SyncModel

    controller = ProjectController()
    # Only the path is used here; set_video() would probe a real container.
    controller.video = tmp_path / "clip.MP4"

    assert not controller.has_alignment
    assert not controller.sync_is_saved

    controller.set_sync(SyncModel(log_delay=12.5, scale=1.001, note="autosync"))
    assert controller.has_alignment
    assert not controller.sync_is_saved, "showing a result must not write it to disk"
    assert not SyncModel.path_for(controller.video).exists()

    path = controller.save_sync()
    assert controller.sync_is_saved
    assert SyncModel.load(path).log_delay == pytest.approx(12.5)

    # A drag after saving makes it dirty again, so the header can say so.
    controller.nudge_log_delay(0.25)
    assert not controller.sync_is_saved
    assert SyncModel.load(path).log_delay == pytest.approx(12.5)


def test_loading_a_video_with_a_sync_file_starts_clean(qapp, tmp_path, monkeypatch):
    """An alignment read back at startup is 'saved', not pending work.

    Goes through set_video() rather than assigning the state by hand: loading
    the file and marking it as the on-disk baseline happen there, and that
    pairing is what the header's saved/unsaved indicator depends on.
    """
    import types

    monkeypatch.setenv("TELEMETRY_OVERLAY_CACHE", str(tmp_path / "cache"))
    from telemetry_overlay.gui import controller as controller_module
    from telemetry_overlay.sync import SyncModel

    video = tmp_path / "clip.MP4"
    SyncModel(log_delay=7.5, scale=1.0002).save(SyncModel.path_for(video))
    monkeypatch.setattr(
        controller_module, "probe_video", lambda path: types.SimpleNamespace(duration=60.0)
    )

    controller = controller_module.ProjectController()
    controller.set_video(video)

    assert controller.sync.log_delay == pytest.approx(7.5)
    assert controller.has_alignment
    assert controller.sync_is_saved
    # The whole video becomes the shared From/To range.
    assert (controller.range_start, controller.range_end) == pytest.approx((0.0, 60.0))


def test_loading_a_video_without_a_sync_file_is_not_aligned(qapp, tmp_path, monkeypatch):
    """Delay 0 / drift 1 with no file means 'not aligned yet', not 'aligned to zero'."""
    import types

    monkeypatch.setenv("TELEMETRY_OVERLAY_CACHE", str(tmp_path / "cache"))
    from telemetry_overlay.gui import controller as controller_module

    monkeypatch.setattr(
        controller_module, "probe_video", lambda path: types.SimpleNamespace(duration=60.0)
    )
    controller = controller_module.ProjectController()
    controller.set_video(tmp_path / "fresh.MP4")

    assert not controller.has_alignment
    assert not controller.sync_is_saved


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
