"""Mapping between video time and log time.

The camera clock cannot be trusted (on the sample footage the MP4 creation time is off
by more than a year), so the alignment is a user-supplied log delay rather than
something derived from metadata.

``log_time = log_delay + video_time * scale``

The user is not expected to compute ``log_delay`` by hand: they point at one moment
they can recognise both in the video and in the log (e.g. takeoff) and give its two
timestamps, ``anchor_video_time`` and ``anchor_log_time``. ``log_delay`` is derived from
that pair (see :meth:`SyncModel.from_anchors`) and stays the quantity every other module
works with; the anchor pair is kept alongside it purely so the sync file remains
readable and re-editable by a human.

``scale`` exists for clock drift between the camera and the flight controller. It is
1.0 in practice for clips of a few minutes; it is kept in the model and in the file so
a future drift-correction tool needs no format change.

The sync lives in its own small file per flight, deliberately separate from the preset:
a preset describes a *look* and is reused across flights, a log delay belongs to one
video/log pair.

It is stored in the video's ``cache/`` directory to keep the user's media folders
clean, but it is **not** cache: it holds alignment work done by hand and cannot be
recomputed, so ``cache.clear_cache_for`` preserves it (see ``cache.PRESERVED_NAMES``).
Files written by earlier versions, which sat next to the video as ``<video>.sync.json``,
are still read from there if the new location has nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .cache import cache_dir_for, ensure_cache_dir_for

SYNC_SUFFIX = ".sync.json"
SYNC_FILENAME = "sync.json"


@dataclass
class SyncModel:
    """Time alignment between a video file and a telemetry log."""

    log_delay: float = 0.0
    scale: float = 1.0
    #: The event's timestamps, kept only for readability/re-editing; not read by
    #: ``log_time``/``video_time``, which use ``log_delay`` directly. ``None`` when
    #: ``log_delay`` was given directly instead of derived from an anchor pair.
    anchor_video_time: float | None = None
    anchor_log_time: float | None = None
    #: Free-form note, e.g. how the log delay was found ("autosync", "manual anchor").
    note: str = ""
    #: When this sync file was first written, ISO 8601 with the local UTC offset.
    #: Preserved across re-saves -- it dates the alignment work, not the last write.
    created: str = ""
    #: When it was last written, same format. Rewritten on every save: this is the
    #: one to look at when asking "is this alignment still the one I was working on?".
    updated: str = ""
    source: Path | None = field(default=None, compare=False)

    @classmethod
    def from_anchors(
        cls,
        anchor_video_time: float,
        anchor_log_time: float,
        *,
        scale: float = 1.0,
        note: str = "",
    ) -> SyncModel:
        """Build a sync from one moment's timestamp in the video and in the log.

        ``anchor_video_time``/``anchor_log_time`` are the same real-world instant (e.g.
        the start of the takeoff roll) read off the video and the log respectively; the
        user picks it by eye instead of subtracting the two by hand.
        """
        return cls(
            log_delay=anchor_log_time - anchor_video_time * scale,
            scale=scale,
            anchor_video_time=anchor_video_time,
            anchor_log_time=anchor_log_time,
            note=note,
        )

    def log_time(self, video_time: float) -> float:
        return self.log_delay + video_time * self.scale

    def video_time(self, log_time: float) -> float:
        return (log_time - self.log_delay) / self.scale

    # ---- persistence -------------------------------------------------------

    @staticmethod
    def path_for(video: Path) -> Path:
        """This video's sync file, in its ``cache/`` directory."""
        return ensure_cache_dir_for(video) / SYNC_FILENAME

    @staticmethod
    def legacy_path_for(video: Path) -> Path:
        """Where versions before the ``cache/`` layout kept it: next to the video."""
        return Path(video).with_suffix(SYNC_SUFFIX)

    @classmethod
    def load(cls, path: Path) -> SyncModel:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        anchor_video_time = data.get("anchor_video_time")
        anchor_log_time = data.get("anchor_log_time")
        return cls(
            log_delay=float(data["log_delay"]),
            scale=float(data.get("scale", 1.0)),
            anchor_video_time=None if anchor_video_time is None else float(anchor_video_time),
            anchor_log_time=None if anchor_log_time is None else float(anchor_log_time),
            note=str(data.get("note", "")),
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
            source=Path(path),
        )

    @classmethod
    def load_for(cls, video: Path) -> SyncModel | None:
        """Load the sync file for a video, or ``None`` if there is none.

        Falls back to the pre-``cache/`` location so an existing alignment is not
        silently lost; the next save moves it to the new one.
        """
        video = Path(video)
        path = cache_dir_for(video) / SYNC_FILENAME
        if path.exists():
            return cls.load(path)
        legacy = cls.legacy_path_for(video)
        return cls.load(legacy) if legacy.exists() else None

    def save(self, path: Path) -> Path:
        path = Path(path)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        # Keep the original creation stamp when overwriting: it dates when this
        # alignment was first established, not when it was last written out. The GUI
        # rebuilds a fresh SyncModel on every drag, so the stamp has to be recovered
        # from the file on disk rather than assumed to be on the object.
        created = self.created
        if not created and path.exists():
            try:
                created = str(json.loads(path.read_text(encoding="utf-8")).get("created", ""))
            except (OSError, ValueError):
                created = ""
        self.created = created or now
        self.updated = now

        payload = {
            "log_delay": self.log_delay,
            "scale": self.scale,
            "note": self.note,
            "created": self.created,
            "updated": self.updated,
        }
        if self.anchor_video_time is not None and self.anchor_log_time is not None:
            payload["anchor_video_time"] = self.anchor_video_time
            payload["anchor_log_time"] = self.anchor_log_time
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self.source = path
        return path
