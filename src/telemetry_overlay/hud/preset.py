"""The overlay preset: which elements exist, where they sit, how they look.

A preset is deliberately independent of any particular flight -- it holds layout, theme
and units only. The video/log pair and the log delay live in a separate sync file, so
one preset can be reused across every flight.

Positions are normalised to the frame (0..1) and sizes are fractions of frame height,
which makes a preset resolution-independent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..telemetry.events import MessageTrackOptions
from ..telemetry.timeline import SamplingOptions
from .theme import Theme

PRESET_VERSION = 1

#: Anchor names: which point of the element box sits at (x, y).
ANCHORS = (
    "top-left",
    "top-center",
    "top-right",
    "center-left",
    "center",
    "center-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)


@dataclass
class ElementConfig:
    """Placement and per-element options for one HUD element."""

    type: str
    id: str = ""
    x: float = 0.05
    y: float = 0.05
    anchor: str = "top-left"
    visible: bool = True
    #: Multiplies the theme's sizes for this element only.
    scale: float = 1.0
    #: Display unit key; ``None`` means "the default for this quantity".
    unit: str | None = None
    #: Decimal places; ``None`` means "the unit's default".
    decimals: int | None = None
    #: Element-specific settings, see each element class.
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.type
        if self.anchor not in ANCHORS:
            raise ValueError(
                f"element {self.id}: unknown anchor {self.anchor!r}; "
                f"expected one of {', '.join(ANCHORS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "id": self.id,
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "anchor": self.anchor,
        }
        if not self.visible:
            data["visible"] = False
        if self.scale != 1.0:
            data["scale"] = self.scale
        if self.unit is not None:
            data["unit"] = self.unit
        if self.decimals is not None:
            data["decimals"] = self.decimals
        if self.options:
            data["options"] = dict(self.options)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElementConfig:
        try:
            type_name = data["type"]
        except KeyError:
            raise ValueError("element entry is missing its 'type'") from None
        return cls(
            type=type_name,
            id=data.get("id", ""),
            x=float(data.get("x", 0.05)),
            y=float(data.get("y", 0.05)),
            anchor=data.get("anchor", "top-left"),
            visible=bool(data.get("visible", True)),
            scale=float(data.get("scale", 1.0)),
            unit=data.get("unit"),
            decimals=data.get("decimals"),
            options=dict(data.get("options", {})),
        )


@dataclass
class Preset:
    """A complete overlay description."""

    name: str = "default"
    version: int = PRESET_VERSION
    theme: Theme = field(default_factory=Theme)
    elements: list[ElementConfig] = field(default_factory=list)
    messages: MessageTrackOptions = field(default_factory=MessageTrackOptions)
    sampling: SamplingOptions = field(default_factory=SamplingOptions)
    source: Path | None = field(default=None, compare=False)

    def element(self, element_id: str) -> ElementConfig | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def visible_elements(self) -> list[ElementConfig]:
        return [e for e in self.elements if e.visible]

    # ---- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "theme": self.theme.to_dict(),
            "messages": {
                "max_lines": self.messages.max_lines,
                "min_duration": self.messages.min_duration,
                "duration": self.messages.duration,
                "skip_before_arm": self.messages.skip_before_arm,
                "exclude": list(self.messages.exclude),
            },
            "sampling": {
                "max_gap": self.sampling.max_gap,
                "hysteresis": dict(self.sampling.hysteresis),
            },
            "elements": [e.to_dict() for e in self.elements],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Preset:
        version = int(data.get("version", PRESET_VERSION))
        if version > PRESET_VERSION:
            raise ValueError(
                f"preset version {version} is newer than this build supports "
                f"({PRESET_VERSION}); update the tool"
            )
        msg = data.get("messages") or {}
        sampling = data.get("sampling") or {}
        defaults = MessageTrackOptions()
        sampling_defaults = SamplingOptions()
        return cls(
            name=str(data.get("name", "default")),
            version=version,
            theme=Theme.from_dict(data.get("theme")),
            elements=[
                ElementConfig.from_dict(e) for e in data.get("elements", [])
            ],
            messages=MessageTrackOptions(
                max_lines=int(msg.get("max_lines", defaults.max_lines)),
                min_duration=float(msg.get("min_duration", defaults.min_duration)),
                duration=float(msg.get("duration", defaults.duration)),
                skip_before_arm=bool(
                    msg.get("skip_before_arm", defaults.skip_before_arm)
                ),
                exclude=tuple(msg.get("exclude", ())),
            ),
            sampling=SamplingOptions(
                max_gap=float(sampling.get("max_gap", sampling_defaults.max_gap)),
                hysteresis={
                    str(k): float(v)
                    for k, v in (
                        sampling.get("hysteresis")
                        or sampling_defaults.hysteresis
                    ).items()
                },
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> Preset:
        path = Path(path)
        preset = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        preset.source = path
        return preset

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        self.source = path
        return path
