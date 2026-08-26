"""Background frame decoding + overlay compositing for the preview's scrubbing.

Scrubbing is random-access seeking, not sequential reading (see the note on
``FrameReader``), so a single distant seek can take a moment; decoding must not
run on the GUI thread. It also must not run on a ``QThread`` -- see the trap
documented in ``workers.py`` and ``CLAUDE.md`` about PyAV crashing under
``QThread`` on Windows -- so this uses a plain ``threading.Thread`` with a
single-slot mailbox: only the most recent scrub position is ever decoded, and
any request superseded while a decode is already in flight is simply dropped.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPainter

from ..hud.renderer import OverlayRenderer
from ..telemetry.timeline import Timeline
from ..video.probe import VideoInfo
from ..video.reader import FrameReader


class FrameDecodeWorker(QObject):
    """Owns one ``FrameReader`` and decodes/composites frames off the GUI thread."""

    frame_ready = Signal(QImage, float)
    failed = Signal(str)

    def __init__(self, video: Path, info: VideoInfo, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._video = video
        self._info = info
        self._lock = threading.Lock()
        self._pending: tuple[float, OverlayRenderer, Timeline, tuple[int, int]] | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def request(
        self,
        video_time: float,
        renderer: OverlayRenderer,
        timeline: Timeline,
        render_size: tuple[int, int],
    ) -> None:
        """Ask for a frame at ``video_time``; supersedes any not-yet-started request.

        ``render_size`` must match ``renderer``'s own ``(width, height)`` -- it is
        the size the decoded frame is rescaled to (via PyAV's own swscale-based
        ``reformat()``, not opencv, which this project reserves for autosync) before
        the overlay is painted on top, so the two always align pixel-for-pixel. A
        size smaller than the source is the "preview quality" control: it cannot
        speed up the decode itself, but it cuts every step after it (colour
        conversion, compositing, and the final smooth-scale to the widget).
        """
        with self._lock:
            self._pending = (video_time, renderer, timeline, render_size)
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    # ---- worker thread ---------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None:
                continue
            video_time, renderer, timeline, render_size = job
            # A fresh container per request, not one reused across the worker's
            # whole lifetime: reusing one PyAV container across many repeated
            # `seek()` calls, with the Qt event loop active on another thread,
            # reliably escalates into multi-second stalls and eventually
            # indefinite hangs on Windows -- reproduced with nothing more than a
            # burst of scrub requests, no widgets involved. Opening (and
            # discarding) a new container per request costs perhaps a few tens
            # of milliseconds and completely avoids it -- see the trap in
            # CLAUDE.md. Do not "optimise" this back to a shared reader.
            reader = FrameReader(self._video, self._info)
            try:
                image, actual_time = self._decode(
                    reader, renderer, timeline, video_time, render_size
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
                self.failed.emit(str(exc))
                continue
            finally:
                reader.close()
            self.frame_ready.emit(image, actual_time)

    def _decode(
        self,
        reader: FrameReader,
        renderer: OverlayRenderer,
        timeline: Timeline,
        video_time: float,
        render_size: tuple[int, int],
    ) -> tuple[QImage, float]:
        index = self._info.frame_index_at(video_time)
        frame = reader.frame_at_index(index)
        width, height = render_size
        if (width, height) != (frame.width, frame.height):
            frame = frame.reformat(width=width, height=height, format="rgb24")
        rgb = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
        # RGB888 (3 bytes/px) -> RGBA8888 (4 bytes/px) always reallocates, so the
        # result owns its own memory: no risk of outliving `rgb`'s buffer.
        base = QImage(
            rgb.data, width, height, width * 3, QImage.Format.Format_RGB888,
        ).convertToFormat(QImage.Format.Format_RGBA8888)

        state = timeline.state(min(index, len(timeline) - 1))
        overlay = renderer.render_frame(state)
        painter = QPainter(base)
        try:
            painter.drawImage(0, 0, overlay)
        finally:
            painter.end()
        return base, float(state.video_time)
