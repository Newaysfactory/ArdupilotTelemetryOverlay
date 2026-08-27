"""Align tab: one place to line the telemetry up with the video.

Replaces the separate autosync/manualsync tabs. They were two ways of doing one
job -- and splitting them cost the user the one thing that makes an automatic
suggestion judgeable: seeing it land. Here the automatic search fills the same
live plot the drag acts on, so pressing "Run auto align" visibly moves the
orange trace onto the blue one, and dragging afterwards is a correction of the
same value rather than a separate workflow.

Two levels of "applied", deliberately:

* the search result and every drag write :attr:`ProjectController.sync`, so the
  preview and the export immediately reflect what you are looking at;
* only **Save alignment** writes ``sync.json``, so nothing is persisted until
  the user says so. The header shows which of the two states you are in.

One run is enough for both outputs: ``cmd_autosync`` (with a plot directory)
collects an :class:`AutoSyncDiagnostics` over the whole analysed span, which is
exactly what :class:`ManualSyncPlot` consumes -- so the live plot costs no extra
optical flow, and the fit PNG comes from the same pass.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..autosync import flow_is_cached, save_diagnostic_plots
from ..cache import ensure_cache_dir_for
from ..cli import cmd_autosync
from ..sync import SyncModel
from .controller import ProjectController
from .manual_sync_plot import ManualSyncPlot
from .terminal import TerminalWidget
from .widgets import (
    compact_form,
    field_label,
    help_icon,
    primary_button,
    section_label,
    step_header,
)
from .workers import CommandWorker
from .zoomable_image_view import ZoomableImageView

_PLOT_HELP = (
    "[Left-drag] slide the video trace to align it with the log\n"
    "[Wheel] zoom the time axis\n"
    "[Right-drag] rescale the vertical axis"
)

_FIT_HELP = (
    "One point per analysis window: the log delay each one found, against where it "
    "sits in the video. A straight line through them is the clock drift; scattered "
    "points mean the windows disagree and the result should not be trusted."
)


def format_drift(scale: float) -> str:
    """Turn a clock-drift factor into something with a felt size.

    ``1.001467`` says nothing on its own; "+1.47 s every 1000 s" can be judged
    against how long the flight was.
    """
    if scale == 1.0:
        return "= no drift"
    return f"= {(scale - 1.0) * 1000:+.2f} s every 1000 s"


class AlignTab(QWidget):
    """Automatic search and by-eye correction of the same two numbers."""

    def __init__(
        self,
        controller: ProjectController,
        terminal: TerminalWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.terminal = terminal
        self._worker: CommandWorker | None = None
        self._diagnostics = None
        self._result = None
        self._plot_dir: Path | None = None
        #: Guards against feeding a change straight back into the field that just
        #: produced it -- the fields, the plot drag and the shared controller all
        #: write the same two numbers.
        self._suppress_sync_feedback = False
        self._suppress_range_feedback = False
        #: The video the auto-populate-on-open check has already run for, so a
        #: cached analysis loads once per video rather than on every state change.
        self._auto_populate_video: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(
            step_header(
                "Align",
                "Line the telemetry up with the video. Run the automatic search for a "
                "starting point, then drag the orange trace if you want to correct it "
                "by hand — both end up in the same two numbers.",
            )
        )

        groups = QHBoxLayout()
        groups.setSpacing(14)
        # AlignTop: the two groups are different heights, and the shorter one
        # should keep its natural size instead of stretching into a hollow box.
        groups.addWidget(self._build_search_group(), 0, Qt.AlignmentFlag.AlignTop)
        groups.addWidget(self._build_alignment_group(), 0, Qt.AlignmentFlag.AlignTop)
        groups.addStretch(1)
        layout.addLayout(groups)

        layout.addWidget(self._build_result_card())
        layout.addLayout(self._build_plot_headers())
        layout.addLayout(self._build_plots(), 1)

        self.setEnabled(False)
        controller.state_changed.connect(self._on_state_changed)

    # ---- construction ------------------------------------------------------

    def _build_search_group(self) -> QGroupBox:
        group = QGroupBox("Automatic search")
        box = QVBoxLayout(group)
        box.setSpacing(8)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 1e6)
        self.start_spin.setSuffix(" s")
        self.start_spin.valueChanged.connect(self._on_range_edited)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 1e6)
        self.end_spin.setSuffix(" s")
        self.end_spin.valueChanged.connect(self._on_range_edited)

        self.windows_spin = QSpinBox()
        self.windows_spin.setRange(1, 50)
        self.windows_spin.setValue(6)

        self.window_length_spin = QDoubleSpinBox()
        self.window_length_spin.setRange(1.0, 300.0)
        self.window_length_spin.setSuffix(" s")
        self.window_length_spin.setValue(20.0)

        form = compact_form()
        form.addRow(
            field_label(
                "From",
                "Video time where the analysed span starts. Shared with the Preview "
                "tab's timeline handles and the Export tab.",
            ),
            self.start_spin,
        )
        form.addRow(
            field_label(
                "To",
                "Video time where the analysed span ends. Shared with the Preview "
                "tab's timeline handles and the Export tab.",
            ),
            self.end_spin,
        )
        form.addRow(
            field_label(
                "Windows",
                "How many places across the span to look at, chosen where the image "
                "rotates the most. 1 window disables the clock-drift estimate and "
                "keeps drift fixed at 1.0.",
            ),
            self.windows_spin,
        )
        form.addRow(
            field_label(
                "Window length",
                "How much video each window uses. Longer windows correlate more "
                "reliably but take longer -- the sample footage needed 40 s to find a "
                "confident peak.",
            ),
            self.window_length_spin,
        )
        box.addLayout(form)

        self.advanced_box = QGroupBox("Advanced: limit the search range")
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Restrict which log delays the search may return, in log time. Useful if "
            "you already know roughly where in the log the clip falls -- 'Probe' "
            "reports the valid range."
        )
        self.search_min_spin = QDoubleSpinBox()
        self.search_min_spin.setRange(0.0, 1e6)
        self.search_min_spin.setSuffix(" s (log time)")
        self.search_max_spin = QDoubleSpinBox()
        self.search_max_spin.setRange(0.0, 1e6)
        self.search_max_spin.setSuffix(" s (log time)")
        advanced_form = compact_form()
        advanced_form.addRow(
            field_label("Earliest log time", "The estimated log delay will never be lower than this."),
            self.search_min_spin,
        )
        advanced_form.addRow(
            field_label("Latest log time", "The estimated log delay will never be higher than this."),
            self.search_max_spin,
        )
        self.advanced_box.setLayout(advanced_form)
        box.addWidget(self.advanced_box)

        self.run_button = primary_button("Run auto align")
        self.run_button.clicked.connect(self._run)
        run_row = QHBoxLayout()
        run_row.addWidget(self.run_button)
        run_row.addStretch(1)
        box.addLayout(run_row)
        return group

    def _build_alignment_group(self) -> QGroupBox:
        group = QGroupBox("Current alignment")
        box = QVBoxLayout(group)
        box.setSpacing(8)

        self.log_delay_spin = QDoubleSpinBox()
        self.log_delay_spin.setRange(-1e6, 1e6)
        self.log_delay_spin.setDecimals(4)
        self.log_delay_spin.setSingleStep(0.1)
        self.log_delay_spin.setSuffix(" s")
        self.log_delay_spin.valueChanged.connect(self._on_log_delay_edited)

        self.drift_spin = QDoubleSpinBox()
        self.drift_spin.setRange(0.5, 2.0)
        self.drift_spin.setDecimals(6)
        self.drift_spin.setSingleStep(0.0001)
        self.drift_spin.setValue(1.0)
        self.drift_spin.valueChanged.connect(self._on_drift_edited)

        # A bare factor like 1.001467 is impossible to judge; the translation next
        # to it turns it into something with a felt size.
        self.drift_hint = QLabel()
        self.drift_hint.setStyleSheet("QLabel { color: palette(mid); font-size: 11px; }")
        drift_row = QWidget()
        drift_layout = QHBoxLayout(drift_row)
        drift_layout.setContentsMargins(0, 0, 0, 0)
        drift_layout.setSpacing(8)
        drift_layout.addWidget(self.drift_spin)
        drift_layout.addWidget(self.drift_hint)
        drift_layout.addStretch(1)

        form = compact_form()
        form.addRow(
            field_label(
                "Log delay",
                "Where the log sits relative to the video: "
                "log time = log delay + video time × clock drift. Drag the plot below, "
                "or type an exact value here.",
            ),
            self.log_delay_spin,
        )
        form.addRow(
            field_label(
                "Clock drift",
                "How much faster the autopilot clock runs than the camera's. "
                "1.000000 means they agree; real drift is a few parts in a thousand "
                "and only becomes visible over a long flight.",
            ),
            drift_row,
        )
        box.addLayout(form)
        return group

    def _build_result_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("resultCard")
        card.setStyleSheet(
            "QFrame#resultCard { background: palette(alternate-base); "
            "border: 1px solid palette(mid); border-radius: 6px; }"
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 1)

        self.save_button = primary_button("Save alignment")
        self.save_button.setToolTip(
            "Write the current alignment to this video's sync.json. Running the search "
            "or dragging the plot changes the alignment in this session only -- this is "
            "the only thing that puts it on disk."
        )
        self.save_button.clicked.connect(self._save_alignment)
        layout.addWidget(self.save_button)
        return card

    def _build_plot_headers(self) -> QHBoxLayout:
        headers = QHBoxLayout()

        left = QHBoxLayout()
        left.addWidget(section_label("Roll rate — drag to align"))
        left.addWidget(help_icon(_PLOT_HELP))
        left.addStretch(1)
        # These act on the plot, so they belong with it rather than among the
        # parameters, where they left a half-empty box.
        self.reset_view_button = QPushButton("Reset view")
        self.reset_view_button.setEnabled(False)
        self.reset_view_button.clicked.connect(self._reset_view)
        self.save_png_button = QPushButton("Save diagnostic PNG")
        self.save_png_button.setEnabled(False)
        self.save_png_button.setToolTip(
            "Save a snapshot of the current alignment to this video's cache directory, "
            "for comparison or documentation -- the live plot is not a file."
        )
        self.save_png_button.clicked.connect(self._save_plot)
        left.addWidget(self.reset_view_button)
        left.addWidget(self.save_png_button)

        right = QHBoxLayout()
        right.addWidget(section_label("Window fit"))
        right.addWidget(help_icon(_FIT_HELP))
        right.addStretch(1)

        headers.addLayout(left, 2)
        headers.addLayout(right, 1)
        return headers

    def _build_plots(self) -> QHBoxLayout:
        plots = QHBoxLayout()
        plots.setSpacing(12)

        self.plot = ManualSyncPlot()
        self.plot.delta_dragged.connect(self._on_delta_dragged)
        plots.addWidget(self.plot, 2)

        self.fit_pane = ZoomableImageView(
            "Fit plot appears here after a run with more than one window."
        )
        self.fit_pane.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        plots.addWidget(self.fit_pane, 1)
        return plots

    # ---- reacting to the shared project state -----------------------------

    def _on_state_changed(self) -> None:
        c = self.controller
        ready = c.ready_for_commands
        # setEnabled(True) on the tab does NOT re-enable a child that was disabled
        # explicitly -- the buttons manage their own state, here and in _run().
        self.setEnabled(ready)
        self.run_button.setEnabled(ready and self._worker is None)
        self.save_button.setEnabled(ready and c.has_alignment and not c.sync_is_saved)

        if ready and c.info is not None:
            self.start_spin.setRange(0.0, c.info.duration)
            self.end_spin.setRange(0.0, c.info.duration)
        if not self._suppress_range_feedback:
            for spin, value in ((self.start_spin, c.range_start), (self.end_spin, c.range_end)):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

        if not self._suppress_sync_feedback:
            self.log_delay_spin.blockSignals(True)
            self.log_delay_spin.setValue(c.sync.log_delay)
            self.log_delay_spin.blockSignals(False)
            self.drift_spin.blockSignals(True)
            self.drift_spin.setValue(c.sync.scale)
            self.drift_spin.blockSignals(False)
            self.plot.update_sync(c.sync.log_delay, c.sync.scale)
        self._update_drift_hint()

        if c.video != self._auto_populate_video:
            # A different video: the previous analysis and result belonged to it.
            self._auto_populate_video = c.video
            self._diagnostics = None
            self._result = None
            self.reset_view_button.setEnabled(False)
            self.save_png_button.setEnabled(False)
            self.plot.clear()
            self.fit_pane.clear_image()
        self._refresh_status()
        self._maybe_auto_populate()

    def _update_drift_hint(self) -> None:
        self.drift_hint.setText(format_drift(self.drift_spin.value()))

    def _refresh_status(self) -> None:
        """The card's line: what the search found, and whether it is on disk."""
        c = self.controller
        if self._worker is not None:
            return  # the running message stays until the worker finishes
        self.status_label.setStyleSheet("")
        if not c.ready_for_commands:
            self.status_label.setText("Load a video and a log to align them.")
            return
        if not c.has_alignment:
            self.status_label.setText(
                "Not aligned yet. Run the automatic search, or drag the plot once it "
                "has data."
            )
            return
        if self._result is not None:
            verdict = (
                "looks trustworthy" if self._result.trustworthy else "check it by eye"
            )
            saved = (
                "saved to sync.json"
                if c.sync_is_saved
                else "<b>not saved yet</b> — press 'Save alignment' to keep it"
            )
            self.status_label.setText(
                f"Auto align found this alignment — {verdict}. Drag the orange trace "
                f"to correct it; {saved}."
            )
        elif c.sync_is_saved:
            # Loaded from the video's sync file at startup, not produced this session.
            self.status_label.setText(
                "Alignment loaded from sync.json. Run the search or drag the plot to "
                "change it."
            )
        else:
            self.status_label.setText(
                "Alignment changed and <b>not saved yet</b> — press 'Save alignment' "
                "to keep it."
            )

    def _on_range_edited(self, _value: float) -> None:
        self._suppress_range_feedback = True
        self.controller.set_range(self.start_spin.value(), self.end_spin.value())
        self._suppress_range_feedback = False

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._maybe_auto_populate()

    def _maybe_auto_populate(self) -> None:
        """Run automatically if this exact span's optical flow is already cached.

        Cheap: reading the cache header is a small file read, and a run that hits
        the cache entirely finishes in milliseconds -- no different from the user
        having clicked "Run auto align" themselves. Tried once per loaded video.
        """
        c = self.controller
        if self._worker is not None or self._diagnostics is not None:
            return
        if not self.isVisible() or not c.ready_for_commands or c.info is None:
            return
        start, end = self.start_spin.value(), self.end_spin.value()
        if end <= start:
            return
        if not flow_is_cached(c.video, c.info, start, end):
            return
        self._run(auto=True)

    # ---- running -----------------------------------------------------------

    def _run(self, *, auto: bool = False) -> None:
        c = self.controller
        if c.video is None or c.log is None:
            return
        # Never start a second command on top of a running one. The button is
        # disabled while a worker is alive, but _maybe_auto_populate() calls this
        # directly, and two concurrent commands would nest
        # terminal.redirect_to_terminal's process-global sys.stdout swap -- the
        # inner one restores the outer one's redirector, leaving stdout pointing
        # at the terminal widget for the rest of the session.
        if self._worker is not None:
            return
        start, end = self.start_spin.value(), self.end_spin.value()
        if end <= start:
            self.status_label.setText("'To' must be after 'From'.")
            return

        # Per-video, under cache/: the plots are regenerated on every run and
        # belong to this clip, so they live and die with its other derived files.
        self._plot_dir = ensure_cache_dir_for(c.video) / "plots"
        self._plot_dir.mkdir(parents=True, exist_ok=True)
        args = argparse.Namespace(
            video=c.video,
            log=c.log,
            start=start,
            end=end,
            windows=self.windows_spin.value(),
            window_length=self.window_length_spin.value(),
            search_min=self.search_min_spin.value() if self.advanced_box.isChecked() else None,
            search_max=self.search_max_spin.value() if self.advanced_box.isChecked() else None,
            write=False,  # only the Save alignment button writes sync.json
            plot=self._plot_dir,
        )
        result_sink: list = []

        self.run_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status_label.setStyleSheet("font-weight: bold; color: palette(link);")
        self.status_label.setText("Searching...")
        self.fit_pane.clear_image()
        self.terminal.write(
            "[align] found a cached analysis, loading it automatically\n"
            if auto
            else "[align] automatic search started\n"
        )

        self._worker = CommandWorker(
            lambda: cmd_autosync(args, result_sink=result_sink), self.terminal, self
        )
        self._worker.succeeded.connect(lambda _rc: self._on_finished(result_sink))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._on_state_changed()

    def _on_finished(self, result_sink: list) -> None:
        self.status_label.setStyleSheet("")
        if not result_sink:
            self.status_label.setText("The search did not produce a result (see terminal).")
            return
        result = result_sink[0]
        self._result = result

        # Show the result: it becomes the working alignment straight away, so the
        # plot, the preview and the export all reflect what is on screen. It is
        # still not on disk -- that needs "Save alignment".
        self.controller.set_sync(
            SyncModel(log_delay=result.log_delay, scale=result.scale, note="autosync")
        )

        if result.diagnostics is not None:
            self._diagnostics = result.diagnostics
            self.plot.set_diagnostics(result.diagnostics, result.log_delay, result.scale)
            self.reset_view_button.setEnabled(True)
            self.save_png_button.setEnabled(True)

        if self._plot_dir is not None:
            fit_path = self._plot_dir / "autosync_fit.png"
            if fit_path.exists():
                self.fit_pane.show_image(fit_path)
        self.terminal.write(
            f"[align] showing log delay {result.log_delay:.3f}s, drift {result.scale:.6f} "
            "-- press 'Save alignment' to write it to sync.json\n"
        )

    def _on_failed(self, message: str) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"Automatic search failed: {message}")

    # ---- interaction --------------------------------------------------------

    def _on_delta_dragged(self, delta: float) -> None:
        # The plot already moved its own trace; avoid feeding the resulting
        # state_changed back into a redundant update_sync() call.
        self._suppress_sync_feedback = True
        self.controller.nudge_log_delay(delta)
        self._suppress_sync_feedback = False
        self.log_delay_spin.blockSignals(True)
        self.log_delay_spin.setValue(self.controller.sync.log_delay)
        self.log_delay_spin.blockSignals(False)

    def _on_log_delay_edited(self, value: float) -> None:
        c = self.controller
        if value == c.sync.log_delay:
            return
        c.set_sync(SyncModel(log_delay=value, scale=c.sync.scale, note=c.sync.note))

    def _on_drift_edited(self, value: float) -> None:
        self._update_drift_hint()
        c = self.controller
        if value == c.sync.scale:
            return
        c.set_sync(SyncModel(log_delay=c.sync.log_delay, scale=value, note=c.sync.note))

    def _save_alignment(self) -> None:
        try:
            path = self.controller.save_sync()
        except Exception as exc:  # noqa: BLE001 - reported inline, not fatal
            self.status_label.setText(f"Could not save the alignment: {exc}")
            return
        self.terminal.write(f"[align] saved {path}\n")

    def _reset_view(self) -> None:
        self.plot.reset_view()

    def _save_plot(self) -> None:
        if self._diagnostics is None:
            return
        c = self.controller
        plot_dir = ensure_cache_dir_for(c.video) / "plots"
        plot_dir.mkdir(parents=True, exist_ok=True)
        path = save_diagnostic_plots(
            self._diagnostics,
            c.sync.log_delay,
            plot_dir,
            scale=c.sync.scale,
            filename="align_diagnostics.png",
            xlim=(c.sync.log_time(self.start_spin.value()), c.sync.log_time(self.end_spin.value())),
        )
        self.terminal.write(f"[align] saved {path}\n")
