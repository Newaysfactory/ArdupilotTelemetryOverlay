"""Mapping between video time and log time.

The camera clock cannot be trusted (on the sample footage the MP4 creation time is off
by more than a year), so the alignment is a user-supplied log delay rather than
something derived from metadata.

``log_time = log_delay + video_time * scale``

``scale`` exists for clock drift between the camera and the flight controller. It is
1.0 in practice for clips of a few minutes; it is kept in the model and in the file so
a future drift-correction tool needs no format change.

The sync lives in its own small file per flight, deliberately separate from the preset:
a preset describes a *look* and is reused across flights, a log delay belongs to one
video/log pair.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SYNC_SUFFIX = ".sync.json"


@dataclass
class SyncModel:
    """Time alignment between a video file and a telemetry log."""

    log_delay: float = 0.0
    scale: float = 1.0
    #: Free-form note, e.g. how the log delay was found ("autosync", "manual anchor").
    note: str = ""
    source: Path | None = field(default=None, compare=False)

    def log_time(self, video_time: float) -> float:
        return self.log_delay + video_time * self.scale

    def video_time(self, log_time: float) -> float:
        return (log_time - self.log_delay) / self.scale

    # ---- persistence -------------------------------------------------------

    @staticmethod
    def path_for(video: Path) -> Path:
        """Companion sync file next to the video: ``clip.MP4`` -> ``clip.sync.json``."""
        return video.with_suffix(SYNC_SUFFIX)

    @classmethod
    def load(cls, path: Path) -> SyncModel:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            log_delay=float(data["log_delay"]),
            scale=float(data.get("scale", 1.0)),
            note=str(data.get("note", "")),
            source=Path(path),
        )

    @classmethod
    def load_for(cls, video: Path) -> SyncModel | None:
        """Load the companion sync file for a video, or ``None`` if absent."""
        path = cls.path_for(Path(video))
        return cls.load(path) if path.exists() else None

    def save(self, path: Path) -> Path:
        path = Path(path)
        payload = {"log_delay": self.log_delay, "scale": self.scale, "note": self.note}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.source = path
        return path
