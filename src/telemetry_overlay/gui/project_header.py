"""The bar above the tabs: what is loaded, and what the project currently is.

Two rows, and the split is the point. The top row is *input* -- the three files
plus the two actions that apply to the whole project. The bottom row is *derived
state*: the alignment, and what the source video is.

The alignment lives here rather than inside a tab because it is the one number
the entire program turns on, and it used to be visible only from the tab that
produced it: from the preview or the export tab you could not tell whether you
were aligned at all, let alone whether the value had been saved. Its three
states (missing, unsaved, saved) are spelled out, not colour-coded only.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import AUTHOR, __version__
from .controller import ProjectController
from .help_links import LATEST_README_URL, resolve_readme_url_async

#: Warm colour for the one action here that destroys something, so it does not
#: read as a peer of the harmless "Probe" next to it.
_DESTRUCTIVE_COLOUR = "#8a4a3a"
_WARN_COLOUR = "#a3651b"


def _chip(key: str, value: str, *, warn: bool = False) -> QWidget:
    """A small uppercase caption with a value under it."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(1)

    caption = QLabel(key.upper())
    caption.setStyleSheet(
        "QLabel { color: palette(mid); font-size: 10px; letter-spacing: 1px; }"
    )
    body = QLabel(value)
    body.setStyleSheet(
        f"QLabel {{ font-weight: 600; color: {_WARN_COLOUR}; }}"
        if warn
        else "QLabel { font-weight: 600; }"
    )
    layout.addWidget(caption)
    layout.addWidget(body)
    return container


