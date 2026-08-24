"""Data types produced by the log reader."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Channel:
    """A scalar telemetry channel in SI units, on the log's own timebase.

    ``t`` is strictly ascending log time in seconds. ``valid`` marks samples the
    autopilot itself considered usable (unhealthy airspeed sensor, rangefinder out of
    range, ...); invalid samples keep their raw value so diagnostics can show it, but
    consumers must honour the mask.
    """

    name: str
    kind: str
    t: np.ndarray
    v: np.ndarray
    valid: np.ndarray
    source: str

    def __post_init__(self) -> None:
        if not (len(self.t) == len(self.v) == len(self.valid)):
            raise ValueError(f"channel {self.name}: array lengths differ")

    def __len__(self) -> int:
        return len(self.t)

    @property
    def rate_hz(self) -> float:
        if len(self.t) < 2:
            return 0.0
        span = float(self.t[-1] - self.t[0])
        return (len(self.t) - 1) / span if span > 0 else 0.0

    @property
    def valid_fraction(self) -> float:
        return float(np.mean(self.valid)) if len(self) else 0.0

    def valid_range(self) -> tuple[float, float] | None:
        """Min/max over valid samples, or ``None`` when nothing is valid."""
        if not len(self) or not self.valid.any():
            return None
        vv = self.v[self.valid]
        return float(vv.min()), float(vv.max())


@dataclass
class MessageEvent:
    """A ``MSG`` status text emitted by the autopilot."""

    t: float
    text: str


@dataclass
class ModeChange:
    """A ``MODE`` transition."""

    t: float
    number: int
    name: str


@dataclass
class TelemetryLog:
    """Everything the overlay needs from one DataFlash log."""

    path: str
    channels: dict[str, Channel] = field(default_factory=dict)
    messages: list[MessageEvent] = field(default_factory=list)
    modes: list[ModeChange] = field(default_factory=list)
    #: Log time at which the vehicle was first armed, used as the flight-timer origin.
    arm_time: float | None = None
    t_start: float = 0.0
    t_end: float = 0.0
    #: Sources that were requested but produced no samples, for the probe report.
    missing: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start

    def channel(self, name: str) -> Channel | None:
        return self.channels.get(name)
