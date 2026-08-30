"""Main window: drag&drop loading, the Probe action, tabs, and the terminal."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import AUTHOR, LICENSE, PROJECT_URL, __version__
from ..cache import cache_dir_for, clear_cache_for
from ..cli import DEFAULT_PRESET, cmd_probe
from ..telemetry import read_log
from .align_tab import AlignTab
from .controller import ProjectController
from .export_tab import ExportTab
from .preview_tab import PreviewTab
from .project_header import ProjectHeader
from .terminal import TerminalPane
from .workers import CommandWorker

log = logging.getLogger(__name__)

#: Recognised video container extensions for drag&drop file-type sniffing.
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
LOG_SUFFIXES = {".bin"}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # The title bar is what a user quotes back in a screenshot, so the
        # version belongs here as well as in the header chip.
        self.setWindowTitle(f"ArduPilot Telemetry Overlay {__version__}")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self.controller = ProjectController(self)
        self.terminal_pane = TerminalPane()
        self.terminal = self.terminal_pane.terminal
        self.terminal_pane.maximizeToggled.connect(self._toggle_terminal_maximized)
        self._terminal_maximized = False
        self._workers: list[CommandWorker] = []

        self._build_central_widget()

        # First line in the terminal, so it also ends up in any output a user
        # copies out when reporting a problem: which build, and where it came from.
        self.terminal.write(
            f"ArduPilot Telemetry Overlay v{__version__} — {AUTHOR} — "
            f"{LICENSE} — {PROJECT_URL}\n"
        )
        self.header.start_help_link_lookup()

    # ---- layout --------------------------------------------------------

    def _build_central_widget(self) -> None:
        # The header replaces the old toolbar: it carries the same three file
        # pickers plus Probe/Clear cache, and adds the alignment state that used
        # to be visible only from inside the tab that produced it.
        self.header = ProjectHeader(self.controller)
        self.header.browse_video_requested.connect(self._browse_video)
        self.header.browse_log_requested.connect(self._browse_log)
        self.header.browse_preset_requested.connect(self._browse_preset)
        self.header.probe_requested.connect(self._run_probe)
        self.header.clear_cache_requested.connect(self._clear_cache)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # Numbered, and named for the task rather than the CLI subcommand: the
        # order is the order the work actually happens in.
        self.tabs = QTabWidget()
        self.align_tab = AlignTab(self.controller, self.terminal)
        self.tabs.addTab(self.align_tab, "1 · Align")
        self.preview_tab = PreviewTab(self.controller)
        self.tabs.addTab(self.preview_tab, "2 · Preview")
        self.export_tab = ExportTab(self.controller, self.terminal)
        self.tabs.addTab(self.export_tab, "3 · Export")
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.terminal_pane)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        self._splitter = splitter

        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.header)
        outer.addWidget(splitter, 1)
        self.setCentralWidget(wrapper)

    def _toggle_terminal_maximized(self) -> None:
        """Double-click on the terminal title bar: fill the tab area with it.

        The title bar's own label always says how to get back ("double-click to
        restore" while maximised), since hiding the tabs alone gives the user no
        other clue that the terminal isn't just always this big.
        """
        self._terminal_maximized = not self._terminal_maximized
        self.tabs.setVisible(not self._terminal_maximized)
        self.terminal_pane.set_maximized(self._terminal_maximized)

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
        self.terminal.write(f"[video] loading {path.name}...\n")
        QApplication.processEvents()
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

    def _clear_cache(self) -> None:
        """Wipe the derived files for the loaded video/log, keeping the sync."""
        targets = [p for p in (self.controller.video, self.controller.log) if p]
        if not targets:
            QMessageBox.information(self, "Clear cache", "Load a video or a log first.")
            return

        listing = "\n".join(f"  {cache_dir_for(p)}" for p in targets)
        confirm = QMessageBox.question(
            self,
            "Clear cache",
            "Delete the computed files in:\n\n"
            f"{listing}\n\n"
            "The optical flow analysis and the parsed telemetry will be recomputed on "
            "the next run. Your sync (log delay) is kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        removed = clear_cache_for(*targets)
        if removed:
            for path in removed:
                self.terminal.write(f"[cache] removed {path}\n")
            self.terminal.write(f"[cache] cleared {len(removed)} item(s)\n")
        else:
            self.terminal.write("[cache] nothing to clear\n")

    # ---- helpers ---------------------------------------------------------

    def _track_worker(self, worker: CommandWorker) -> None:
        """Keep a reference alive until it finishes (an unowned worker would be GC'd)."""
        self._workers.append(worker)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
