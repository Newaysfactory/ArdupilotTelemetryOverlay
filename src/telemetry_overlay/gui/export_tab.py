"""Export tab: burns the overlay into a video file.

Calls ``export_video()`` directly rather than going through ``cmd_export`` --
``cli.py``'s ``export`` subcommand has a ``dest`` collision between ``--scale``
(frame downscale) and ``--time-scale`` (sync clock-drift scale), both landing on
``args.scale``. Building ``ExportOptions``/``SyncModel`` explicitly here sidesteps
that parser entirely instead of working around it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..cli import _progress_bar
from ..hud.preset import Preset
from ..units import format_duration
from ..video.encoders import available_encoders
from .controller import ProjectController
from .terminal import TerminalWidget
from .widgets import field_label, help_icon
from .workers import CommandWorker


class ExportTab(QWidget):
    """Burns the current preset/sync into a video file, in the background."""

    def __init__(
        self, controller: ProjectController, terminal: TerminalWidget, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.terminal = terminal
        self._worker: CommandWorker | None = None
        #: Guards against feeding a range change straight back into the field that
        #: just produced it -- same pattern as manualsync_tab's log-delay drag.
        self._suppress_range_feedback = False

        self.output_field = QLineEdit()
        output_browse = QPushButton("Browse...")
        output_browse.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_field, 1)
        output_row.addWidget(output_browse)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 1e6)
        self.start_spin.setSuffix(" s")
        self.start_spin.valueChanged.connect(self._on_range_edited)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 1e6)
        self.end_spin.setSuffix(" s")
        self.end_spin.valueChanged.connect(self._on_range_edited)

        self.encoder_combo = QComboBox()
        self.encoder_combo.addItem("Automatic (best available)", None)

        self.quality_spin = QDoubleSpinBox()
        self.quality_spin.setRange(0.0, 51.0)
        self.quality_spin.setDecimals(0)
        self.quality_spin.setSpecialValueText("encoder default")
        self.quality_spin.setValue(0.0)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.1, 1.0)
        self.scale_spin.setDecimals(2)
        self.scale_spin.setSingleStep(0.1)
        self.scale_spin.setValue(1.0)

        self.audio_check = QCheckBox("Copy audio")
        self.audio_check.setChecked(True)
        audio_row = QHBoxLayout()
        audio_row.addWidget(self.audio_check)
        audio_row.addWidget(
            help_icon("Copies the source audio track packet-for-packet, never re-encoded. Unticking omits audio entirely.")
        )
        audio_row.addStretch(1)

        self.overwrite_check = QCheckBox("Overwrite if it exists")
        overwrite_row = QHBoxLayout()
        overwrite_row.addWidget(self.overwrite_check)
        overwrite_row.addWidget(
            help_icon("If unticked, exporting to a file that already exists fails instead of replacing it.")
        )
        overwrite_row.addStretch(1)

        form = QFormLayout()
        form.addRow(
            field_label("Output", "Where the rendered video is written. Defaults to out/<video>.overlay.mp4."),
            output_row,
        )
        form.addRow(
            field_label(
                "From",
                "Video time (seconds) where the export starts. Shared with the "
                "Preview tab's slider and every other tab's From/To.",
            ),
            self.start_spin,
        )
        form.addRow(
            field_label(
                "To",
                "Video time (seconds) where the export ends. Shared with the "
                "Preview tab's slider and every other tab's From/To.",
            ),
            self.end_spin,
        )
        form.addRow(
            field_label(
                "Encoder",
                "Which video encoder to use. 'Automatic' prefers hardware (NVENC) "
                "and falls back to software (x264) when no compatible GPU is available.",
            ),
            self.encoder_combo,
        )
        form.addRow(
            field_label(
                "Quality",
                "Constant-quality setting (lower is better/larger: 18-23 is typically "
                "visually lossless). 'Encoder default' picks a sensible value per encoder.",
            ),
            self.quality_spin,
        )
        form.addRow(
            field_label(
                "Scale",
                "Downscale factor applied to the output frame, e.g. 0.5 for half "
                "resolution. Meant for a fast draft export, not the final delivery.",
            ),
            self.scale_spin,
        )
        form.addRow("", audio_row)
        form.addRow("", overwrite_row)

        self.run_button = QPushButton("Export")
        self.run_button.clicked.connect(self._run)

        self.status_label = QLabel("No export yet.")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.run_button)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

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
        if ready and c.info is not None:
            if not self.output_field.text():
                self.output_field.setText(str(Path("out") / (c.video.stem + ".overlay.mp4")))
            self.start_spin.setRange(0.0, c.info.duration)
            self.end_spin.setRange(0.0, c.info.duration)
            self._refresh_encoders()
        if not self._suppress_range_feedback:
            self.start_spin.blockSignals(True)
            self.start_spin.setValue(c.range_start)
            self.start_spin.blockSignals(False)
            self.end_spin.blockSignals(True)
            self.end_spin.setValue(c.range_end)
            self.end_spin.blockSignals(False)

    def _on_range_edited(self, _value: float) -> None:
        self._suppress_range_feedback = True
        self.controller.set_range(self.start_spin.value(), self.end_spin.value())
        self._suppress_range_feedback = False

    def _refresh_encoders(self) -> None:
        if self.encoder_combo.count() > 1:
            return  # populated once; available_encoders() probes hardware, not cheap
        for spec in available_encoders():
            self.encoder_combo.addItem(spec.description, spec.key)

    # ---- running -------------------------------------------------------

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to", self.output_field.text(), "MP4 video (*.mp4)"
        )
        if path:
            self.output_field.setText(path)

    def _run(self) -> None:
        from ..sync import SyncModel
        from ..video.export import ExportOptions, export_video

        c = self.controller
        if c.video is None or c.telemetry is None or c.info is None:
            return
        output = Path(self.output_field.text())
        if output.exists() and not self.overwrite_check.isChecked():
            self.status_label.setText(f"{output} already exists -- tick 'Overwrite' to replace it.")
            return

        preset = Preset.load(c.preset_path)
        sync = c.sync
        options = ExportOptions(
            encoder=self.encoder_combo.currentData(),
            quality=int(self.quality_spin.value()) or None,
            scale=self.scale_spin.value(),
            start=self.start_spin.value(),
            end=self.end_spin.value(),
            copy_audio=self.audio_check.isChecked(),
            overwrite=self.overwrite_check.isChecked(),
        )

        self.run_button.setEnabled(False)
        self.status_label.setStyleSheet("font-weight: bold; color: palette(link);")
        self.status_label.setText("Exporting...")
        self.terminal.write(f"[export] {c.video.name} + {c.log.name} -> {output}\n")

        def run_export():
            return export_video(
                c.video,
                c.telemetry,
                preset,
                sync,
                output,
                options,
                info=c.info,
                progress=lambda p: _progress_bar(
                    f"  {p.frames_done}/{p.frames_total}",
                    p.fraction,
                    suffix=f"{p.fps:5.1f} fps  eta {format_duration(p.eta)}",
                ),
            )

        self._worker = CommandWorker(run_export, self.terminal, self)
        self._worker.succeeded.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._on_state_changed()

    def _on_finished(self, result) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(
            f"Done: {result.frames} frames in {result.elapsed:.1f}s ({result.fps:.1f} fps) "
            f"with {result.encoder}. Audio "
            f"{'copied without re-encoding' if result.audio_copied else 'not written'}, "
            f"bands covered {result.band_coverage * 100:.1f}% of each frame."
        )
        self.terminal.write(
            f"\ndone: {result.frames} frames in {result.elapsed:.1f}s "
            f"({result.fps:.1f} fps) with {result.encoder}\n"
            f"  audio {'copied without re-encoding' if result.audio_copied else 'not written'}"
            f"   overlay bands covered {result.band_coverage * 100:.1f}% of each frame\n"
            f"  wrote {result.path} ({result.path.stat().st_size / 1e6:.1f} MB)\n"
        )

    def _on_failed(self, message: str) -> None:
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"Export failed: {message}")
