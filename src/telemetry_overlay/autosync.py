"""Estimate the video/log time offset automatically.

The camera clock is unreliable, so the offset normally comes from the user. This module
offers a starting guess instead: it measures how fast the *image* rotates (optical flow
between consecutive frames) and cross-correlates that with the roll rate the flight
controller recorded. The lag of the correlation peak is the offset.

It is a suggestion, never applied on its own. Cases where it fails are real and easy to
hit: footage of empty sky has nothing to track, straight and level flight has no signal
to correlate, and a gimballed camera does not roll with the airframe. The returned
confidence exists to make those cases visible rather than silently wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .telemetry import fields
from .telemetry.model import TelemetryLog
from .video.probe import VideoInfo, probe_video

log = logging.getLogger(__name__)


@dataclass
class AutoSyncDiagnostics:
    """Raw signals behind an :class:`AutoSyncResult`, for plotting."""

    video_times: np.ndarray
    video_roll_rate: np.ndarray
    log_times: np.ndarray
    log_roll: np.ndarray
    log_roll_rate: np.ndarray


@dataclass
class AutoSyncResult:
    offset: float
    #: Peak-to-runner-up ratio of the correlation. Above ~1.5 the peak is distinct;
    #: near 1.0 the correlation is flat and the answer is meaningless.
    confidence: float
    #: Correlation coefficient at the peak, in -1..1.
    correlation: float
    #: Video seconds actually analysed.
    analysed: float
    samples: int
    #: Set when the result should not be trusted, with the reason.
    warning: str = ""
    #: Present when the caller asked for the raw signals (see ``AutoSyncOptions.collect_diagnostics``).
    diagnostics: AutoSyncDiagnostics | None = None

    @property
    def trustworthy(self) -> bool:
        return not self.warning and self.confidence >= 1.5 and self.correlation >= 0.35


@dataclass
class AutoSyncOptions:
    #: Video window to analyse, in seconds. Longer is more reliable and slower.
    window: float = 60.0
    #: Where in the clip to start analysing.
    start: float = 0.0
    #: Frames are downscaled to this width before tracking; the rotation of the whole
    #: image is a large-scale signal, so detail is not needed.
    analysis_width: int = 480
    #: Offsets searched, in seconds, relative to the log's own timeline.
    search_min: float | None = None
    search_max: float | None = None
    #: Tracked feature points per frame.
    max_features: int = 250
    #: Keep the raw video/log roll-rate signals on the result, for plotting.
    collect_diagnostics: bool = False


def estimate_offset(
    video: str | Path,
    telemetry: TelemetryLog,
    options: AutoSyncOptions | None = None,
    *,
    info: VideoInfo | None = None,
    progress: Callable[[float], None] | None = None,
) -> AutoSyncResult:
    """Estimate the log time corresponding to video time zero."""
    options = options or AutoSyncOptions()
    info = info or probe_video(video)
    roll = telemetry.channel(fields.ROLL)
    if roll is None or len(roll) < 10:
        return AutoSyncResult(0.0, 0.0, 0.0, 0.0, 0, "the log has no attitude data")

    video_rate, analysed, frames = _video_roll_rate(
        Path(video), info, options, progress
    )
    if frames < 30:
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "too few frames could be analysed"
        )
    if not np.any(np.abs(video_rate) > 1.0):
        return AutoSyncResult(
            0.0,
            0.0,
            0.0,
            analysed,
            frames,
            "the image barely rotates in this window: pick a section with manoeuvres",
        )

    return _correlate(video_rate, info, roll, options, analysed, frames)


def compute_manual_diagnostics(
    video: str | Path,
    telemetry: TelemetryLog,
    start: float,
    end: float,
    options: AutoSyncOptions | None = None,
    *,
    info: VideoInfo | None = None,
    progress: Callable[[float], None] | None = None,
) -> AutoSyncDiagnostics:
    """Roll and roll-rate signals for a manually chosen video slice, for plotting.

    Unlike :func:`estimate_offset` this does not search for an offset -- the caller
    already has one (from ``manualsync``) and wants to see how well it lines up.
    """
    options = options or AutoSyncOptions()
    options = AutoSyncOptions(
        window=end - start,
        start=start,
        analysis_width=options.analysis_width,
        max_features=options.max_features,
    )
    info = info or probe_video(video)
    roll = telemetry.channel(fields.ROLL)
    if roll is None or len(roll) < 10:
        raise ValueError("the log has no attitude data")

    video_rate, _, _ = _video_roll_rate(Path(video), info, options, progress)
    dt = info.frame_interval
    log_times, log_roll, log_rate = _log_roll_signal(roll, dt)
    video_times = start + dt * (1 + np.arange(len(video_rate)))
    return AutoSyncDiagnostics(
        video_times=video_times,
        video_roll_rate=video_rate,
        log_times=log_times,
        log_roll=log_roll,
        log_roll_rate=log_rate,
    )


# ---------------------------------------------------------------------------


def _video_roll_rate(
    path: Path,
    info: VideoInfo,
    options: AutoSyncOptions,
    progress: Callable[[float], None] | None,
) -> tuple[np.ndarray, float, int]:
    """Per-frame image rotation rate, in degrees per second."""
    import cv2

    from .video.reader import FrameReader

    scale = options.analysis_width / info.width
    first = info.frame_index_at(options.start)
    last = min(
        info.frame_count - 1,
        first + int(round(options.window * float(info.frame_rate))),
    )
    rates: list[float] = []
    previous: np.ndarray | None = None
    feature_args = {
        "maxCorners": options.max_features,
        "qualityLevel": 0.01,
        "minDistance": 8,
    }

    with FrameReader(path, info) as reader:
        for index in range(first, last + 1):
            try:
                frame = reader.frame_at_index(index)
            except IndexError:
                break
            small = cv2.resize(
                frame.to_ndarray(format="gray8"),
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA,
            )
            if previous is not None:
                rates.append(_rotation_between(cv2, previous, small, feature_args))
            previous = small
            if progress and index % 30 == 0:
                progress((index - first) / max(1, last - first))

    rate = np.asarray(rates, dtype=np.float64) * float(info.frame_rate)
    # A failed frame pair yields NaN; fill so the correlation stays defined.
    nans = ~np.isfinite(rate)
    if nans.any():
        rate[nans] = 0.0
    return rate, len(rates) * info.frame_interval, len(rates)


def _rotation_between(cv2, previous, current, feature_args) -> float:
    """Rotation in degrees from one frame to the next, or NaN if untrackable."""
    points = cv2.goodFeaturesToTrack(previous, **feature_args)
    if points is None or len(points) < 12:
        return float("nan")
    moved, status, _ = cv2.calcOpticalFlowPyrLK(previous, current, points, None)
    if moved is None or status is None:
        return float("nan")
    keep = status.ravel() == 1
    if keep.sum() < 12:
        return float("nan")
    matrix, _ = cv2.estimateAffinePartial2D(
        points[keep], moved[keep], method=cv2.RANSAC, ransacReprojThreshold=2.0
    )
    if matrix is None:
        return float("nan")
    # A partial affine is scale*rotation plus translation; recover the angle. This is
    # the rotation of the *scene* between the two frames, which is the airframe's
    # rotation with the sign flipped: when the aircraft rolls right, the camera rolls
    # with it, and stationary ground features appear to rotate the opposite way in the
    # image. Negate so the sign matches ArduPilot's ROLL convention (positive = right
    # wing down) before this gets cross-correlated against the log.
    return -float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))


def _log_roll_signal(roll, dt: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Roll and roll rate resampled onto a fixed-``dt`` grid spanning the whole log."""
    log_times = np.arange(roll.t[0], roll.t[-1], dt)
    log_roll = np.interp(log_times, roll.t, np.unwrap(roll.v, period=360.0))
    log_rate = np.gradient(log_roll, dt)
    return log_times, log_roll, log_rate


