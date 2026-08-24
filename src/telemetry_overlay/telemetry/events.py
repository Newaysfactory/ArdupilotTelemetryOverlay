"""Status-message track with guaranteed minimum on-screen time.

ArduPilot status texts are instantaneous events, and several often land in the same
millisecond ("Landing sequence start" is followed by three more within one frame).
Drawing them naively would make each flash for a single frame.

The rule implemented here: a message occupies a slot for at least ``min_duration``
seconds. With ``max_lines`` slots, message *i* is pushed off screen by message
*i + max_lines*, so postponing that message's appearance until slot capacity frees up
is exactly what guarantees the minimum. Bursts therefore play out as a short queue
instead of being lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .dfreader import filter_boot_messages
from .model import MessageEvent, TelemetryLog


@dataclass
class MessageTrackOptions:
    #: Simultaneously visible messages.
    max_lines: int = 3
    #: Guaranteed on-screen time; the requirement is at least one second.
    min_duration: float = 1.0
    #: How long a message stays when nothing pushes it out.
    duration: float = 4.0
    #: Drop the boot banner and everything logged before arming.
    skip_before_arm: bool = True
    #: Extra regular expressions to hide (fully matched against the text).
    exclude: tuple[str, ...] = ()


@dataclass
class MessageTrack:
    """Precomputed visibility windows for every message."""

    texts: list[str] = field(default_factory=list)
    starts: np.ndarray = field(default_factory=lambda: np.zeros(0))
    ends: np.ndarray = field(default_factory=lambda: np.zeros(0))
    max_lines: int = 3

    @classmethod
    def from_log(
        cls, log: TelemetryLog, options: MessageTrackOptions | None = None
    ) -> MessageTrack:
        options = options or MessageTrackOptions()
        events = log.messages
        if options.skip_before_arm:
            events = filter_boot_messages(events, log.arm_time)
        if options.exclude:
            patterns = [re.compile(p) for p in options.exclude]
            events = [
                e for e in events if not any(p.fullmatch(e.text) for p in patterns)
            ]
        return cls.from_events(events, options)

    @classmethod
    def from_events(
        cls, events: list[MessageEvent], options: MessageTrackOptions | None = None
    ) -> MessageTrack:
        options = options or MessageTrackOptions()
        events = sorted(events, key=lambda e: e.t)
        lines = max(1, options.max_lines)
        n = len(events)
        starts = np.zeros(n)
        for i, event in enumerate(events):
            # A message may only appear once the message it will displace has had its
            # guaranteed time on screen.
            earliest = (
                starts[i - lines] + options.min_duration if i >= lines else -np.inf
            )
            starts[i] = max(event.t, earliest)

        ends = starts + max(options.duration, options.min_duration)
        # Displaced by a later message, but never before its minimum time.
        if n > lines:
            ends[:-lines] = np.minimum(ends[:-lines], starts[lines:])
        return cls(
            texts=[e.text for e in events],
            starts=starts,
            ends=ends,
            max_lines=lines,
        )

    def at(self, t: float) -> tuple[str, ...]:
        """Messages visible at log time ``t``, oldest first."""
        if not self.texts:
            return ()
        # Only the last `max_lines` candidates can be visible, by construction.
        last = int(np.searchsorted(self.starts, t, side="right"))
        first = max(0, last - self.max_lines)
        return tuple(
            self.texts[i]
            for i in range(first, last)
            if self.starts[i] <= t < self.ends[i]
        )
