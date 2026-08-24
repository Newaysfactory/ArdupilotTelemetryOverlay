"""Burn the overlay into a video file.

Everything happens in one process. PyAV ships a full FFmpeg, so the decode, the
compositing (libavfilter's ``overlay``) and the encode all run in C: Python only draws
the HUD bands, which is a few milliseconds per frame. There is no frame pipe to a
second process, and therefore no 4K bandwidth problem and no way for the overlay to
drift out of step with the picture -- each band frame carries the timestamp of the
video frame it belongs to.

About re-encoding: burning pixels into a picture cannot avoid re-encoding the video
stream, so the aim here is to make it as close to lossless as practical --- constant
quality at a high setting, the original resolution and frame rate, and the audio
stream copied through untouched.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ..hud.preset import Preset
from ..hud.renderer import OverlayRenderer, qimage_to_ndarray
from ..sync import SyncModel
from ..telemetry.events import MessageTrack
from ..telemetry.model import TelemetryLog
from ..telemetry.timeline import build_timeline
from .encoders import EncoderSpec, select_encoder
from .probe import VideoInfo, probe_video

log = logging.getLogger(__name__)


@dataclass
class ExportOptions:
    """Knobs for one export."""

    encoder: str | None = None
    quality: int | None = None
    #: Trim window in video time, in seconds. ``None`` means the whole clip.
    start: float | None = None
    duration: float | None = None
    copy_audio: bool = True
    max_bands: int = 6
    overwrite: bool = False
    #: Extra encoder options, merged last so they can override the defaults.
    encoder_options: dict[str, str] = field(default_factory=dict)


@dataclass
class ExportProgress:
    frames_done: int
    frames_total: int
    elapsed: float

    @property
    def fps(self) -> float:
        return self.frames_done / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def fraction(self) -> float:
        return self.frames_done / self.frames_total if self.frames_total else 1.0

    @property
    def eta(self) -> float:
        rate = self.fps
        remaining = self.frames_total - self.frames_done
        return remaining / rate if rate > 0 else float("nan")


@dataclass
class ExportResult:
    path: Path
    frames: int
    elapsed: float
    encoder: str
    audio_copied: bool
    band_coverage: float

    @property
    def fps(self) -> float:
        return self.frames / self.elapsed if self.elapsed else 0.0


ProgressCallback = Callable[[ExportProgress], None]


def export_video(
    video: str | Path,
    telemetry: TelemetryLog,
    preset: Preset,
    sync: SyncModel,
    output: str | Path,
    options: ExportOptions | None = None,
    *,
    info: VideoInfo | None = None,
    progress: ProgressCallback | None = None,
    progress_interval: float = 0.5,
) -> ExportResult:
    """Render ``video`` with ``telemetry`` overlaid, into ``output``."""
    options = options or ExportOptions()
    video = Path(video)
    output = Path(output)
    if output.exists() and not options.overwrite:
        raise FileExistsError(f"{output} exists; pass overwrite to replace it")
    if output.resolve() == video.resolve():
        raise ValueError("refusing to write the overlay over the source video")
    output.parent.mkdir(parents=True, exist_ok=True)

    info = info or probe_video(video)
    spec = select_encoder(options.encoder)
    first, last = _frame_range(info, options)
    total = last - first + 1
    if total <= 0:
        raise ValueError("the requested trim window contains no frames")

    display_w, display_h = _display_size(info)
    renderer = OverlayRenderer(
        preset, display_w, display_h, max_bands=options.max_bands
    )
    timeline = _build_timeline(info, telemetry, preset, sync)

    log.info(
        "exporting %d frames (%.1fs) at %s with %s, %d overlay bands covering %.1f%%",
        total,
        total * info.frame_interval,
        f"{display_w}x{display_h}",
        spec.codec,
        len(renderer.bands),
        renderer.coverage * 100,
    )

    started = time.monotonic()
    frames_written = 0
    audio_copied = False

    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        graph, video_input, band_inputs = _build_graph(stream, renderer, info)

        with av.open(
            str(output), "w", options={"movflags": "+faststart"}
        ) as out_container:
            out_video = _add_video_stream(
                out_container, spec, info, display_w, display_h, options
            )
            # Captured once, deliberately. Writing the header lets the muxer replace
            # the stream's time_base with its own (mp4 picks 1/15360), so reading it
            # back per frame would stamp later frames in different units than the
            # first -- which corrupts the frame rate and can break dts monotonicity.
            frame_time_base = Fraction(1, 1) / info.frame_rate
            audio: _AudioCopier | None = None
            if options.copy_audio and info.has_audio:
                audio = _AudioCopier(
                    video,
                    out_container,
                    out_container.add_stream_from_template(
                        container.streams.audio[0]
                    ),
                    first * info.frame_interval,
                    (last + 1) * info.frame_interval,
                )

            if first > 0:
                _seek(container, stream, first, info)

            last_report = started
            for frame in container.decode(stream):
                index = _frame_index(frame, stream, info)
                if index < first:
                    continue
                if index > last:
                    break
                state = timeline.state(min(index, len(timeline) - 1))
                _push_bands(band_inputs, renderer, state, frame)
                # Push to the video buffer explicitly: Graph.push() with no target
                # broadcasts to every buffer source, band inputs included.
                video_input.push(frame)
                for composed in _pull_all(graph):
                    composed.pts = frames_written
                    composed.time_base = frame_time_base
                    for packet in out_video.encode(composed):
                        out_container.mux(packet)
                    frames_written += 1
                if audio is not None:
                    audio.advance(index * info.frame_interval)

                now = time.monotonic()
                if progress and now - last_report >= progress_interval:
                    progress(ExportProgress(frames_written, total, now - started))
                    last_report = now

            # Flush the filter graph, then the encoder.
            video_input.push(None)
            for buffer in band_inputs:
                buffer.push(None)
            for composed in _pull_all(graph):
                composed.pts = frames_written
                composed.time_base = frame_time_base
                for packet in out_video.encode(composed):
                    out_container.mux(packet)
                frames_written += 1
            for packet in out_video.encode():
                out_container.mux(packet)

            if audio is not None:
                audio.close()
                audio_copied = audio.written > 0

    elapsed = time.monotonic() - started
    if progress:
        progress(ExportProgress(frames_written, total, elapsed))
    return ExportResult(
        path=output,
        frames=frames_written,
        elapsed=elapsed,
        encoder=spec.codec,
        audio_copied=audio_copied,
        band_coverage=renderer.coverage,
    )


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _frame_range(info: VideoInfo, options: ExportOptions) -> tuple[int, int]:
    first = info.frame_index_at(options.start) if options.start else 0
    if options.duration is None:
        return first, info.frame_count - 1
    span = max(1, int(round(options.duration * float(info.frame_rate))))
    return first, min(info.frame_count - 1, first + span - 1)


def _display_size(info: VideoInfo) -> tuple[int, int]:
    """Frame size as the viewer sees it, i.e. after any container rotation."""
    if info.rotation in (90, 270):
        return info.height, info.width
    return info.width, info.height


def _build_timeline(
    info: VideoInfo, telemetry: TelemetryLog, preset: Preset, sync: SyncModel
):
    video_times = info.frame_times()
    log_times = sync.offset + video_times * sync.scale
    return build_timeline(
        telemetry,
        video_times,
        log_times,
        options=preset.sampling,
        message_track=MessageTrack.from_log(telemetry, preset.messages),
    )


def _build_graph(stream, renderer: OverlayRenderer, info: VideoInfo):
    """Video -> [rotate] -> overlay per band -> yuv420p -> sink."""
    graph = av.filter.Graph()
    video_input = graph.add_buffer(template=stream)
    node = video_input

    if info.rotation:
        node = _add_rotation(graph, node, info.rotation)

    band_inputs = []
    for band in renderer.bands:
        buffer = graph.add(
            "buffer",
            video_size=f"{band.rect.width()}x{band.rect.height()}",
            pix_fmt="rgba",
            time_base=str(stream.time_base),
            pixel_aspect="1/1",
        )
        overlay = graph.add(
            "overlay",
            f"x={band.rect.left()}:y={band.rect.top()}:"
            "format=auto:eof_action=repeat:repeatlast=1",
        )
        node.link_to(overlay, 0, 0)
        buffer.link_to(overlay, 0, 1)
        band_inputs.append(buffer)
        node = overlay

    fmt = graph.add("format", "yuv420p")
    node.link_to(fmt)
    fmt.link_to(graph.add("buffersink"))
    graph.configure()
    return graph, video_input, band_inputs


def _add_rotation(graph, node, rotation: int):
    """Apply container rotation so the overlay is drawn in the viewer's orientation."""
    if rotation in (90, 270):
        transpose = graph.add("transpose", "clock" if rotation == 90 else "cclock")
        node.link_to(transpose)
        return transpose
    if rotation == 180:
        hflip = graph.add("hflip")
        vflip = graph.add("vflip")
        node.link_to(hflip)
        hflip.link_to(vflip)
        return vflip
    raise ValueError(f"unsupported rotation {rotation}")


