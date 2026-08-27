"""Persistent, incrementally-filled store for the video's optical-flow roll rate.

Optical flow is by far the slowest thing the program does, and both ``autosync`` and
``manualsync`` need the same signal. The trick that makes caching simple is that the
computed value is a property of *one pair of consecutive frames*:
``rate[p]`` is the image rotation between frame ``p`` and frame ``p + 1``, and it does
not depend at all on which time range the user asked for. A requested range only
selects *which* pairs get computed, never what any of them is worth.

So this does not cache "the analysis of range X" -- which would leave the caller to
reconcile partial, overlapping and disjoint ranges by hand. It caches one array
covering the *whole* video, plus a boolean mask of which entries have actually been
computed. A request fills only the holes inside it and reuses everything else, so:

* analysing 0-450s and then 100-115s costs nothing the second time;
* analysing 100-115s first and then 0-450s only computes 0-100 and 115-450;
* ranges in any order, overlapping or not, never recompute a pair twice.

Each pair is independently computable, so a hole filled next to existing data has no
seam: correctness never depends on the order the ranges arrived in.

Size is a non-issue: 450s at 30fps is ~13500 float32 values, about 54 KB.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .cache import ensure_cache_dir_for

log = logging.getLogger(__name__)

#: Bump when a change to the optical-flow computation makes stored values wrong.
#: Unlike the telemetry cache -- which fingerprints ``fields.py`` automatically --
#: this cannot fingerprint its source file: the flow computation shares
#: ``autosync.py`` with plotting and fitting code, so hashing the file would discard
#: valid caches on every unrelated edit. Bump this by hand when changing
#: ``_rotation_between`` or ``_compute_pair_range``.
FLOW_CACHE_VERSION = 1

CACHE_FILENAME = "roll-rate.npz"

#: Holes shorter than this are filled by computing straight through them instead of
#: splitting the pass in two. A seek plus the decode back up to the next keyframe
#: costs roughly as much as decoding a second or two sequentially, so splitting to
#: save a handful of frames is a net loss.
DEFAULT_MERGE_GAP_SECONDS = 2.0


@dataclass(frozen=True)
class FlowParams:
    """The inputs, besides the video itself, that change the computed values."""

    analysis_width: int
    max_features: int

    def as_dict(self) -> dict[str, int]:
        return {"analysis_width": self.analysis_width, "max_features": self.max_features}


class RollRateCache:
    """Whole-video roll-rate array plus a mask of which entries are real.

    Create with :meth:`load`; fill holes with :meth:`store`; persist with
    :meth:`save`. Values are degrees per second, indexed by frame-pair index.
    """

    def __init__(self, path: Path, n_pairs: int, key: dict, rate=None, computed=None) -> None:
        self.path = path
        self.n_pairs = n_pairs
        self.key = key
        self.rate = np.zeros(n_pairs, dtype=np.float32) if rate is None else rate
        self.computed = (
            np.zeros(n_pairs, dtype=bool) if computed is None else computed
        )
        self._dirty = False

    # ---- persistence -------------------------------------------------------

    @classmethod
    def load(cls, video: Path, info, params: FlowParams) -> RollRateCache:
        """Load the cache for ``video``, or an empty one if absent or stale."""
        directory = ensure_cache_dir_for(video)
        path = directory / CACHE_FILENAME
        n_pairs = max(0, info.frame_count - 1)
        key = _cache_key(video, info, params)

        if path.exists():
            try:
                with np.load(path, allow_pickle=False) as data:
                    stored_key = json.loads(bytes(data["meta"]).decode("utf-8"))
                    if stored_key == key and len(data["rate"]) == n_pairs:
                        return cls(path, n_pairs, key, data["rate"], data["computed"])
                    log.debug("roll-rate cache %s is stale, starting fresh", path)
            except Exception as exc:  # noqa: BLE001 - a corrupt cache must never be fatal
                log.warning("ignoring unreadable roll-rate cache %s: %s", path, exc)
        return cls(path, n_pairs, key)

    def save(self) -> Path | None:
        """Write the cache if anything changed. Returns the path, or ``None``."""
        if not self._dirty:
            return None
        meta = np.frombuffer(json.dumps(self.key).encode("utf-8"), dtype=np.uint8)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, rate=self.rate, computed=self.computed, meta=meta)
        tmp.replace(self.path)
        self._dirty = False
        return self.path

    # ---- content ------------------------------------------------------------

    @property
    def coverage(self) -> float:
        """Fraction of the whole video already computed, for reporting."""
        return float(self.computed.mean()) if self.n_pairs else 0.0

    def store(self, first: int, values: np.ndarray) -> None:
        """Record ``values`` for the pairs starting at ``first``."""
        if values.size == 0:
            return
        last = first + values.size
        self.rate[first:last] = values.astype(np.float32)
        self.computed[first:last] = True
        self._dirty = True

    def slice(self, first: int, last: int) -> np.ndarray:
        """Values for pairs ``[first, last]`` inclusive, as float64 for the maths."""
        return self.rate[first : last + 1].astype(np.float64)

    def missing_ranges(
        self, first: int, last: int, *, merge_gap: int = 0
    ) -> list[tuple[int, int]]:
        """Inclusive ``[a, b]`` pair ranges inside ``[first, last]`` still to compute.

        Ranges separated by fewer than ``merge_gap`` already-computed pairs are
        returned merged into one, so the caller decodes straight through a short
        hole rather than paying for an extra seek (see
        :data:`DEFAULT_MERGE_GAP_SECONDS`).
        """
        first = max(0, first)
        last = min(self.n_pairs - 1, last)
        if last < first:
            return []

        todo = ~self.computed[first : last + 1]
        if not todo.any():
            return []

        # Run-length boundaries of the True (== missing) stretches.
        edges = np.flatnonzero(np.diff(np.concatenate(([0], todo.view(np.int8), [0]))))
        ranges = [
            (int(start) + first, int(end) - 1 + first)
            for start, end in zip(edges[0::2], edges[1::2], strict=True)
        ]

        merged: list[tuple[int, int]] = []
        for start, end in ranges:
            if merged and start - merged[-1][1] - 1 <= merge_gap:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return merged


def _cache_key(video: Path, info, params: FlowParams) -> dict:
    """Identity of the cached data: bust it if the video or the parameters change."""
    stat = Path(video).stat()
    return {
        "version": FLOW_CACHE_VERSION,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "frame_count": info.frame_count,
        "frame_rate": [info.frame_rate.numerator, info.frame_rate.denominator],
        **params.as_dict(),
    }
