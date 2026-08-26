"""Declarative mapping from DataFlash messages to HUD channels.

Every channel lists its sources in order of preference. The reader collects all of
them in a single pass and keeps the first one that actually produced samples, so a log
from a vehicle without an airspeed sensor or without an EKF3 core silently falls back
instead of failing.

Field names and rates below were verified against a real ArduPlane 4.7 log; see
``telemetry-overlay probe`` to check them against your own.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..units import ANGLE, CHARGE, CURRENT, LENGTH, PERCENT, SPEED, VERTICAL_SPEED, VOLTAGE

#: Channel names used throughout the HUD.
IAS = "ias"
GS = "gs"
ALT = "alt"
AGL = "agl"
VS = "vs"
VOLTAGE_CH = "voltage"
CURRENT_CH = "current"
CONSUMED = "consumed"
THROTTLE = "throttle"
ROLL = "roll"
PITCH = "pitch"
YAW = "yaw"
WIND_SPEED = "wind_speed"
WIND_DIR = "wind_dir"


@dataclass(frozen=True)
class Source:
    """One candidate origin for a channel.

    ``value`` and ``valid`` receive a decoded DataFlash message. They may raise
    ``AttributeError`` for messages from a firmware revision that lacks a field; the
    reader treats that as "this source is unavailable" rather than an error.
    """

    label: str
    msg: str
    value: Callable[[Any], float]
    valid: Callable[[Any], bool] | None = None
    #: Name of the instance/core field, when the message is emitted per sensor.
    instance_field: str | None = None
    instance: int = 0


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    kind: str
    #: Short label drawn by the default preset.
    label: str
    sources: tuple[Source, ...]


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        IAS,
        SPEED,
        "IAS",
        (
            # Gated on ARSP.H (sensor health) only. ARSP.U means "the EKF is currently
            # using this airspeed", which ArduPlane switches off during some manoeuvres
            # even though the pitot reading itself is fine -- and the reading is what a
            # HUD should show.
            Source(
                "ARSP.Airspeed",
                "ARSP",
                lambda m: m.Airspeed,
                lambda m: bool(m.H),
                "I",
            ),
            Source("CTUN.As", "CTUN", lambda m: m.As),
        ),
    ),
    ChannelSpec(
        GS,
        SPEED,
        "GS",
        (
            # EKF horizontal velocity at 50 Hz: ten times smoother on video than the
            # 5 Hz GPS speed, and already free of GPS glitches.
            Source(
                "XKF1.VN/VE",
                "XKF1",
                lambda m: math.hypot(m.VN, m.VE),
                None,
                "C",
            ),
            Source("GPS.Spd", "GPS", lambda m: m.Spd, lambda m: m.Status >= 3, "I"),
        ),
    ),
    ChannelSpec(
        ALT,
        LENGTH,
        "ALT",
        (
            Source("BARO.AltAMSL", "BARO", lambda m: m.AltAMSL, None, "I"),
            Source("GPS.Alt", "GPS", lambda m: m.Alt, lambda m: m.Status >= 3, "I"),
            Source("POS.Alt", "POS", lambda m: m.Alt),
        ),
    ),
    ChannelSpec(
        AGL,
        LENGTH,
        "AGL",
        (
            # Rangefinder only. Stat == 4 is RangeFinder::Status::Good; anything else
            # (NoData, OutOfRangeLow, OutOfRangeHigh) means the HUD shows "NO AGL".
            # There is deliberately no fallback: AGL must mean measured height.
            Source(
                "RFND.Dist",
                "RFND",
                lambda m: m.Dist,
                lambda m: m.Stat == 4,
                "Instance",
            ),
        ),
    ),
    ChannelSpec(
        VS,
        VERTICAL_SPEED,
        "VS",
        (
            Source("BARO.CRt", "BARO", lambda m: m.CRt, None, "I"),
            # EKF velocity is NED, so down-velocity negated is climb rate.
            Source("XKF1.VD", "XKF1", lambda m: -m.VD, None, "C"),
        ),
    ),
    ChannelSpec(
        VOLTAGE_CH,
        VOLTAGE,
        "BAT",
        (Source("BAT.Volt", "BAT", lambda m: m.Volt, None, "Inst"),),
    ),
    ChannelSpec(
        CURRENT_CH,
        CURRENT,
        "CUR",
        (Source("BAT.Curr", "BAT", lambda m: m.Curr, None, "Inst"),),
    ),
    ChannelSpec(
        CONSUMED,
        CHARGE,
        "MAH",
        # BAT.CurrTot is already the cumulative consumption in mAh, not coulombs.
        (Source("BAT.CurrTot", "BAT", lambda m: m.CurrTot, None, "Inst"),),
    ),
    ChannelSpec(
        THROTTLE,
        PERCENT,
        "THR",
        # CTUN.ThO is the throttle output already in 0..100 percent.
        (Source("CTUN.ThO", "CTUN", lambda m: m.ThO),),
    ),
    ChannelSpec(
        ROLL,
        ANGLE,
        "ROLL",
        (
            Source("ATT.Roll", "ATT", lambda m: m.Roll),
            Source("AHR2.Roll", "AHR2", lambda m: m.Roll),
        ),
    ),
    ChannelSpec(
        PITCH,
        ANGLE,
        "PITCH",
        (
            Source("ATT.Pitch", "ATT", lambda m: m.Pitch),
            Source("AHR2.Pitch", "AHR2", lambda m: m.Pitch),
        ),
    ),
    ChannelSpec(
        YAW,
        ANGLE,
        "HDG",
        (
            Source("ATT.Yaw", "ATT", lambda m: m.Yaw),
            Source("AHR2.Yaw", "AHR2", lambda m: m.Yaw),
        ),
    ),
    ChannelSpec(
        WIND_SPEED,
        SPEED,
        "WIND",
        # EKF3's own wind estimate (NE velocity of the air mass, m/s). No fallback:
        # a log without EKF3 wind estimation has no other source for this.
        (Source("XKF2.VWN/VWE", "XKF2", lambda m: math.hypot(m.VWN, m.VWE), None, "C"),),
    ),
    ChannelSpec(
        WIND_DIR,
        ANGLE,
        "WIND DIR",
        # Bearing the wind blows *toward* (the heading of the (VWN, VWE) vector
        # itself), matching a G1000-style wind vector rather than the METAR "wind
        # from" convention -- see WindReadout for how the HUD arrow uses this.
        (
            Source(
                "XKF2.VWN/VWE",
                "XKF2",
                lambda m: math.degrees(math.atan2(m.VWE, m.VWN)) % 360.0,
                None,
                "C",
            ),
        ),
    ),
)

CHANNELS_BY_NAME: dict[str, ChannelSpec] = {c.name: c for c in CHANNELS}

#: Non-channel message types the reader also needs.
EVENT_TYPES: frozenset[str] = frozenset({"MSG", "MODE", "EV"})


def wanted_types() -> frozenset[str]:
    """All DataFlash message types worth decoding."""
    return frozenset(
        {src.msg for spec in CHANNELS for src in spec.sources}
    ) | EVENT_TYPES


# ArduPlane flight modes (ArduPlane/mode.h, Number enum). Kept local rather than taken
# from pymavlink so an unknown number degrades to "MODE n" instead of raising.
PLANE_MODES: dict[int, str] = {
    0: "MANUAL",
    1: "CIRCLE",
    2: "STABILIZE",
    3: "TRAINING",
    4: "ACRO",
    5: "FBWA",
    6: "FBWB",
    7: "CRUISE",
    8: "AUTOTUNE",
    10: "AUTO",
    11: "RTL",
    12: "LOITER",
    13: "TAKEOFF",
    14: "AVOID_ADSB",
    15: "GUIDED",
    16: "INITIALISING",
    17: "QSTABILIZE",
    18: "QHOVER",
    19: "QLOITER",
    20: "QLAND",
    21: "QRTL",
    22: "QAUTOTUNE",
    23: "QACRO",
    24: "THERMAL",
    25: "LOITER_ALT_QLAND",
}


def mode_name(number: int) -> str:
    return PLANE_MODES.get(int(number), f"MODE {int(number)}")


#: ``EV`` subsystem ids we care about (ArduPilot LogEvent enum).
EV_ARMED = 10
EV_DISARMED = 11
