"""Read the channels the HUD needs out of a DataFlash ``.bin`` log.

Parsing is done by pymavlink, the parser maintained by the ArduPilot project itself.
A full log holds hundreds of thousands of messages (187k IMU records in the sample),
so the decoded result is cached next to the log as a small ``.npz``: re-running an
export or tweaking the layout then costs nothing.

Time base: log time is ``TimeUS`` in seconds, i.e. time since the flight controller
booted. It is monotonic and independent of GPS lock, which makes it the right reference
for a video log delay.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from . import fields
from .model import Channel, MessageEvent, ModeChange, TelemetryLog

log = logging.getLogger(__name__)

#: Cache format version. The channel definitions are fingerprinted separately (see
#: ``_extraction_signature``) so editing fields.py invalidates caches automatically.
CACHE_VERSION = 2
CACHE_SUFFIX = ".overlay-cache.npz"

#: Status texts emitted while booting, before the vehicle can fly. They are dropped by
#: default: a dozen of them land in the same millisecond and would otherwise occupy the
#: message area for a dozen seconds.
_BOOT_MESSAGE_PREFIXES = (
    "ArduPlane",
    "ChibiOS:",
    "Param space used:",
    "RCOut:",
    "IMU0:",
    "IMU1:",
    "IMU2:",
    "Frame:",
    "GPS 1:",
    "GPS 2:",
    "u-blox",
    "New fence",
    "Field Elevation Set:",
    "EKF3 IMU",
    "AHRS:",
    "Calibrating barometer",
    "Barometer",
    "Airspeed",
    "RC Protocol:",
)


def cache_path_for(log_path: Path) -> Path:
    return Path(str(log_path) + CACHE_SUFFIX)


def read_log(
    path: str | Path,
    *,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> TelemetryLog:
    """Parse ``path`` (or load its cache) into a :class:`TelemetryLog`."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    cache = cache_path_for(path)
    if use_cache and cache.exists():
        cached = _load_cache(cache, path)
        if cached is not None:
            log.debug("loaded telemetry cache %s", cache)
            return cached

    if progress:
        progress(f"parsing {path.name} ({path.stat().st_size / 1e6:.0f} MB)...")
    result = _parse(path)

    if use_cache:
        try:
            _save_cache(cache, path, result)
        except OSError as exc:  # a read-only data directory must not break the run
            log.warning("could not write telemetry cache: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _Collector:
    """Accumulates samples for one (channel, source) pair."""

    __slots__ = ("t", "v", "valid", "broken")

    def __init__(self) -> None:
        self.t: list[float] = []
        self.v: list[float] = []
        self.valid: list[bool] = []
        #: Set when the message lacks a field this source needs, in which case the
        #: source is skipped entirely rather than yielding a partial channel.
        self.broken = False


def _parse(path: Path) -> TelemetryLog:
    from pymavlink import mavutil

    # pymavlink's skip_to_type() mutates the set it is handed (and adds a few types of
    # its own), so give it a private mutable copy.
    types = set(fields.wanted_types())
    mlog = mavutil.mavlink_connection(str(path), robust_parsing=True)

    # msg type -> [(spec, source, collector), ...]
    dispatch: dict[str, list[tuple[fields.ChannelSpec, fields.Source, _Collector]]] = {}
    collectors: dict[tuple[str, str], _Collector] = {}
    for spec in fields.CHANNELS:
        for src in spec.sources:
            collector = _Collector()
            collectors[(spec.name, src.label)] = collector
            dispatch.setdefault(src.msg, []).append((spec, src, collector))

    messages: list[MessageEvent] = []
    modes: list[ModeChange] = []
    arm_time: float | None = None
    t_min = float("inf")
    t_max = float("-inf")

    while True:
        msg = mlog.recv_match(type=types)
        if msg is None:
            break
        msg_type = msg.get_type()
        time_us = getattr(msg, "TimeUS", None)
        if time_us is None:
            continue
        t = time_us * 1e-6
        t_min = min(t_min, t)
        t_max = max(t_max, t)

        if msg_type == "MSG":
            messages.append(MessageEvent(t, str(msg.Message).strip()))
            continue
        if msg_type == "MODE":
            number = int(getattr(msg, "ModeNum", -1))
            modes.append(ModeChange(t, number, fields.mode_name(number)))
            continue
        if msg_type == "EV":
            if int(msg.Id) == fields.EV_ARMED and arm_time is None:
                arm_time = t
            continue

        for _spec, src, collector in dispatch.get(msg_type, ()):
            if collector.broken:
                continue
            if src.instance_field is not None:
                instance = getattr(msg, src.instance_field, 0)
                if instance is not None and int(instance) != src.instance:
                    continue
            try:
                value = float(src.value(msg))
                is_valid = True if src.valid is None else bool(src.valid(msg))
            except AttributeError:
                collector.broken = True
                continue
            except (TypeError, ValueError):
                continue
            collector.t.append(t)
            collector.v.append(value)
            collector.valid.append(is_valid)

    if arm_time is None:
        arm_time = _arm_time_from_messages(messages)

    channels: dict[str, Channel] = {}
    missing: list[str] = []
    for spec in fields.CHANNELS:
        channel = _pick_source(spec, collectors)
        if channel is None:
            missing.append(spec.name)
        else:
            channels[spec.name] = channel

    if t_min == float("inf"):
        t_min = t_max = 0.0

    return TelemetryLog(
        path=str(path),
        channels=channels,
        messages=messages,
        modes=modes,
        arm_time=arm_time,
        t_start=t_min,
        t_end=t_max,
        missing=missing,
    )


def _pick_source(
    spec: fields.ChannelSpec, collectors: dict[tuple[str, str], _Collector]
) -> Channel | None:
    """First source in preference order that produced usable samples."""
    for src in spec.sources:
        collector = collectors[(spec.name, src.label)]
        if collector.broken or not collector.t:
            continue
        t = np.asarray(collector.t, dtype=np.float64)
        v = np.asarray(collector.v, dtype=np.float64)
        valid = np.asarray(collector.valid, dtype=bool)
        # Non-finite readings (a disconnected sensor can log NaN) are never valid.
        valid &= np.isfinite(v)
        t, v, valid = _ensure_ascending(t, v, valid)
        return Channel(spec.name, spec.kind, t, v, valid, src.label)
    return None


def _ensure_ascending(
    t: np.ndarray, v: np.ndarray, valid: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sort by time and drop duplicate timestamps.

    ``TimeUS`` is monotonic in practice, but a log that was cut and concatenated, or a
    controller reboot mid-file, can break that. ``np.interp`` silently returns garbage
    on unsorted input, so this is cheap insurance.
    """
    if t.size and not np.all(np.diff(t) > 0):
        order = np.argsort(t, kind="stable")
        t, v, valid = t[order], v[order], valid[order]
        keep = np.ones(t.size, dtype=bool)
        keep[1:] = np.diff(t) > 0
        t, v, valid = t[keep], v[keep], valid[keep]
    return t, v, valid


def _arm_time_from_messages(messages: list[MessageEvent]) -> float | None:
    """Fall back to the status text when no ``EV`` arming event was logged."""
    for event in messages:
        if event.text.startswith("Throttle armed"):
            return event.t
    return None


def filter_boot_messages(
    messages: list[MessageEvent], arm_time: float | None
) -> list[MessageEvent]:
    """Drop the boot banner: everything before arming plus known startup chatter."""
    result = []
    for event in messages:
        if arm_time is not None and event.t < arm_time:
            continue
        if event.text.startswith(_BOOT_MESSAGE_PREFIXES):
            continue
        result.append(event)
    return result


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _extraction_signature() -> str:
    """Fingerprint of the channel definitions.

    Included in the cache key so that changing a source, a validity rule or a fallback
    order in ``fields.py`` invalidates every cache automatically -- forgetting to bump
    a version number would otherwise serve stale telemetry that looks correct.
    """
    source = Path(fields.__file__).read_bytes()
    return hashlib.sha256(source).hexdigest()[:16]


def _cache_key(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "version": CACHE_VERSION,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "fields": _extraction_signature(),
    }


def _save_cache(cache: Path, source: Path, result: TelemetryLog) -> None:
    arrays: dict[str, np.ndarray] = {}
    meta: dict[str, Any] = {
        "key": _cache_key(source),
        "t_start": result.t_start,
        "t_end": result.t_end,
        "arm_time": result.arm_time,
        "missing": result.missing,
        "channels": {},
        "messages": [[e.t, e.text] for e in result.messages],
        "modes": [[m.t, m.number, m.name] for m in result.modes],
    }
    for name, channel in result.channels.items():
        arrays[f"{name}/t"] = channel.t
        arrays[f"{name}/v"] = channel.v
        arrays[f"{name}/valid"] = channel.valid
        meta["channels"][name] = {"kind": channel.kind, "source": channel.source}
    arrays["meta"] = np.frombuffer(
        json.dumps(meta).encode("utf-8"), dtype=np.uint8
    )
    tmp = cache.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(cache)


def _load_cache(cache: Path, source: Path) -> TelemetryLog | None:
    try:
        with np.load(cache, allow_pickle=False) as data:
            meta = json.loads(bytes(data["meta"]).decode("utf-8"))
            if meta.get("key") != _cache_key(source):
                return None
            channels = {}
            for name, info in meta["channels"].items():
                channels[name] = Channel(
                    name=name,
                    kind=info["kind"],
                    t=data[f"{name}/t"],
                    v=data[f"{name}/v"],
                    valid=data[f"{name}/valid"],
                    source=info["source"],
                )
            return TelemetryLog(
                path=str(source),
                channels=channels,
                messages=[MessageEvent(t, text) for t, text in meta["messages"]],
                modes=[ModeChange(t, n, name) for t, n, name in meta["modes"]],
                arm_time=meta["arm_time"],
                t_start=meta["t_start"],
                t_end=meta["t_end"],
                missing=list(meta["missing"]),
            )
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        log.warning("ignoring unreadable telemetry cache %s: %s", cache, exc)
        return None
