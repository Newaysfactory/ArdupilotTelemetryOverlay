"""Preview tab: scrubbing through the video with the overlay composited live."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..hud.preset import Preset
from ..hud.renderer import OverlayRenderer
from ..telemetry.events import MessageTrack
from ..telemetry.timeline import build_timeline
from ..video.export import _display_size, _scaled_size  # shared on purpose
from .controller import ProjectController
from .frame_decode_worker import FrameDecodeWorker
from .range_slider import RangeSlider
from .widgets import field_label

#: How long to wait, after the last slider movement, before decoding.
_DEBOUNCE_MS = 60
#: Preview quality choices: label -> downscale factor applied before compositing.
#: Cuts colour conversion, painting and the final smooth-scale to the widget; it
#: cannot speed up the PyAV decode itself (that happens at source resolution
#: regardless -- there is no cheap way to decode H.264 at reduced size without a
#: full filter graph), so this mainly helps on a slow display/paint path, not a
#: slow seek.
_QUALITY_LEVELS: list[tuple[str, float]] = [
    ("Full", 1.0),
    ("1/2", 0.5),
    ("1/4", 0.25),
]


class _ImageLabel(QLabel):
    """Shows a QImage scaled to fit, re-scaling on resize."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background-color: #202020;")
        self._source: QImage | None = None

    def set_image(self, image: QImage) -> None:
        self._source = image
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._source is None:
            return
        pixmap = QPixmap.fromImage(self._source).scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pixmap)


class PreviewTab(QWidget):
    """Overlay preview with a debounced scrub slider and an exact-time field."""

    def __init__(self, controller: ProjectController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._decode_worker: FrameDecodeWorker | None = None
        self._renderer: OverlayRenderer | None = None
        self._timeline = None
        self._render_size: tuple[int, int] | None = None
        self._current_preset: Preset | None = None
        self._duration = 0.0
        self._updating_controls = False
        #: Guards against feeding a range change straight back into the slider
        #: that just produced it -- same pattern as manualsync_tab's log-delay drag.
        self._suppress_range_feedback = False

        self.image_label = _ImageLabel()

        self.slider = RangeSlider()
        self.slider.positionChanged.connect(self._on_slider_position_changed)
        self.slider.positionCommitted.connect(self._request_frame_now)
        self.slider.rangeChanged.connect(self._on_slider_range_dragged)

        self.time_spin = QDoubleSpinBox()
        self.time_spin.setDecimals(2)
        self.time_spin.setSuffix(" s")
        self.time_spin.valueChanged.connect(self._on_spin_changed)

        self.range_label = QLabel("From 0.00s  To 0.00s")

        self.quality_combo = QComboBox()
        for label, _scale in _QUALITY_LEVELS:
            self.quality_combo.addItem(label)
        self.quality_combo.setToolTip(
            "Preview resolution: lower is faster to composite and display, but "
            "does not speed up decoding the video itself."
        )
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)

        self.status_label = QLabel("Load a video and a log to preview the overlay.")

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._request_frame_now)

        slider_header = QHBoxLayout()
        slider_header.addWidget(
            field_label(
                "Timeline",
                "The needle scrubs the preview frame; the two round handles set the "
                "From/To range shared with the Autosync/Manualsync/Export tabs. Click "
                "empty groove to jump the needle there; drag any handle to move it.",
            )
        )
        slider_header.addWidget(self.range_label)
        slider_header.addStretch(1)

        controls = QHBoxLayout()
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_spin)
        controls.addWidget(QLabel("Quality:"))
        controls.addWidget(self.quality_combo)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.image_label, 1)
        layout.addLayout(slider_header)
        layout.addLayout(controls)

        self.setEnabled(False)
        controller.state_changed.connect(self._on_state_changed)

    # ---- lifecycle -------------------------------------------------------

    def shutdown(self) -> None:
        if self._decode_worker is not None:
            self._decode_worker.stop()
            self._decode_worker = None

    # ---- reacting to the shared project state -----------------------------

    def _on_state_changed(self) -> None:
        c = self.controller
        if c.video is None or c.info is None or c.telemetry is None:
            self.setEnabled(False)
            self.status_label.setText("Load a video and a log to preview the overlay.")
            return

        try:
            preset = Preset.load(c.preset_path)
        except Exception as exc:  # noqa: BLE001 - shown inline, not fatal
            self.setEnabled(False)
            self.status_label.setText(f"Could not load preset: {exc}")
            return

        self._current_preset = preset
        self._rebuild_renderer()

        video_times = c.info.frame_times()
        log_times = c.sync.log_delay + video_times * c.sync.scale
        self._timeline = build_timeline(
            c.telemetry,
            video_times,
            log_times,
            options=preset.sampling,
            message_track=MessageTrack.from_log(c.telemetry, preset.messages),
        )

        if self._decode_worker is None:
            self._decode_worker = FrameDecodeWorker(c.video, c.info, self)
            self._decode_worker.frame_ready.connect(self._on_frame_ready)
            self._decode_worker.failed.connect(self._on_decode_failed)

        self._duration = c.info.duration
        self.status_label.setText(f"{c.video.name}  +  {c.log.name if c.log else '(no log)'}")

        self._updating_controls = True
        self.slider.setRange(0.0, self._duration)
        self.time_spin.setRange(0.0, self._duration)
        self._updating_controls = False

        if not self._suppress_range_feedback:
            self.slider.setLowHigh(c.range_start, c.range_end)
            self._update_range_label()

        self.setEnabled(True)
        self._request_frame_now()

    def _rebuild_renderer(self) -> None:
        """(Re)build the renderer at the current preview-quality resolution."""
        info = self.controller.info
        if info is None or self._current_preset is None:
            return
        scale = _QUALITY_LEVELS[self.quality_combo.currentIndex()][1]
        width, height = _scaled_size(_display_size(info), scale)
        self._render_size = (width, height)
        self._renderer = OverlayRenderer(self._current_preset, width, height)

    def _on_quality_changed(self, _index: int) -> None:
        if self._current_preset is None:
            return
        self._rebuild_renderer()
        self._request_frame_now()

    # ---- slider/spin box <-> video time -----------------------------------

    def _video_time(self) -> float:
        return self.slider.position()

    def _on_slider_position_changed(self, value: float) -> None:
        if self._updating_controls:
            return
        self._updating_controls = True
        self.time_spin.setValue(value)
        self._updating_controls = False
        self._debounce.start()

    def _on_spin_changed(self, value: float) -> None:
        if self._updating_controls:
            return
        self._updating_controls = True
        self.slider.setPosition(value)
        self._updating_controls = False
        self._debounce.start()

    def _on_slider_range_dragged(self, low: float, high: float) -> None:
        self._update_range_label(low, high)
        self._suppress_range_feedback = True
        self.controller.set_range(low, high)
        self._suppress_range_feedback = False

    def _update_range_label(self, low: float | None = None, high: float | None = None) -> None:
        low = self.controller.range_start if low is None else low
        high = self.controller.range_end if high is None else high
        self.range_label.setText(f"From {low:.2f}s  To {high:.2f}s")

    # ---- decoding ----------------------------------------------------------

    def _request_frame_now(self) -> None:
        if (
            self._decode_worker is None
            or self._renderer is None
            or self._timeline is None
            or self._render_size is None
        ):
            return
        self._decode_worker.request(
            self._video_time(), self._renderer, self._timeline, self._render_size
        )

    def _on_frame_ready(self, image: QImage, video_time: float) -> None:
        self.image_label.set_image(image)

    def _on_decode_failed(self, message: str) -> None:
        self.status_label.setText(f"Preview error: {message}")
