"""Main window: drag&drop loading, the Probe action, tabs, and the terminal."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..cli import DEFAULT_PRESET, cmd_probe
from ..telemetry import read_log
from .autosync_tab import AutosyncTab
from .controller import ProjectController
from .export_tab import ExportTab
from .manualsync_tab import ManualsyncTab
from .preview_tab import PreviewTab
from .terminal import TerminalWidget
from .workers import CommandWorker

log = logging.getLogger(__name__)

#: Recognised video container extensions for drag&drop file-type sniffing.
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
LOG_SUFFIXES = {".bin"}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ArduPilot Telemetry Overlay")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self.controller = ProjectController(self)
        self.terminal = TerminalWidget()
        self._workers: list[CommandWorker] = []

        self._build_toolbar()
        self._build_central_widget()
        self.controller.state_changed.connect(self._refresh_paths)
        self._refresh_paths()

    # ---- layout --------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Project")
        bar.setMovable(False)
        self.addToolBar(bar)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)

        self.video_field = self._path_row(layout, "Video:", self._browse_video)
        self.log_field = self._path_row(layout, "Log:", self._browse_log)
        self.preset_field = self._path_row(layout, "Preset:", self._browse_preset)

        layout.addStretch(1)
        self.probe_button = QPushButton("Probe")
        self.probe_button.setToolTip(
            "Analyse the video and log and print the report to the terminal below "
            "(equivalent to 'telemetry-overlay probe')"
        )
        self.probe_button.clicked.connect(self._run_probe)
        layout.addWidget(self.probe_button)

        bar.addWidget(container)

    def _path_row(self, layout: QHBoxLayout, label: str, on_browse) -> QLineEdit:
        layout.addWidget(QLabel(label))
        field = QLineEdit()
        field.setReadOnly(True)
        field.setMinimumWidth(220)
        layout.addWidget(field)
        button = QPushButton("Browse...")
        button.clicked.connect(on_browse)
        layout.addWidget(button)
        return field

    def _build_central_widget(self) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tabs = QTabWidget()
        self.preview_tab = PreviewTab(self.controller)
        self.tabs.addTab(self.preview_tab, "Preview")
        self.autosync_tab = AutosyncTab(self.controller, self.terminal)
        self.tabs.addTab(self.autosync_tab, "Autosync")
        self.manualsync_tab = ManualsyncTab(self.controller, self.terminal)
        self.tabs.addTab(self.manualsync_tab, "Manualsync")
        self.export_tab = ExportTab(self.controller, self.terminal)
        self.tabs.addTab(self.export_tab, "Export")
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.terminal)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)
        self.setCentralWidget(wrapper)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.preview_tab.shutdown()
        super().closeEvent(event)

    # ---- drag&drop -------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in VIDEO_SUFFIXES:
                self.load_video(path)
            elif suffix in LOG_SUFFIXES:
                self.load_log(path)
            else:
                self.terminal.write(f"[drag&drop] unrecognised file type: {path.name}\n")
        event.acceptProposedAction()

    # ---- loading -----------------------------------------------------------

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", "", "Video (*.mp4 *.mov *.avi *.mkv *.m4v)"
        )
        if path:
            self.load_video(Path(path))

    def _browse_log(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open log", "", "DataFlash log (*.bin)"
        )
        if path:
            self.load_log(Path(path))

    def _browse_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open preset", str(DEFAULT_PRESET.parent), "Preset (*.json)"
        )
        if path:
            self.controller.set_preset_path(Path(path))

    def load_video(self, path: Path) -> None:
        try:
            self.controller.set_video(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user, not swallowed
            QMessageBox.critical(self, "Could not open video", str(exc))
            return
        self.terminal.write(f"[video] loaded {path.name}\n")

    def load_log(self, path: Path) -> None:
        self.controller.set_log_path(path)
        self.terminal.write(f"[log] parsing {path.name}...\n")

        def parse() -> object:
            return read_log(path, progress=lambda msg: print(f"  {msg}"))

        worker = CommandWorker(parse, self.terminal, self)
        worker.succeeded.connect(self._on_log_parsed)
        worker.failed.connect(
            lambda message: QMessageBox.critical(self, "Could not read log", message)
        )
        self._track_worker(worker)
        worker.start()

    def _on_log_parsed(self, telemetry: object) -> None:
        self.controller.set_telemetry(telemetry)  # type: ignore[arg-type]
        self.terminal.write("[log] ready\n")

    # ---- commands ------------------------------------------------------

    def _run_probe(self) -> None:
        if not self.controller.ready_for_probe:
            QMessageBox.information(self, "Probe", "Load a video first.")
            return
        args = argparse.Namespace(video=self.controller.video, log=self.controller.log)
        self.terminal.write("[probe] started\n")
        worker = CommandWorker(lambda: cmd_probe(args), self.terminal, self)
        self._track_worker(worker)
        worker.start()

    # ---- helpers ---------------------------------------------------------

    def _track_worker(self, worker: CommandWorker) -> None:
        """Keep a reference alive until it finishes (an unowned worker would be GC'd)."""
        self._workers.append(worker)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)

    def _refresh_paths(self) -> None:
        self.video_field.setText(str(self.controller.video) if self.controller.video else "")
        self.log_field.setText(str(self.controller.log) if self.controller.log else "")
        self.preset_field.setText(str(self.controller.preset_path))