def _correlate(
    video_rate: np.ndarray,
    info: VideoInfo,
    roll,
    options: AutoSyncOptions,
    analysed: float,
    frames: int,
) -> AutoSyncResult:
    """Slide the log's roll rate against the video's and take the best lag."""
    dt = info.frame_interval
    log_times, log_roll, log_rate = _log_roll_signal(roll, dt)

    a = _normalise(video_rate)
    b = _normalise(log_rate)
    if a is None or b is None:
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "one of the signals is flat"
        )

    # Correlation of the (shorter) video signal against the whole log.
    correlation = np.correlate(b, a, mode="valid") / len(a)
    if correlation.size == 0:
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "the analysed window is longer than the log"
        )

    lags = log_times[: correlation.size]
    # Default the search to offsets where the analysed window actually falls inside the
    # log. Without this the peak can land somewhere that leaves the clip hanging off
    # the end of the flight -- an answer that is impossible rather than merely wrong.
    lo, hi = _default_search_range(roll, options)
    if options.search_min is not None:
        lo = options.search_min
    if options.search_max is not None:
        hi = options.search_max
    window = (lags >= lo) & (lags <= hi)
    if not window.any():
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "the search range excludes the whole log"
        )
    correlation = np.where(window, correlation, -np.inf)

    best = int(np.argmax(correlation))
    peak = float(correlation[best])
    # Ignore the neighbourhood of the peak when looking for the runner-up: adjacent
    # lags are correlated by construction and would always look like a rival peak.
    guard = max(1, int(round(1.0 / dt)))
    masked = correlation.copy()
    masked[max(0, best - guard) : best + guard + 1] = -np.inf
    finite = masked[np.isfinite(masked)]
    runner_up = float(np.max(finite)) if finite.size else 0.0
    confidence = peak / runner_up if runner_up > 0 else (peak * 10 if peak > 0 else 0.0)

    offset = float(lags[best] - options.start)
    warning = ""
    if peak < 0.35:
        warning = "weak correlation: the estimate is probably meaningless"
    elif confidence < 1.5:
        warning = "several candidate offsets score alike: verify by eye"

    diagnostics = None
    if options.collect_diagnostics:
        video_times = options.start + dt * (1 + np.arange(len(video_rate)))
        diagnostics = AutoSyncDiagnostics(
            video_times=video_times,
            video_roll_rate=video_rate,
            log_times=log_times,
            log_roll=log_roll,
            log_roll_rate=log_rate,
        )

    return AutoSyncResult(
        offset=offset,
        confidence=float(confidence),
        correlation=peak,
        analysed=analysed,
        samples=frames,
        warning=warning,
        diagnostics=diagnostics,
    )


