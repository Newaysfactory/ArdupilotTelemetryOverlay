"""Random-access frame reading, for the preview and single-frame renders."""

from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .probe import VideoInfo, probe_video

log = logging.getLogger(__name__)


class FrameReader:
    """Decodes individual frames by index, seeking to the nearest keyframe.

    Sequential reads are the fast path (the decoder just keeps going); a backwards or
    distant jump triggers a seek. Good enough for scrubbing 4K, which decodes at
    several times realtime.
    """

    def __init__(self, path: str | Path, info: VideoInfo | None = None) -> None:
        self.path = Path(path)
        self.info = info or probe_video(self.path)
        self._container = av.open(str(self.path))
        self._stream = self._container.streams.video[0]
        self._stream.thread_type = "AUTO"
        self._iterator = None
        self._next_index: int | None = None

    def close(self) -> None:
        self._container.close()

    def __enter__(self) -> FrameReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- internals ---------------------------------------------------------

    def _time_to_index(self, seconds: float) -> int:
        return self.info.frame_index_at(seconds)

    def _seek(self, index: int) -> None:
        target = int(
            round(index / float(self.info.frame_rate) / float(self._stream.time_base))
        )
        self._container.seek(target, stream=self._stream, backward=True, any_frame=False)
        self._iterator = self._container.decode(video=0)
        self._next_index = None

    # ---- public ------------------------------------------------------------

    def frame_at_index(self, index: int) -> av.VideoFrame:
        """Decode the frame with the given index."""
        index = max(0, min(self.info.frame_count - 1, int(index)))
        if self._iterator is None or self._next_index is None or index < self._next_index:
            self._seek(index)
        for frame in self._iterator:  # type: ignore[union-attr]
            frame_index = self._frame_index(frame)
            self._next_index = frame_index + 1
            if frame_index >= index:
                return frame
        raise IndexError(f"frame {index} is past the end of {self.path.name}")

    def frame_at_time(self, seconds: float) -> av.VideoFrame:
        return self.frame_at_index(self._time_to_index(seconds))

    def rgb_at_time(self, seconds: float) -> np.ndarray:
        """Frame as an ``(h, w, 3)`` uint8 RGB array."""
        return self.frame_at_time(seconds).to_ndarray(format="rgb24")

    def _frame_index(self, frame: av.VideoFrame) -> int:
        if frame.pts is None:
            return 0
        time_base = self._stream.time_base or Fraction(1, 1000)
        return int(round(float(frame.pts * time_base) * float(self.info.frame_rate)))
