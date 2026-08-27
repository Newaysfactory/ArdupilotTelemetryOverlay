"""Autosync tab: runs `autosync` in the background and shows its diagnostic plots."""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..cli import cmd_autosync
from ..sync import SyncModel
from .controller import ProjectController
from .terminal import TerminalWidget
from .widgets import field_label
from .workers import CommandWorker
from .zoomable_image_view import ZoomableImageView

#: Where the GUI asks autosync to save its diagnostic plots.
_PLOT_DIR = Path("out") / "gui" / "autosync"


class AutosyncTab(QWidget):
    """Suggests a log delay/scale by correlating image roll with logged roll."""

    def __init__(
        self, controller: ProjectController, terminal: TerminalWidget, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.terminal = terminal
        self._worker: CommandWorker | None = None
        self._last_result = None

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 1e6)
        self.start_spin.setSuffix(" s")
        self.start_spin.setValue(0.0)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 1e6)
        self.end_spin.setSuffix(" s")
        self.end_spin.setSpecialValueText("end of video")
        self.end_spin.setValue(0.0)

        self.windows_spin = QSpinBox()
        self.windows_spin.setRange(1, 50)
        self.windows_spin.setValue(6)

        self.window_length_spin = QDoubleSpinBox()
        self.window_length_spin.setRange(1.0, 300.0)
        self.window_length_spin.setSuffix(" s")
        self.window_length_spin.setValue(20.0)

        form = QFormLayout()
        form.addRow(
            field_label("From", "Video time (seconds) where the analysis span starts."),
            self.start_spin,
        )
        form.addRow(
            field_label(
                "To",
                "Video time (seconds) where the analysis span ends. Leave at "
                "'end of video' to use the whole rest of the clip.",
            ),
            self.end_spin,
        )
        form.addRow(
            field_label(
                "Windows",
                "Number of analysis windows spread across the span, placed where the "
                "image rotates the most. 1 window disables the clock-drift (scale) "
                "estimate and keeps scale fixed at 1.0.",
            ),
            self.windows_spin,
        )
        form.addRow(
            field_label(
                "Window length",
                "Duration of each analysis window. Longer windows correlate more "
                "reliably but take longer to analyse -- the sample footage needed "
                "40s to find a confident peak.",
            ),
            self.window_length_spin,
        )

        self.advanced_box = QGroupBox("Advanced: limit the search range")
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Restrict which log delays autosync may return, in log time. Useful "
            "if you already know roughly where in the log the clip falls -- "
            "'probe' reports the valid range."
        )
        self.search_min_spin = QDoubleSpinBox()
        self.search_min_spin.setRange(0.0, 1e6)
        self.search_min_spin.setSuffix(" s (log time)")
        self.search_max_spin = QDoubleSpinBox()
        self.search_max_spin.setRange(0.0, 1e6)
        self.search_max_spin.setSuffix(" s (log time)")
        advanced_form = QFormLayout()
        advanced_form.addRow(
            field_label("Earliest log time", "The estimated log delay will never be lower than this."),
            self.search_min_spin,
        )
        advanced_form.addRow(
            field_label("Latest log time", "The estimated log delay will never be higher than this."),
            self.search_max_spin,
        )
        self.advanced_box.setLayout(advanced_form)

        self.run_button = QPushButton("Run autosync")
        self.run_button.clicked.connect(self._run)

        self.result_label = QLabel("No result yet.")
        self.result_label.setWordWrap(True)

        self.use_button = QPushButton("Use this result")
        self.use_button.setEnabled(False)
        self.use_button.setToolTip(
            "Copies the estimated log delay and scale into the shared sync -- "
            "never applied automatically."
        )
        self.use_button.clicked.connect(self._use_result)

        self.diagnostics_pane = ZoomableImageView("Diagnostics plot appears here after a run.")
        self.fit_pane = ZoomableImageView("Fit plot appears here (only with more than one window).")
        images = QHBoxLayout()
        images.addWidget(self.diagnostics_pane, 1)
        images.addWidget(self.fit_pane, 1)

        result_row = QHBoxLayout()
        result_row.addWidget(self.result_label, 1)
        result_row.addWidget(self.use_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.advanced_box)
        layout.addWidget(self.run_button)
        layout.addLayout(result_row)
        layout.addLayout(images, 1)

        self.setEnabled(False)
        controller.state_changed.connect(self._on_state_changed)

    # ---- reacting to the shared project state -----------------------------

    def _on_state_changed(self) -> None:
        c = self.controller
        ready = c.video is not None and c.log is not None and c.telemetry is not None
        # setEnabled(True) on the tab does NOT re-enable a child that was
        # explicitly disabled (its own enabled flag stays false regardless of the
        # parent's) -- run_button's enabled state is managed separately, in
        # _run()/_on_worker_done(), not here.
        self.setEnabled(ready)
        self.run_button.setEnabled(ready and self._worker is None)
        if ready and self.end_spin.value() == 0.0 and c.info is not None:
            self.end_spin.setValue(c.info.duration)

    # ---- running -------------------------------------------------------

    def _run(self) -> None:
        c = self.controller
        if c.video is None or c.log is None:
            return
        _PLOT_DIR.mkdir(parents=True, exist_ok=True)
        args = argparse.Namespace(
            video=c.video,
            log=c.log,
            start=self.start_spin.value(),
            end=self.end_spin.value() or None,
            windows=self.windows_spin.value(),
            window_length=self.window_length_spin.value(),
            search_min=self.search_min_spin.value() if self.advanced_box.isChecked() else None,
            search_max=self.search_max_spin.value() if self.advanced_box.isChecked() else None,
            write=False,  # the GUI never writes .sync.json on its own
            plot=_PLOT_DIR,
        )
        result_sink: list = []
        self.run_button.setEnabled(False)
        self.use_button.setEnabled(False)
        self.result_label.setText("Running...")
        self.diagnostics_pane.clear_image()
        self.fit_pane.clear_image()
        self.terminal.write("[autosync] started\n")

        self._worker = CommandWorker(lambda: cmd_autosync(args, result_sink=result_sink), self.terminal, self)
        self._worker.succeeded.connect(lambda _rc: self._on_finished(result_sink))
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._on_state_changed()

    def _on_finished(self, result_sink: list) -> None:
        if not result_sink:
            self.result_label.setText("Autosync did not produce a result (see terminal).")
            return
        result = result_sink[0]
        self._last_result = result
        verdict = "looks trustworthy" if result.trustworthy else "check it by eye"
        self.result_label.setText(
            f"log delay {result.log_delay:.3f}s   scale {result.scale:.5f}   {verdict}"
        )
        self.use_button.setEnabled(True)

        diagnostics_path = _PLOT_DIR / "autosync_diagnostics.png"
        if diagnostics_path.exists():
            self.diagnostics_pane.show_image(diagnostics_path)
        fit_path = _PLOT_DIR / "autosync_fit.png"
        if fit_path.exists():
            self.fit_pane.show_image(fit_path)

    def _on_failed(self, message: str) -> None:
        self.result_label.setText(f"Autosync failed: {message}")

    def _use_result(self) -> None:
        if self._last_result is None:
            return
        self.controller.set_sync(
            SyncModel(
                log_delay=self._last_result.log_delay,
                scale=self._last_result.scale,
                note="autosync",
            )
        )
        self.terminal.write(
            f"[autosync] applied log delay {self._last_result.log_delay:.3f}s, "
            f"scale {self._last_result.scale:.5f}\n"
        )
