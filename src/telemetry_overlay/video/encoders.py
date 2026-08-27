"""Encoder selection.

Hardware encoding is tried first and silently falls back to software, because whether
NVENC works depends on which GPU is driving the machine right now -- a laptop running
on its integrated graphics has the NVIDIA libraries installed but no usable device.
Availability is therefore probed by actually opening the encoder, not by looking at a
list of compiled-in codecs.
"""

from __future__ import annotations

import gc
import logging
from dataclasses import dataclass, field
from functools import lru_cache

import av
import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncoderSpec:
    """A candidate output encoder and its quality knob."""

    key: str
    codec: str
    #: Human-readable description for the CLI and the GUI's encoder picker.
    description: str
    #: Option name carrying the constant-quality value ("cq" on NVENC, "crf" on x264).
    quality_option: str
    default_quality: int
    options: dict[str, str] = field(default_factory=dict)
    hardware: bool = False

    def build_options(
        self, quality: int | None = None, target_bit_rate: int | None = None
    ) -> dict[str, str]:
        opts = dict(self.options)
        opts[self.quality_option] = str(
            self.default_quality if quality is None else quality
        )
        if target_bit_rate:
            # Constant-quality mode alone has no size ceiling: on 4K footage it
            # routinely asks for well over the source's own bitrate, since a camera's
            # real-time hardware encoder compresses harder than a quality target tuned
            # for "as close to lossless as practical" (see export.py). Capping maxrate
            # at the source's bitrate keeps the *quality* knob as the primary control
            # while stopping the output from growing past the original by design,
            # per the project's "keep quality and size as close to unchanged as
            # possible" rule (CLAUDE.md, Export).
            opts["maxrate"] = str(target_bit_rate)
            opts["bufsize"] = str(target_bit_rate * 2)
            if self.hardware:
                # NVENC's vbr rate control ignores maxrate unless an average target
                # bitrate is also set -- the spec's default of b=0 means "unconstrained".
                opts["b"] = str(target_bit_rate)
        return opts


#: Preference order: hardware first, then software.
ENCODERS: tuple[EncoderSpec, ...] = (
    EncoderSpec(
        key="nvenc_h264",
        codec="h264_nvenc",
        description="NVIDIA NVENC H.264 (hardware)",
        quality_option="cq",
        default_quality=19,
        # p5/hq is the quality-leaning preset that still runs far above realtime.
        # profile=high matches the High profile most cameras record in (8x8
        # transform, more CABAC tools); NVENC otherwise defaults to Main, which
        # burns extra bits for no quality gain.
        options={"preset": "p5", "tune": "hq", "rc": "vbr", "b": "0", "profile": "high"},
        hardware=True,
    ),
    EncoderSpec(
        key="nvenc_hevc",
        codec="hevc_nvenc",
        description="NVIDIA NVENC HEVC (hardware, smaller files)",
        quality_option="cq",
        default_quality=22,
        options={"preset": "p5", "tune": "hq", "rc": "vbr", "b": "0", "profile": "main"},
        hardware=True,
    ),
    EncoderSpec(
        key="x264",
        codec="libx264",
        description="x264 H.264 (software)",
        quality_option="crf",
        default_quality=18,
        options={"preset": "medium"},
    ),
    EncoderSpec(
        key="x265",
        codec="libx265",
        description="x265 HEVC (software, slow)",
        quality_option="crf",
        default_quality=22,
        # x265 writes its banner and summary straight to stderr rather than through
        # FFmpeg's log, so it has to be silenced with its own parameter.
        options={"preset": "medium", "x265-params": "log-level=none"},
    ),
)

ENCODERS_BY_KEY: dict[str, EncoderSpec] = {e.key: e for e in ENCODERS}


#: Options applied only while probing, to keep chatty encoders quiet.
_PROBE_OPTIONS: dict[str, dict[str, str]] = {
    "libx265": {"x265-params": "log-level=none"},
}


@lru_cache(maxsize=None)
def is_available(codec: str) -> bool:
    """Whether ``codec`` can actually encode a frame on this machine."""
    if codec not in av.codecs_available:
        return False
    # x265 and friends print a multi-line banner on open; a capability probe must stay
    # quiet, so silence the FFmpeg log for the duration.
    previous = av.logging.get_level()
    av.logging.set_level(av.logging.PANIC)
    try:
        with av.open("/dev/null" if _has_devnull() else "NUL", "w", format="null") as oc:
            stream = oc.add_stream(codec, rate=30)
            stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
            stream.options = _PROBE_OPTIONS.get(codec, {})
            frame = av.VideoFrame.from_ndarray(
                np.zeros((240, 320, 3), dtype=np.uint8), format="rgb24"
            )
            frame.pts = 0
            list(stream.encode(frame))
            list(stream.encode())
            del stream
        # Encoders print their summary when freed, so make that happen while the log
        # is still silenced rather than at the next garbage collection.
        gc.collect()
    except Exception as exc:  # noqa: BLE001 - any failure means "unusable here"
        log.debug("encoder %s unavailable: %s", codec, exc)
        return False
    finally:
        av.logging.set_level(previous)
    return True


def _has_devnull() -> bool:
    import os

    return os.name != "nt"


def available_encoders() -> list[EncoderSpec]:
    return [spec for spec in ENCODERS if is_available(spec.codec)]


def select_encoder(preferred: str | None = None) -> EncoderSpec:
    """Resolve an encoder key to a usable encoder.

    An explicitly requested encoder that cannot run is an error rather than a silent
    downgrade: the user asked for it and deserves to know why they are not getting it.
    Without a request, the best available one wins.
    """
    if preferred:
        try:
            spec = ENCODERS_BY_KEY[preferred]
        except KeyError:
            raise ValueError(
                f"unknown encoder {preferred!r}; available keys: "
                f"{', '.join(ENCODERS_BY_KEY)}"
            ) from None
        if not is_available(spec.codec):
            raise RuntimeError(
                f"encoder {spec.codec} is not usable on this machine "
                "(no compatible GPU, or the driver is unavailable)"
            )
        return spec

    for spec in ENCODERS:
        if is_available(spec.codec):
            if spec.hardware:
                log.info("using hardware encoder %s", spec.codec)
            return spec
    raise RuntimeError("no usable video encoder found")