def _default_search_range(roll, options: AutoSyncOptions) -> tuple[float, float]:
    """Log times at which the analysed video window can begin and still be covered."""
    return float(roll.t[0]), float(roll.t[-1] - options.window)


def _autoscale_y(ax, series: list[tuple[np.ndarray, np.ndarray]], xlim: tuple[float, float]) -> None:
    """Set y-limits from the data actually inside ``xlim``, with a small margin."""
    values = [
        y[(x >= xlim[0]) & (x <= xlim[1])]
        for x, y in series
    ]
    non_empty = [v for v in values if v.size]
    if not non_empty:
        raise ValueError(
            f"no log data between {xlim[0]:.1f}s and {xlim[1]:.1f}s: check --offset, "
            "the log covers a different time range"
        )
    values = np.concatenate(non_empty)
    if values.size == 0:
        return
    lo, hi = float(values.min()), float(values.max())
    margin = (hi - lo) * 0.1 or 1.0
    ax.set_ylim(lo - margin, hi + margin)


def save_diagnostic_plots(
    diagnostics: AutoSyncDiagnostics,
    offset: float,
    output_dir: Path,
    *,
    filename: str = "autosync_diagnostics.png",
    xlim: tuple[float, float] | None = None,
) -> Path:
    """Save the signals behind a sync estimate as one two-panel PNG.

    ``offset`` shifts the video times onto the log's timebase (``log_time = offset +
    video_time``), so the two roll-rate traces in the bottom panel line up the way the
    estimate claims they do. ``xlim``, in log seconds, crops both panels to a range of
    interest (e.g. the slice a manual sync was checked against).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    fig, (ax_roll, ax_rate) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

    ax_roll.plot(diagnostics.log_times, diagnostics.log_roll, linewidth=0.8)
    ax_roll.set_title("Log roll")
    ax_roll.set_ylabel("roll [deg]")
    ax_roll.grid(True, alpha=0.3)

    ax_rate.plot(
        diagnostics.log_times,
        diagnostics.log_roll_rate,
        linewidth=0.8,
        color="tab:blue",
        label="log roll rate",
    )
    ax_rate.plot(
        diagnostics.video_times + offset,
        diagnostics.video_roll_rate,
        linewidth=0.8,
        color="tab:orange",
        label="video roll rate",
    )
    ax_rate.set_title("Log vs. video roll rate")
    ax_rate.set_xlabel("log time [s]")
    ax_rate.set_ylabel("roll rate [deg/s]")
    ax_rate.grid(True, alpha=0.3)
    ax_rate.legend()

    if xlim is not None:
        ax_rate.set_xlim(xlim)
        _autoscale_y(ax_roll, [(diagnostics.log_times, diagnostics.log_roll)], xlim)
        _autoscale_y(
            ax_rate,
            [
                (diagnostics.log_times, diagnostics.log_roll_rate),
                (diagnostics.video_times + offset, diagnostics.video_roll_rate),
            ],
            xlim,
        )

    fig.tight_layout()
    path = output_dir / filename
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _normalise(signal: np.ndarray) -> np.ndarray | None:
    """Zero-mean, unit-variance, so the correlation is a correlation coefficient."""
    centred = signal - signal.mean()
    norm = np.sqrt(np.mean(centred**2))
    if norm < 1e-9:
        return None
    return centred / norm
