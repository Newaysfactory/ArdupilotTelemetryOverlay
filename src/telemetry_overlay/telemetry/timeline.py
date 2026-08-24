"""Resample telemetry onto the video's frame timebase.

Everything is computed once, vectorised, for all frames of the export before the first
frame is drawn: 6743 frames of a dozen channels is a few hundred kilobytes and removes
per-frame interpolation from the render loop entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import fields
from .events import MessageTrack
from .model import Channel, TelemetryLog


@dataclass
class SamplingOptions:
    """Tunables for turning irregular log samples into per-frame values."""

    #: Beyond this hole between two consecutive valid samples the channel reads as
    #: unavailable rather than being interpolated across the gap.
    max_gap: float = 1.0
    #: Per-channel debounce, in seconds, applied to the validity mask. The rangefinder
    #: flickers in and out of range at 50 Hz near its limit; without this the AGL
    #: readout would strobe between a number and "NO AGL".
    hysteresis: dict[str, float] = field(
        default_factory=lambda: {fields.AGL: 0.3}
    )


def sample_channel(
    channel: Channel,
    times: np.ndarray,
    *,
    max_gap: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Linearly interpolate a channel at ``times``.

    Only samples the autopilot flagged valid take part in the interpolation. A time is
    reported invalid when it falls outside the channel's valid span or inside a gap
    longer than ``max_gap``, so a dead sensor or a log hole shows as "no data" instead
    of a straight line drawn between two distant readings.
    """
    times = np.asarray(times, dtype=np.float64)
    tv = channel.t[channel.valid]
    vv = channel.v[channel.valid]
    if tv.size == 0:
        return np.zeros_like(times), np.zeros(times.shape, dtype=bool)
    if tv.size == 1:
        values = np.full(times.shape, vv[0])
        return values, np.abs(times - tv[0]) <= max_gap

    values = np.interp(times, tv, vv)
    idx = np.searchsorted(tv, times, side="left")
    left = np.clip(idx - 1, 0, tv.size - 1)
    right = np.clip(idx, 0, tv.size - 1)
    gap = tv[right] - tv[left]
    inside = (times >= tv[0]) & (times <= tv[-1])
    return values, inside & (gap <= max_gap)


def debounce(valid: np.ndarray, dt: float, hold: float) -> np.ndarray:
    """Suppress state changes shorter than ``hold`` seconds.

    The output only follows the input once the input has held its new state for the
    whole window, which delays every transition by ``hold`` -- acceptable for a
    readout, and the point of the filter.
    """
    if hold <= 0 or valid.size == 0 or dt <= 0:
        return valid
    needed = max(1, int(round(hold / dt)))
    out = np.empty_like(valid)
    state = bool(valid[0])
    run = 0
    for i in range(valid.size):
        if bool(valid[i]) == state:
            run = 0
        else:
            run += 1
            if run >= needed:
                state = not state
                run = 0
        out[i] = state
    return out


def sample_steps(change_times: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Index of the step in effect at each time, ``-1`` before the first change."""
    if change_times.size == 0:
        return np.full(times.shape, -1, dtype=np.int32)
    return (np.searchsorted(change_times, times, side="right") - 1).astype(np.int32)


@dataclass
class FrameState:
    """Everything the renderer needs for one frame. Values are SI."""

    frame: int
    video_time: float
    log_time: float
    values: dict[str, float]
    valid: dict[str, bool]
    mode: str | None
    flight_time: float | None
    messages: tuple[str, ...]

    def value(self, name: str) -> float | None:
        """SI value, or ``None`` when the channel has no usable data here."""
        return self.values[name] if self.valid.get(name) else None


@dataclass
class Timeline:
    """Per-frame telemetry for one video/log pair at a given sync offset."""

    video_times: np.ndarray
    log_times: np.ndarray
    values: dict[str, np.ndarray]
    valid: dict[str, np.ndarray]
    mode_names: list[str]
    mode_index: np.ndarray
    flight_time: np.ndarray
    messages: MessageTrack
    #: Channels the log did not provide at all.
    missing: list[str]

    def __len__(self) -> int:
        return int(self.video_times.size)

    def state(self, frame: int) -> FrameState:
        mode_i = int(self.mode_index[frame])
        ft = float(self.flight_time[frame])
        return FrameState(
            frame=frame,
            video_time=float(self.video_times[frame]),
            log_time=float(self.log_times[frame]),
            values={k: float(v[frame]) for k, v in self.values.items()},
            valid={k: bool(v[frame]) for k, v in self.valid.items()},
            mode=self.mode_names[mode_i] if mode_i >= 0 else None,
            flight_time=None if np.isnan(ft) else ft,
            messages=self.messages.at(float(self.log_times[frame])),
        )


def build_timeline(
    log: TelemetryLog,
    video_times: np.ndarray,
    log_times: np.ndarray,
    *,
    options: SamplingOptions | None = None,
    message_track: MessageTrack | None = None,
) -> Timeline:
    """Resample ``log`` at ``log_times`` (one entry per video frame)."""
    options = options or SamplingOptions()
    video_times = np.asarray(video_times, dtype=np.float64)
    log_times = np.asarray(log_times, dtype=np.float64)
    dt = (
        float(np.median(np.diff(video_times)))
        if video_times.size > 1
        else 0.0
    )

    values: dict[str, np.ndarray] = {}
    valid: dict[str, np.ndarray] = {}
    for spec in fields.CHANNELS:
        channel = log.channel(spec.name)
        if channel is None:
            values[spec.name] = np.zeros_like(log_times)
            valid[spec.name] = np.zeros(log_times.shape, dtype=bool)
            continue
        v, ok = sample_channel(channel, log_times, max_gap=options.max_gap)
        hold = options.hysteresis.get(spec.name, 0.0)
        if hold:
            ok = debounce(ok, dt, hold)
        values[spec.name] = v
        valid[spec.name] = ok

    mode_times = np.asarray([m.t for m in log.modes], dtype=np.float64)
    mode_index = sample_steps(mode_times, log_times)
    mode_names = [m.name for m in log.modes]

    if log.arm_time is None:
        flight_time = np.full(log_times.shape, np.nan)
    else:
        flight_time = log_times - log.arm_time
        flight_time[flight_time < 0] = np.nan

    return Timeline(
        video_times=video_times,
        log_times=log_times,
        values=values,
        valid=valid,
        mode_names=mode_names,
        mode_index=mode_index,
        flight_time=flight_time,
        messages=message_track or MessageTrack.from_log(log),
        missing=list(log.missing),
    )