def _add_video_stream(
    container,
    spec: EncoderSpec,
    info: VideoInfo,
    width: int,
    height: int,
    options: ExportOptions,
):
    stream = container.add_stream(spec.codec, rate=info.frame_rate)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    stream.time_base = Fraction(1, 1) / info.frame_rate
    encoder_options = spec.build_options(options.quality)
    encoder_options.update(options.encoder_options)
    stream.options = encoder_options
    # Carry the source's colour signalling so the burn does not shift the picture's
    # levels: yuvj420p footage is full-range and must be tagged as such.
    ctx = stream.codec_context
    if info.color_range:
        ctx.color_range = info.color_range
    if info.colorspace:
        ctx.colorspace = info.colorspace
    return stream


def _seek(container, stream, index: int, info: VideoInfo) -> None:
    target = int(
        round(index / float(info.frame_rate) / float(stream.time_base or 1))
    )
    container.seek(target, stream=stream, backward=True, any_frame=False)


def _frame_index(frame, stream, info: VideoInfo) -> int:
    if frame.pts is None:
        return 0
    time_base = stream.time_base or Fraction(1, 1000)
    return int(round(float(frame.pts * time_base) * float(info.frame_rate)))


def _push_bands(
    band_inputs: list, renderer: OverlayRenderer, state, frame
) -> None:
    for buffer, band in zip(band_inputs, renderer.bands, strict=True):
        image = renderer.render_band(band, state)
        array = qimage_to_ndarray(image)
        band_frame = av.VideoFrame.from_ndarray(
            np.ascontiguousarray(array), format="rgba"
        )
        band_frame.pts = frame.pts
        band_frame.time_base = frame.time_base
        buffer.push(band_frame)


