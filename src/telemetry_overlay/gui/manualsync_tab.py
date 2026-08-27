"""Manualsync tab: an interactive roll-rate plot to align video and log by eye.

Unlike autosync's suggestion (never applied on its own), the drag here *is* the
sync action -- it writes to ``controller.sync`` live, the same way the user would
directly edit the number if the plot didn't exist.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..autosync import compute_manual_diagnostics, save_diagnostic_plots
from ..cli import _progress_bar
from ..sync import SyncModel
from .controller import ProjectController
from .manual_sync_plot import ManualSyncPlot
from .terminal import TerminalWidget
from .widgets import field_label, help_icon
from .workers import CommandWorker

#: Where the GUI asks manualsync to save a diagnostic snapshot, if requested.
_PLOT_DIR = Path("out") / "gui" / "manualsync"

_PLOT_HELP = (
    "[Left-drag] slide (video) trace to align"
    "[Wheel scroll] zoom"
    "[Right-drag] scale the vertical axis"
)


class ManualsyncTab(QWidget):
    """Analyses a video slice and shows it as a draggable/zoomable roll-rate plot."""

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
        self._suppress_sync_feedback = False

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 1e6)
        self.start_spin.setSuffix(" s")

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 1e6)
        self.end_spin.setSuffix(" s")

        self.log_delay_spin = QDoubleSpinBox()
        self.log_delay_spin.setRange(-1e6, 1e6)
        self.log_delay_spin.setDecimals(4)
        self.log_delay_spin.setSingleStep(0.1)
        self.log_delay_spin.setSuffix(" s")
        self.log_delay_spin.valueChanged.connect(self._on_log_delay_edited)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.5, 2.0)
        self.scale_spin.setDecimals(5)
        self.scale_spin.setSingleStep(0.001)
        self.scale_spin.setValue(1.0)
        self.scale_spin.valueChanged.connect(self._on_scale_edited)

        form = QFormLayout()
        form.addRow(
            field_label("From", "Video time (seconds) where the analysed slice starts."),
            self.start_spin,
        )
        form.addRow(
            field_label("To", "Video time (seconds) where the analysed slice ends."),
            self.end_spin,
        )
        form.addRow(
            field_label(
                "Log delay",
                "log_time = log_delay + video_time * scale. Same value the plot drag "
                "sets -- type an exact number here, or use the arrows to nudge it "
                "0.1s at a time.",
            ),
            self.log_delay_spin,
        )
        form.addRow(
            field_label(
                "Scale",
                "Clock-drift factor: log_time = log_delay + video_time * scale. Usually "
                "very close to 1.0; drag the plot to set the log delay, edit this only to "
                "correct a drift visible across a long slice.",
            ),
            self.scale_spin,
        )

        self.run_button = QPushButton("Analyse")
        self.run_button.clicked.connect(self._run)

        self.status_label = QLabel("No analysis yet.")

        self.reset_view_button = QPushButton("Reset view")
        self.reset_view_button.setEnabled(False)
        self.reset_view_button.clicked.connect(self._reset_view)

        self.save_button = QPushButton("Save diagnostic PNG")
        self.save_button.setEnabled(False)
        self.save_button.setToolTip(
            "Saves a snapshot of the current alignment to out/gui/manualsync/, "
            "for comparison or documentation -- the live plot itself is not a file."
        )
        self.save_button.clicked.connect(self._save_plot)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.status_label, 1)
        buttons_row.addWidget(self.reset_view_button)
        buttons_row.addWidget(self.save_button)

        plot_header = QHBoxLayout()
        plot_header.addWidget(QLabel("Roll rate"))
        plot_header.addWidget(help_icon(_PLOT_HELP))
        plot_header.addStretch(1)

        self.plot = ManualSyncPlot()
        self.plot.delta_dragged.connect(self._on_delta_dragged)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.run_button)
        layout.addLayout(buttons_row)
        layout.addLayout(plot_header)
        layout.addWidget(self.plot, 1)

        self.setEnabled(False)
        controller.state_changed.connect(self._on_state_changed)

    # ---- reacting to the shared project state -----------------------------

    def _on_state_changed(self) -> None:
        c = self.controller
        ready = c.video is not None and c.log is not None and c.telemetry is not None
        # setEnabled(True) on the tab does not re-enable a child disabled explicitly
        # elsewhere -- run_button is managed on its own, in _run()/_on_worker_done().
        self.setEnabled(ready)
        self.run_button.setEnabled(ready and self._worker is None)
        if ready and self.end_spin.value() == 0.0 and c.info is not None:
            self.start_spin.setValue(0.0)
            self.end_spin.setValue(c.info.duration)

        if not self._suppress_sync_feedback:
            self.log_delay_spin.blockSignals(True)
            self.log_delay_spin.setValue(c.sync.log_delay)
            self.log_delay_spin.blockSignals(False)
            self.scale_spin.blockSignals(True)
            self.scale_spin.setValue(c.sync.scale)
            self.scale_spin.blockSignals(False)
            self.plot.update_sync(c.sync.log_delay, c.sync.scale)

    # ---- running -------------------------------------------------------

    def _run(self) -> None:
        c = self.controller
        if c.video is None or c.telemetry is None:
            return
        start = self.start_spin.value()
        end = self.end_spin.value()
        if end <= start:
            self.status_label.setText("'To' must be after 'From'.")
            return

        self.run_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status_label.setStyleSheet("font-weight: bold; color: palette(link);")
        self.status_label.setText("Analysing...")
        self.terminal.write("[manualsync] started\n")

        def analyse():
            return compute_manual_diagnostics(
                c.video,
                c.telemetry,
                start,
                end,
                info=c.info,
                progress=lambda f: _progress_bar("  tracking", f),
            )

        self._worker = CommandWorker(analyse, self.terminal, self)
        self._worker.succeeded.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._on_state_changed()

    def _on_finished(self, diagnostics) -> None:
        self._diagnostics = diagnostics
        c = self.controller
        self.plot.set_diagnostics(diagnostics, c.sync.log_delay, c.sync.scale)
        self.reset_view_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.status_label.setStyleSheet("")
        self.status_label.setText("Ready -- drag the orange trace to align it.")
        self.terminal.write("[manualsync] ready -- drag the orange trace to align it\n")

    def _on_failed(self, message: str) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"Analysis failed: {message}")

    # ---- interaction -----------------------------------------------------

    def _on_delta_dragged(self, delta: float) -> None:
        # The plot already moved its own trace; avoid feeding the resulting
        # state_changed straight back into a redundant update_sync() call.
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

    def _on_scale_edited(self, value: float) -> None:
        c = self.controller
        if value == c.sync.scale:
            return
        c.set_sync(SyncModel(log_delay=c.sync.log_delay, scale=value, note=c.sync.note))

    def _reset_view(self) -> None:
        self.plot.reset_view()

    def _save_plot(self) -> None:
        if self._diagnostics is None:
            return
        c = self.controller
        _PLOT_DIR.mkdir(parents=True, exist_ok=True)
        start = self.start_spin.value()
        end = self.end_spin.value()
        path = save_diagnostic_plots(
            self._diagnostics,
            c.sync.log_delay,
            _PLOT_DIR,
            scale=c.sync.scale,
            filename="manualsync_diagnostics.png",
            xlim=(c.sync.log_time(start), c.sync.log_time(end)),
        )
        self.terminal.write(f"[manualsync] saved {path}\n")
