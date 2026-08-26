"""Central, shared state: the video/log/preset/sync a session is working on.

Deliberately small. There is no layout or theme editor in this phase, so the
only state that actually needs to flow between tabs is the loaded video/log
and the current :class:`SyncModel` -- the preview, the manualsync plot and the
export tab all read and write the same ``sync``.
"""

from __future__ import annotations

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

    def nudge_log_delay(self, delta: float) -> None:
        """Used by the manualsync plot's live drag; bypasses a full new SyncModel."""
        self.sync = SyncModel(
            log_delay=self.sync.log_delay + delta,
            scale=self.sync.scale,
            note=self.sync.note,
        )
        self.state_changed.emit()
