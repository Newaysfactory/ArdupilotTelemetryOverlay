"""Inspect a video container.

Uses PyAV rather than shelling out to ffprobe: PyAV ships a complete FFmpeg build, so
the tool has no external binary to install.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


@dataclass(frozen=True)
class VideoInfo:
    """Container and stream properties needed to plan an overlay render."""

    path: Path
    width: int
    height: int
    #: Exact frame rate as a fraction. Never rounded to a float: on a long clip a
    #: 29.97-vs-30 confusion drifts the telemetry visibly against the picture.
    frame_rate: Fraction
    frame_count: int
    duration: float
    time_base: Fraction
    codec: str
    pix_fmt: str
    color_range: int
    colorspace: int
    rotation: int
    has_audio: bool
    audio_codec: str | None
    audio_rate: int | None
    creation_time: dt.datetime | None

    @property
    def fps(self) -> float:
        return float(self.frame_rate)

    @property
    def frame_interval(self) -> float:
        return 1.0 / float(self.frame_rate)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def frame_times(self) -> np.ndarray:
        """Nominal presentation time of every frame, in seconds."""
        return np.arange(self.frame_count, dtype=np.float64) * self.frame_interval

    def frame_index_at(self, video_time: float) -> int:
        """Index of the frame displayed at ``video_time``, clamped to the clip."""
        idx = int(round(video_time * float(self.frame_rate)))
        return max(0, min(self.frame_count - 1, idx))


def probe_video(path: str | Path) -> VideoInfo:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError(f"{path} contains no video stream")
        stream = container.streams.video[0]
        ctx = stream.codec_context

        rate = stream.average_rate or stream.base_rate or Fraction(30, 1)
        time_base = stream.time_base or Fraction(1, 1000)
        duration = _duration(container, stream, time_base)
        frame_count = int(stream.frames) or int(round(duration * float(rate)))

        audio = container.streams.audio[0] if container.streams.audio else None

        return VideoInfo(
            path=path,
            width=int(stream.width),
            height=int(stream.height),
            frame_rate=Fraction(rate),
            frame_count=frame_count,
            duration=duration,
            time_base=Fraction(time_base),
            codec=ctx.name,
            pix_fmt=str(ctx.pix_fmt),
            color_range=int(ctx.color_range or 0),
            colorspace=int(ctx.colorspace or 0),
            rotation=_rotation(stream),
            has_audio=audio is not None,
            audio_codec=audio.codec_context.name if audio else None,
            audio_rate=int(audio.rate) if audio else None,
            creation_time=_creation_time(container),
        )


def _duration(container: av.container.InputContainer, stream, time_base) -> float:
    if stream.duration is not None:
        return float(stream.duration * time_base)
    if container.duration is not None:
        return float(container.duration / av.time_base)
    return 0.0


def _rotation(stream) -> int:
    """Display rotation in degrees, from the display matrix or the legacy tag."""
    tag = (stream.metadata or {}).get("rotate")
    if tag:
        try:
            return int(float(tag)) % 360
        except ValueError:
            pass
    for data in getattr(stream, "side_data", None) or ():
        rotation = getattr(data, "rotation", None)
        if rotation is not None:
            return int(round(-float(rotation))) % 360
    return 0


def _creation_time(container: av.container.InputContainer) -> dt.datetime | None:
    """Container creation time.

    Worth reporting but not worth trusting for synchronisation: action cameras
    routinely record a wrong clock (the sample clip is stamped over a year off), which
    is why the log delay is user-supplied.
    """
    raw = (container.metadata or {}).get("creation_time")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
