"""Central, shared state: the video/log/preset/sync a session is working on.

Deliberately small. There is no layout or theme editor in this phase, so the
only state that actually needs to flow between tabs is the loaded video/log,
the current :class:`SyncModel`, and the From/To video-time range -- the
align, preview and export tabs all read and write the same
``sync``/``range_start``/``range_end``.

The alignment has two levels, and the distinction is deliberate: ``sync`` is the
working value (auto align writes it, dragging the plot writes it, typing in the
fields writes it) while ``_saved_sync`` is what is actually on disk in
``sync.json``. Only :meth:`save_sync` moves the first onto the second, so an
experiment is never persisted behind the user's back -- and
:attr:`sync_is_saved` lets the window show, from every tab, whether there is
unsaved alignment work.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..cli import DEFAULT_PRESET
from ..sync import SyncModel
from ..telemetry.model import TelemetryLog
from ..video.probe import VideoInfo, probe_video


class ProjectController(QObject):
    """Holds the currently loaded video/log/preset/sync; emits on any change."""

    state_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.video: Path | None = None
        self.log: Path | None = None
        self.preset_path: Path = DEFAULT_PRESET
        self.info: VideoInfo | None = None
        self.telemetry: TelemetryLog | None = None
        self.sync: SyncModel = SyncModel()
        #: The alignment as it exists in ``sync.json``, or ``None`` when the video has
        #: no sync file yet. Compared against ``sync`` to tell whether there is unsaved
        #: work; only :meth:`save_sync` updates it.
        self._saved_sync: SyncModel | None = None
        #: Shared From/To video-time range: the align tab's analysis window and the
        #: export tab's trim window, plus the two extra handles on the preview tab's
        #: slider. Reset to the whole video whenever a new video is loaded.
        self.range_start: float = 0.0
        self.range_end: float = 0.0

    @property
    def ready_for_probe(self) -> bool:
        return self.video is not None

    @property
    def ready_for_commands(self) -> bool:
        """Video, log and telemetry all loaded -- autosync/manualsync/export need this."""
        return self.video is not None and self.log is not None and self.telemetry is not None

    def set_video(self, path: Path) -> None:
        self.video = Path(path)
        self.info = probe_video(self.video)
        existing = SyncModel.load_for(self.video)
        self.sync = existing if existing is not None else SyncModel()
        # A freshly loaded file starts out saved by definition; with no file, there
        # is nothing on disk to be in sync with.
        self._saved_sync = replace(existing) if existing is not None else None
        self.range_start = 0.0
        self.range_end = self.info.duration
        self.state_changed.emit()

    def set_log_path(self, path: Path) -> None:
        """Record the chosen log path before the (slow) parse has finished."""
        self.log = Path(path)
        self.state_changed.emit()

    def set_telemetry(self, telemetry: TelemetryLog) -> None:
        """Called once :func:`read_log` has finished parsing ``self.log``."""
        self.telemetry = telemetry
        self.state_changed.emit()

    def set_preset_path(self, path: Path) -> None:
        self.preset_path = Path(path)
        self.state_changed.emit()

    def set_sync(self, sync: SyncModel) -> None:
        self.sync = sync
        self.state_changed.emit()

    def set_range(self, start: float, end: float) -> None:
        """Update the shared From/To video-time range (order not enforced here --
        callers clamp before calling, since the meaning of an inverted range
        depends on which handle is being dragged)."""
        self.range_start = start
        self.range_end = end
        self.state_changed.emit()

    # ---- alignment persistence ---------------------------------------------

    @property
    def has_alignment(self) -> bool:
        """Whether an alignment has been established at all.

        A default :class:`SyncModel` (delay 0, scale 1) on a video with no sync
        file means "not aligned yet", not "aligned to zero" -- the two are
        indistinguishable by value, so the presence of a saved file settles it.
        """
        if self._saved_sync is not None:
            return True
        return self.sync.log_delay != 0.0 or self.sync.scale != 1.0

    @property
    def sync_is_saved(self) -> bool:
        """Whether the working alignment matches the one in ``sync.json``.

        Only ``log_delay``/``scale`` are compared: they are what every other
        module reads. The timestamps and the note change on every save and would
        make an otherwise identical alignment look dirty.
        """
        saved = self._saved_sync
        if saved is None:
            return False
        return saved.log_delay == self.sync.log_delay and saved.scale == self.sync.scale

    def save_sync(self) -> Path:
        """Write the working alignment to this video's ``sync.json``.

        The only place the GUI persists an alignment: auto align and the plot
        drag both stop at :meth:`set_sync`, so nothing reaches disk without an
        explicit action.
        """
        if self.video is None:
            raise RuntimeError("no video loaded")
        path = self.sync.save(SyncModel.path_for(self.video))
        self._saved_sync = replace(self.sync)
        self.state_changed.emit()
        return path

    def nudge_log_delay(self, delta: float) -> None:
        """Used by the manualsync plot's live drag; bypasses a full new SyncModel."""
        self.sync = SyncModel(
            log_delay=self.sync.log_delay + delta,
            scale=self.sync.scale,
            note=self.sync.note,
        )
        self.state_changed.emit()
