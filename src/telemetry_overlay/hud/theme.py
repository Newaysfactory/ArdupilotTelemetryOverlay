"""Visual style of the overlay.

Every size is a fraction of the frame *height*, never a pixel count: a layout designed
against a 1080p proxy renders identically on the 4K master. Colours are ``#RRGGBB`` or
``#RRGGBBAA`` strings so a preset stays hand-editable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields as dataclass_fields
from typing import Any


@dataclass
class Theme:
    """Colours, fonts and line weights shared by all elements."""

    name: str = "osd"

    # -- typography ---------------------------------------------------------
    #: Monospaced by design: proportional digits make a changing readout jitter.
    font_family: str = "Consolas"
    #: Optional path to a font file to load, for output that does not depend on which
    #: fonts happen to be installed on the machine doing the render.
    font_file: str = ""
    font_weight: int = 700
    #: Fractions of frame height.
    value_size: float = 0.034
    label_size: float = 0.018
    unit_size: float = 0.016
    message_size: float = 0.022

    # -- colours ------------------------------------------------------------
    value_color: str = "#FFFFFFFF"
    label_color: str = "#B4C8D2FF"
    unit_color: str = "#8CA0AAFF"
    message_color: str = "#FFE680FF"
    warn_color: str = "#FF6E5AFF"
    #: Used for "NO AGL" and other no-data states.
    no_data_color: str = "#FFFFFF73"

    # -- legibility over arbitrary footage ----------------------------------
    #: Visible thickness of the dark outline around glyphs and lines, as a fraction of
    #: the font size. Half of it grows outward, so ~0.06 reads as a crisp edge while
    #: much more starts closing up the counters of the glyphs.
    outline_width: float = 0.06
    outline_color: str = "#000000C8"
    #: Optional translucent plate behind text blocks; empty string disables it.
    plate_color: str = ""
    plate_padding: float = 0.25

    # -- artificial horizon -------------------------------------------------
    horizon_color: str = "#00FF87FF"
    horizon_line_width: float = 0.0025
    horizon_ladder_color: str = "#00FF87E6"
    #: Translucent sky/ground wash; empty strings keep the horizon a pure line drawing.
    horizon_sky_color: str = ""
    horizon_ground_color: str = ""
    #: Fixed aircraft reference symbol at the centre of the horizon.
    horizon_reference_color: str = "#FFD400FF"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Theme:
        """Build a theme from a preset, ignoring keys this version does not know.

        Forward compatibility matters here: a preset saved by a newer build should still
        open, just without the settings this build cannot honour.
        """
        if not data:
            return cls()
        known = {f.name for f in dataclass_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