def _pull_all(graph):
    frames = []
    while True:
        try:
            frames.append(graph.pull())
        except (av.BlockingIOError, av.EOFError):
            return frames


class _AudioCopier:
    """Remuxes the source audio into the output, without re-encoding it.

    The packets are fed in step with the video rather than appended afterwards: a muxer
    interleaves streams and needs their timestamps to advance together, so writing all
    the video and then all the audio makes it reject the file.

    Compressed audio frames do not align to arbitrary cut points, so a trimmed export
    can start up to one audio frame late (about 21 ms for AAC). An untrimmed export is
    exact, since every packet is copied byte for byte.
    """

    def __init__(
        self,
        source: Path,
        out_container,
        out_stream,
        start_time: float,
        end_time: float,
    ) -> None:
        self._container = av.open(str(source))
        self._stream = self._container.streams.audio[0]
        self._out_container = out_container
        self._out_stream = out_stream
        self._time_base = self._stream.time_base or Fraction(1, 48000)
        self._end_time = end_time
        self._offset = int(round(start_time / float(self._time_base)))
        if start_time > 0:
            self._container.seek(self._offset, stream=self._stream, backward=True)
        self._packets = self._container.demux(self._stream)
        self._pending = None
        self.written = 0
        self.finished = False

    def advance(self, video_time: float) -> None:
        """Write every audio packet that belongs at or before ``video_time``."""
        while not self.finished:
            packet = self._pending or next(self._packets, None)
            self._pending = None
            if packet is None or packet.dts is None:
                if packet is None:
                    self.finished = True
                continue
            timestamp = float((packet.pts or packet.dts) * self._time_base)
            if timestamp > self._end_time:
                self.finished = True
                break
            if timestamp > video_time:
                self._pending = packet
                break
            self._write(packet)

    def _write(self, packet) -> None:
        # Shift onto the trimmed timeline. Packets that would land before zero are
        # dropped rather than clamped: clamping would make several share a timestamp
        # and the muxer requires them to increase.
        if packet.dts is not None:
            packet.dts -= self._offset
        if packet.pts is not None:
            packet.pts -= self._offset
        if (packet.dts is not None and packet.dts < 0) or (
            packet.pts is not None and packet.pts < 0
        ):
            return
        packet.stream = self._out_stream
        self._out_container.mux(packet)
        self.written += 1

    def close(self) -> None:
        """Drain whatever is left of the window, then release the input."""
        self.advance(self._end_time)
        self._container.close()
        log.debug("copied %d audio packets", self.written)
