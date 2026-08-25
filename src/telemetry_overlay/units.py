"""Unit registry and formatting.

All telemetry is kept internally in SI base units (m/s, m, V, deg, s). Units are a
presentation concern: each HUD element stores the unit it wants to display and the
conversion happens at draw time. This keeps the telemetry layer unit-agnostic and lets
a single preset mix, say, knots for airspeed with metres for altitude.
"""

from __future__ import annotations

from dataclasses import dataclass

# Quantity kinds. An element may only be assigned a unit of its channel's kind.
SPEED = "speed"
LENGTH = "length"
VERTICAL_SPEED = "vertical_speed"
VOLTAGE = "voltage"
CURRENT = "current"
CHARGE = "charge"
PERCENT = "percent"
ANGLE = "angle"
DURATION = "duration"
DIMENSIONLESS = "dimensionless"


@dataclass(frozen=True)
class Unit:
    """A display unit. ``display = si_value * factor``."""

    key: str
    kind: str
    symbol: str
    factor: float
    decimals: int = 0

    def convert(self, si_value: float) -> float:
        return si_value * self.factor

    def format(self, si_value: float, decimals: int | None = None) -> str:
        d = self.decimals if decimals is None else decimals
        return f"{self.convert(si_value):.{d}f}"


_UNITS: dict[str, Unit] = {
    # Speed (SI: m/s)
    "m/s": Unit("m/s", SPEED, "m/s", 1.0, 0),
    "km/h": Unit("km/h", SPEED, "km/h", 3.6, 0),
    "kt": Unit("kt", SPEED, "kt", 1.9438444924406, 0),
    "mph": Unit("mph", SPEED, "mph", 2.2369362920544, 0),
    # Length (SI: m)
    "m": Unit("m", LENGTH, "m", 1.0, 0),
    "ft": Unit("ft", LENGTH, "ft", 3.2808398950131, 0),
    "km": Unit("km", LENGTH, "km", 0.001, 2),
    # Vertical speed (SI: m/s)
    "m/s_v": Unit("m/s_v", VERTICAL_SPEED, "m/s", 1.0, 1),
    "ft/min": Unit("ft/min", VERTICAL_SPEED, "ft/min", 196.85039370079, 0),
    # Voltage
    "V": Unit("V", VOLTAGE, "V", 1.0, 1),
    # Current (SI: A)
    "A": Unit("A", CURRENT, "A", 1.0, 1),
    # Charge (SI: mAh -- BAT.CurrTot is already logged in mAh, not coulombs)
    "mAh": Unit("mAh", CHARGE, "mAh", 1.0, 0),
    # Percent
    "%": Unit("%", PERCENT, "%", 1.0, 0),
    # Angle
    "deg": Unit("deg", ANGLE, "°", 1.0, 0),
    # Duration and plain numbers are formatted by their own elements.
    "s": Unit("s", DURATION, "s", 1.0, 0),
    "": Unit("", DIMENSIONLESS, "", 1.0, 0),
}

# Sensible default per kind, used when a preset omits the unit.
DEFAULT_UNITS: dict[str, str] = {
    SPEED: "km/h",
    LENGTH: "m",
    VERTICAL_SPEED: "m/s_v",
    VOLTAGE: "V",
    CURRENT: "A",
    CHARGE: "mAh",
    PERCENT: "%",
    ANGLE: "deg",
    DURATION: "s",
    DIMENSIONLESS: "",
}


def get_unit(key: str) -> Unit:
    try:
        return _UNITS[key]
    except KeyError:
        raise ValueError(
            f"unknown unit {key!r}; available: {', '.join(sorted(_UNITS))}"
        ) from None


def units_for_kind(kind: str) -> list[Unit]:
    """All units compatible with a quantity kind, for the GUI's unit picker."""
    return [u for u in _UNITS.values() if u.kind == kind]


def default_unit(kind: str) -> Unit:
    return get_unit(DEFAULT_UNITS[kind])


def resolve_unit(key: str | None, kind: str) -> Unit:
    """Pick the unit named by a preset, falling back to the kind's default.

    Raises if the named unit exists but measures a different quantity, which is
    almost always a typo in a hand-edited preset rather than an intent.
    """
    if not key:
        return default_unit(kind)
    unit = get_unit(key)
    if unit.kind != kind:
        raise ValueError(
            f"unit {key!r} measures {unit.kind}, not {kind}"
        )
    return unit


def format_duration(seconds: float) -> str:
    """``MM:SS`` for flight timers, growing to ``H:MM:SS`` past an hour."""
    if seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_heading(degrees: float) -> str:
    """Normalise a yaw angle to a 3-digit compass heading (001..360)."""
    h = degrees % 360.0
    rounded = int(round(h)) % 360
    return f"{rounded if rounded else 360:03d}"