class ProjectHeader(QFrame):
    """Always-visible project state, shown above the tab bar."""

    browse_video_requested = Signal()
    browse_log_requested = Signal()
    browse_preset_requested = Signal()
    probe_requested = Signal()
    clear_cache_requested = Signal()
    #: Emitted from the lookup thread; Qt queues it onto the GUI thread.
    _help_url_resolved = Signal(str)

    def __init__(self, controller: ProjectController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            "ProjectHeader { background: palette(alternate-base); "
            "border-bottom: 1px solid palette(mid); }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(8)

        # ---- row 1: the inputs ------------------------------------------
        files = QHBoxLayout()
        files.setSpacing(8)
        self.video_button = self._file_button(self.browse_video_requested)
        self.log_button = self._file_button(self.browse_log_requested)
        self.preset_button = self._file_button(self.browse_preset_requested)
        files.addWidget(self.video_button)
        files.addWidget(self.log_button)
        files.addWidget(self.preset_button)
        files.addStretch(1)

        self.probe_button = QPushButton("Probe")
        self.probe_button.setToolTip(
            "Analyse the video and log and print the report to the terminal below "
            "(equivalent to 'telemetry-overlay probe')"
        )
        self.probe_button.clicked.connect(self.probe_requested)
        files.addWidget(self.probe_button)

        self.clear_cache_button = QPushButton("Clear cache")
        self.clear_cache_button.setStyleSheet(f"QPushButton {{ color: {_DESTRUCTIVE_COLOUR}; }}")
        self.clear_cache_button.setToolTip(
            "Delete everything computed for the current video and log -- the optical "
            "flow analysis, the parsed telemetry and the diagnostic plots -- so the "
            "next run recomputes them from scratch. The alignment is kept: it is work "
            "you did by hand, not something that can be recomputed."
        )
        self.clear_cache_button.clicked.connect(self.clear_cache_requested)
        files.addWidget(self.clear_cache_button)
        outer.addLayout(files)

        # ---- row 2: the derived state -------------------------------------
        # The state row is torn down and rebuilt on every refresh, so the version
        # label sits beside it rather than inside it: it never changes, and it must
        # not be rebuilt (or forgotten) along with the chips.
        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        self.state_row = QHBoxLayout()
        self.state_row.setSpacing(16)
        bottom.addLayout(self.state_row, 1)

        # Version, credit and Help on one quiet line. Help is a plain rich-text
        # anchor with setOpenExternalLinks: Qt opens the system browser itself, so
        # there is no click handler to keep in sync with the URL.
        self._help_url = LATEST_README_URL
        self.footer_label = QLabel()
        self.footer_label.setTextFormat(Qt.TextFormat.RichText)
        self.footer_label.setOpenExternalLinks(True)
        self.footer_label.setToolTip(
            "Version of ArduPilot Telemetry Overlay you are running -- quote it when "
            "reporting a problem, it matches the release tag on GitHub. 'Help' opens "
            "the documentation in your browser."
        )
        self.footer_label.setStyleSheet(
            "QLabel { color: palette(mid); font-size: 11px; }"
        )
        self.footer_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom
        )
        self._refresh_footer()
        bottom.addWidget(self.footer_label)
        outer.addLayout(bottom)

        self._help_url_resolved.connect(self._set_help_url)

        controller.state_changed.connect(self.refresh)
        self.refresh()

    # ---- the footer line ---------------------------------------------------

    def start_help_link_lookup(self) -> None:
        """Upgrade the Help link to this build's tagged README, if that tag exists.

        Called by the main window rather than from ``__init__`` so that merely
        constructing a header never touches the network: the check is a startup
        nicety, not part of building the widget.
        """
        resolve_readme_url_async(self._help_url_resolved.emit)

    def _set_help_url(self, url: str) -> None:
        self._help_url = url
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        self.footer_label.setText(
            f"v{__version__} &nbsp;·&nbsp; by {AUTHOR} &nbsp;·&nbsp; "
            f'<a href="{self._help_url}">Help</a>'
        )

    def _file_button(self, signal: Signal) -> QPushButton:
        button = QPushButton()
        button.setStyleSheet("QPushButton { padding: 5px 10px; text-align: left; }")
        button.clicked.connect(signal)
        return button

    # ---- state ------------------------------------------------------------

    def refresh(self) -> None:
        c = self.controller
        self._set_file_button(self.video_button, "Video", c.video)
        self._set_file_button(self.log_button, "Log", c.log)
        self._set_file_button(self.preset_button, "Preset", c.preset_path)
        self.probe_button.setEnabled(c.ready_for_probe)
        self.clear_cache_button.setEnabled(c.video is not None or c.log is not None)
        self._rebuild_state_row()

    @staticmethod
    def _set_file_button(button: QPushButton, label: str, path: Path | None) -> None:
        name = path.name if path is not None else "choose..."
        button.setText(f"  {label}:  {name}   ▾")
        button.setToolTip(str(path) if path is not None else f"Choose a {label.lower()} file")

    def _rebuild_state_row(self) -> None:
        while self.state_row.count():
            item = self.state_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) before deleteLater(): deletion is deferred to the
                # event loop, and until it runs the old chip is still parented here
                # and still painted -- the new row would be drawn on top of it.
                widget.setParent(None)
                widget.deleteLater()

        c = self.controller
        if c.video is None:
            self.state_row.addWidget(_chip("Project", "drop a video and a log to start"))
            self.state_row.addStretch(1)
            return

        alignment, alignment_is_warning = self._alignment_text()
        self.state_row.addWidget(_chip("Alignment", alignment, warn=alignment_is_warning))
        if c.info is not None:
            self.state_row.addWidget(self._separator())
            self.state_row.addWidget(
                _chip(
                    "Source",
                    f"{c.info.width}×{c.info.height} · "
                    f"{float(c.info.frame_rate):.3f} fps · {c.info.duration:.2f} s",
                )
            )
        self.state_row.addStretch(1)

    def _alignment_text(self) -> tuple[str, bool]:
        """The alignment chip's value and whether it should read as a warning."""
        c = self.controller
        if not c.has_alignment:
            return "!  not aligned yet — run Align", True
        body = f"delay {c.sync.log_delay:.3f} s   ·   drift {c.sync.scale:.6f}"
        if c.sync_is_saved:
            return f"✓  {body}", False
        return f"●  {body}   — not saved", True

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setStyleSheet("color: palette(mid);")
        return line
