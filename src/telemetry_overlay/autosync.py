"""Estimate the video/log time log delay automatically.

The camera clock is unreliable, so the log delay normally comes from the user. This
module offers a starting guess instead: it measures how fast the *image* rotates
(optical flow between consecutive frames) and cross-correlates that with the roll rate
the flight controller recorded. The lag of the correlation peak is the log delay.

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

from .flow_cache import DEFAULT_MERGE_GAP_SECONDS, FlowParams, RollRateCache
from .telemetry import fields
from .telemetry.model import TelemetryLog
from .video.probe import VideoInfo, probe_video

log = logging.getLogger(__name__)

#: Forced on the two diagnostic-plot functions below, regardless of any matplotlib
#: style/rcParams active in the process (a system-wide dark ``matplotlibrc``, or a
#: style another part of the app may have set). These are saved PNGs read back by
#: ``ZoomableImageView`` inside a dark-themed window; without pinning the colours
#: explicitly, an ambient dark style renders black/dark-grey text and lines on a
#: dark or transparent background -- readable nowhere.
_LIGHT_PLOT_STYLE = {
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "black",
    "axes.labelcolor": "black",
    "text.color": "black",
    "xtick.color": "black",
    "ytick.color": "black",
    "grid.color": "#b0b0b0",
    "legend.facecolor": "white",
    "legend.edgecolor": "black",
    "legend.labelcolor": "black",
}


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
    log_delay: float
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
    #: Log delays searched, in seconds, relative to the log's own timeline.
    search_min: float | None = None
    search_max: float | None = None
    #: Tracked feature points per frame.
    max_features: int = 250
    #: Keep the raw video/log roll-rate signals on the result, for plotting.
    collect_diagnostics: bool = False


def estimate_log_delay(
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

    video_rate, first_pair, analysed, frames = _video_roll_rate(
        Path(video), info, options, progress
    )
    gate = _quality_gate(video_rate, analysed, frames)
    if gate is not None:
        return gate

    return _correlate(
        video_rate, info, roll, options, analysed, frames,
        video_time0=_pair_time(first_pair, info),
    )


def _quality_gate(
    video_rate: np.ndarray, analysed: float, frames: int
) -> AutoSyncResult | None:
    """An early-out result for a slice not worth correlating, or ``None`` to proceed."""
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
    return None


@dataclass
class SyncFitOptions:
    """How to spread the per-window correlations that :func:`estimate_sync` fits."""

    #: Video span to spread windows across.
    start: float = 0.0
    end: float | None = None
    #: Number of windows. 1 disables the scale fit (behaves like ``estimate_log_delay``).
    n_windows: int = 6
    #: Duration of each window, in seconds.
    window_length: float = 20.0
    #: Log delays searched, in seconds, relative to the log's own timeline. Applied to
    #: every window.
    search_min: float | None = None
    search_max: float | None = None
    analysis_width: int = 480
    max_features: int = 250
    collect_diagnostics: bool = False
    #: Minimum number of trustworthy windows needed to fit a scale. A 2-point line fits
    #: any two windows exactly (zero residual by construction), so it cannot tell a real
    #: fit from two coincidentally-matching false peaks -- 3 is the minimum that leaves
    #: a degree of freedom for the residual check below to mean anything.
    min_windows_for_scale: int = 3
    #: Minimum fraction of the requested span the used windows must cover for the
    #: slope (scale) to be trusted.
    min_span_fraction: float = 0.25
    #: Residual standard deviation, in seconds, above which the fit is flagged.
    max_residual_std: float = 0.15
    #: Clock drift this large is physically implausible for a few minutes of footage
    #: (real values seen so far are ~1e-3); a fit landing outside this is almost
    #: certainly a mismatch between windows, not real drift.
    max_scale_deviation: float = 0.05
    #: How far (in seconds) a window's log delay may sit from the RANSAC line and still
    #: count as agreeing with it.
    ransac_tolerance: float = 0.2


@dataclass
class WindowFit:
    start: float
    result: AutoSyncResult
    used: bool = False


@dataclass
class SyncFitResult:
    log_delay: float
    scale: float
    windows: list[WindowFit]
    residual_std: float
    span_covered: float
    warning: str = ""
    #: Video/log roll-rate over the *whole* analysed span (one continuous optical-flow
    #: pass, see :func:`estimate_sync`), for plotting -- not just the best window's
    #: slice. Present only when ``collect_diagnostics`` was requested.
    diagnostics: AutoSyncDiagnostics | None = None

    @property
    def used(self) -> list[WindowFit]:
        return [w for w in self.windows if w.used]

    @property
    def trustworthy(self) -> bool:
        return not self.warning


def estimate_sync(
    video: str | Path,
    telemetry: TelemetryLog,
    options: SyncFitOptions | None = None,
    *,
    info: VideoInfo | None = None,
    progress: Callable[[float], None] | None = None,
) -> SyncFitResult:
    """Estimate both log delay and clock-drift scale.

    Runs the optical flow *once*, continuously across the whole requested span, then
    picks the ``n_windows`` sub-slices where the image actually rotates the most --
    footage of calm cruise or straight legs cannot correlate against anything, so there
    is no point spending a window on it. Each selected slice is correlated against the
    log (the same :func:`_correlate` used by :func:`estimate_log_delay`); the results
    are then combined by :func:`_fit_sync`, which fits ``log_delay_i = log_delay +
    (scale - 1) * start_i`` through whichever slices agree with each other. A single
    window (``n_windows == 1``) degrades to the old, single-slice behaviour with
    ``scale`` fixed at 1.0.
    """
    options = options or SyncFitOptions()
    info = info or probe_video(video)
    roll = telemetry.channel(fields.ROLL)
    if roll is None or len(roll) < 10:
        return SyncFitResult(0.0, 1.0, [], 0.0, 0.0, "the log has no attitude data")

    end = options.end if options.end is not None else info.duration
    n_windows = max(1, options.n_windows)

    scan_options = AutoSyncOptions(
        window=max(0.0, end - options.start),
        start=options.start,
        analysis_width=options.analysis_width,
        search_min=options.search_min,
        search_max=options.search_max,
        max_features=options.max_features,
        collect_diagnostics=options.collect_diagnostics,
    )
    video_rate, first_pair, analysed, frames = _video_roll_rate(
        Path(video), info, scan_options, progress
    )
    gate = _quality_gate(video_rate, analysed, frames)
    if gate is not None:
        return SyncFitResult(0.0, 1.0, [], 0.0, 0.0, gate.warning)

    diagnostics = None
    if options.collect_diagnostics:
        dt_diag = info.frame_interval
        log_times, log_roll, log_rate = _log_roll_signal(roll, dt_diag)
        video_times = _pair_times(first_pair, len(video_rate), info)
        diagnostics = AutoSyncDiagnostics(
            video_times=video_times,
            video_roll_rate=video_rate,
            log_times=log_times,
            log_roll=log_roll,
            log_roll_rate=log_rate,
        )

    dt = info.frame_interval
    if n_windows == 1:
        window_start = _pair_time(first_pair, info)
        result = _correlate(
            video_rate, info, roll, scan_options, analysed, frames,
            video_time0=window_start,
        )
        windows = [WindowFit(start=window_start, result=result, used=result.trustworthy)]
        return SyncFitResult(
            log_delay=result.log_delay,
            scale=1.0,
            windows=windows,
            residual_std=0.0,
            span_covered=0.0,
            warning=result.warning,
            diagnostics=diagnostics,
        )

    length = max(1, int(round(options.window_length / dt)))
    indices = _select_active_windows(video_rate, length, n_windows, min_gap=length)

    windows = []
    for idx in indices:
        window_start = _pair_time(first_pair + idx, info)
        slice_rate = video_rate[idx : idx + length]
        window_options = AutoSyncOptions(
            window=options.window_length,
            start=window_start,
            search_min=options.search_min,
            search_max=options.search_max,
            # The whole-span diagnostics above already covers this: no need to collect
            # a redundant per-window slice.
            collect_diagnostics=False,
        )
        window_analysed = len(slice_rate) * dt
        window_gate = _quality_gate(slice_rate, window_analysed, len(slice_rate))
        result = window_gate or _correlate(
            slice_rate, info, roll, window_options, window_analysed, len(slice_rate),
            video_time0=window_start,
        )
        windows.append(WindowFit(start=window_start, result=result))

    return _fit_sync(windows, options, diagnostics)


def _select_active_windows(
    video_rate: np.ndarray, length: int, n_windows: int, *, min_gap: int
) -> list[int]:
    """Start indices of the ``n_windows`` slices of ``video_rate`` with the most energy.

    Picks greedily by descending sliding RMS, skipping any candidate closer than
    ``min_gap`` samples to one already chosen, so the picks stay non-overlapping and
    spread out (needed for the scale fit) instead of clustering on one long manoeuvre.
    """
    if len(video_rate) < length:
        return [0]
    sq = video_rate.astype(np.float64) ** 2
    cumsum = np.cumsum(np.insert(sq, 0, 0.0))
    energy = cumsum[length:] - cumsum[:-length]
    order = np.argsort(energy)[::-1]

    chosen: list[int] = []
    for idx in order:
        idx = int(idx)
        if all(abs(idx - c) >= min_gap for c in chosen):
            chosen.append(idx)
        if len(chosen) >= n_windows:
            break
    return sorted(chosen)


def _fit_sync(
    windows: list[WindowFit],
    options: SyncFitOptions,
    diagnostics: AutoSyncDiagnostics | None = None,
) -> SyncFitResult:
    trustworthy = [w for w in windows if w.result.trustworthy]
    if len(trustworthy) < options.min_windows_for_scale:
        # Prefer an actually-trustworthy window over one with a merely higher raw
        # correlation: a low-confidence window can score higher than a distinct,
        # trustworthy peak elsewhere (see the non-stationary-log case this guards
        # against in `_sliding_normalised_correlation`).
        best = max(trustworthy or windows, key=lambda w: w.result.correlation)
        best.used = best.result.trustworthy
        return SyncFitResult(
            log_delay=best.result.log_delay,
            scale=1.0,
            windows=windows,
            residual_std=0.0,
            span_covered=0.0,
            warning=(
                f"only {len(trustworthy)} trustworthy window(s) (need "
                f"{options.min_windows_for_scale}): scale kept at 1.0, log delay from "
                "the single best window -- try --windows more spread out, a longer "
                "--window-length, or a span with more manoeuvres"
            ),
            diagnostics=diagnostics,
        )

    starts = np.array([w.start for w in trustworthy])
    delays = np.array([w.result.log_delay for w in trustworthy])
    weights = np.array([w.result.correlation for w in trustworthy])

    inliers = _ransac_inliers(starts, delays, options)
    if inliers is None or int(inliers.sum()) < options.min_windows_for_scale:
        best = max(trustworthy, key=lambda w: w.result.correlation)
        best.used = True
        agreement = 0 if inliers is None else int(inliers.sum())
        return SyncFitResult(
            log_delay=best.result.log_delay,
            scale=1.0,
            windows=windows,
            residual_std=0.0,
            span_covered=0.0,
            warning=(
                "no consistent (log delay, scale) line fits enough trustworthy windows "
                f"(best agreement: {agreement} of {len(trustworthy)}): they likely "
                "matched different manoeuvres, not the same one at different times -- "
                "scale kept at 1.0, log delay from the single best window"
            ),
            diagnostics=diagnostics,
        )

    for w, is_inlier in zip(trustworthy, inliers):
        w.used = bool(is_inlier)

    slope, intercept = np.polyfit(starts[inliers], delays[inliers], 1, w=weights[inliers])
    scale = 1.0 + float(slope)
    log_delay = float(intercept)

    residuals = delays[inliers] - (intercept + slope * starts[inliers])
    residual_std = float(np.sqrt(np.average(residuals**2, weights=weights[inliers])))

    requested_span = max(w.start for w in windows) - min(w.start for w in windows)
    span_covered = float(starts[inliers].max() - starts[inliers].min())
    span_fraction = span_covered / requested_span if requested_span > 0 else 0.0

    warning = ""
    if abs(scale - 1.0) > options.max_scale_deviation:
        warning = (
            f"fitted scale {scale:.5f} is not physically plausible for clock drift: "
            "the agreeing windows likely matched the same wrong manoeuvre -- check the "
            "per-window table by eye"
        )
    elif span_fraction < options.min_span_fraction:
        warning = (
            f"the windows that agree with each other only cover {span_covered:.0f}s of "
            f"the {requested_span:.0f}s span: the scale estimate is likely unreliable -- "
            "spread --from/--to wider or add windows"
        )
    elif residual_std > options.max_residual_std:
        warning = (
            f"the fit does not line up well (residual {residual_std * 1000:.0f}ms): "
            "check the per-window table, one window may be off -- verify by eye"
        )

    return SyncFitResult(
        log_delay=log_delay,
        scale=scale,
        windows=windows,
        residual_std=residual_std,
        span_covered=span_covered,
        warning=warning,
        diagnostics=diagnostics,
    )


def _ransac_inliers(
    starts: np.ndarray, delays: np.ndarray, options: SyncFitOptions
) -> np.ndarray | None:
    """Boolean mask of the trustworthy windows lying on the best-supported line.

    A window can be individually trustworthy (a distinct, confident correlation peak)
    and still have matched the wrong occurrence of a repeating manoeuvre -- especially
    once window placement is biased towards the most active parts of the video, where
    similar-looking manoeuvres cluster. Fitting one line through every trustworthy point
    (a plain weighted regression) would let a single such mismatch corrupt the whole
    scale estimate. Instead, try every pair of points, count how many of the rest agree
    with the line through that pair within ``ransac_tolerance``, and keep the
    best-supported line's agreeing points. Cheap: with a handful of windows this is at
    most a few dozen pairs.
    """
    n = len(starts)
    best_inliers: np.ndarray | None = None
    best_count = 0
    for i in range(n):
        for j in range(i + 1, n):
            if starts[j] == starts[i]:
                continue
            slope = (delays[j] - delays[i]) / (starts[j] - starts[i])
            if abs(slope) > options.max_scale_deviation:
                continue
            intercept = delays[i] - slope * starts[i]
            residuals = np.abs(delays - (intercept + slope * starts))
            inliers = residuals <= options.ransac_tolerance
            count = int(inliers.sum())
            if count > best_count:
                best_count = count
                best_inliers = inliers
    return best_inliers


def save_fit_plot(
    result: SyncFitResult,
    output_dir: Path,
    *,
    filename: str = "autosync_fit.png",
) -> Path:
    """Per-window log delay vs. window start, with the fitted line.

    Makes the drift the scale corrects for visible: if the points trend up or down
    instead of sitting flat, ``scale`` is not 1.0 and the plot shows by how much.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(_LIGHT_PLOT_STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))

        used = [w for w in result.windows if w.used]
        unused = [w for w in result.windows if not w.used]
        if unused:
            ax.scatter(
                [w.start for w in unused],
                [w.result.log_delay for w in unused],
                color="tab:gray",
                marker="x",
                label="discarded window",
            )
        if used:
            ax.scatter(
                [w.start for w in used],
                [w.result.log_delay for w in used],
                color="tab:blue",
                label="trustworthy window",
            )
        if len(used) >= 2:
            starts = np.array([w.start for w in result.windows])
            lo, hi = float(starts.min()), float(starts.max())
            xs = np.linspace(lo, hi, 50)
            ax.plot(
                xs,
                result.log_delay + (result.scale - 1.0) * xs,
                color="tab:orange",
                label=f"fit (scale={result.scale:.5f})",
            )

        ax.set_title("Per-window log delay vs. video time")
        ax.set_xlabel("video window start [s]")
        ax.set_ylabel("estimated log delay [s]")
        ax.grid(True, alpha=0.3)
        ax.legend()

        fig.tight_layout()
        path = output_dir / filename
        # 300dpi (vs. matplotlib's 150 default): the GUI's ZoomableImageView lets the
        # user zoom well past 1:1, and a 150dpi PNG turns visibly blocky under that.
        fig.savefig(path, dpi=300)
        plt.close(fig)
    return path


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

    Unlike :func:`estimate_log_delay` this does not search for a log delay -- the caller
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

    video_rate, first_pair, _, _ = _video_roll_rate(Path(video), info, options, progress)
    dt = info.frame_interval
    log_times, log_roll, log_rate = _log_roll_signal(roll, dt)
    video_times = _pair_times(first_pair, len(video_rate), info)
    return AutoSyncDiagnostics(
        video_times=video_times,
        video_roll_rate=video_rate,
        log_times=log_times,
        log_roll=log_roll,
        log_roll_rate=log_rate,
    )


# ---------------------------------------------------------------------------


def _pair_time(pair_index: int, info: VideoInfo) -> float:
    """Video time attributed to one frame pair: the later of its two frames.

    Anchored to the frame index rather than to whatever ``start`` the caller asked
    for, so a given pair carries the same timestamp no matter which request
    produced it -- required now that values are cached and reused across requests.
    """
    return (pair_index + 1) * info.frame_interval


def _pair_times(first_pair: int, count: int, info: VideoInfo) -> np.ndarray:
    return (first_pair + 1 + np.arange(count)) * info.frame_interval


def _pair_range(info: VideoInfo, options: AutoSyncOptions) -> tuple[int, int]:
    """Inclusive frame-pair index range covered by ``options``' time window."""
    n_pairs = max(0, info.frame_count - 1)
    if n_pairs == 0:
        return 0, -1
    first = min(max(0, info.frame_index_at(options.start)), n_pairs - 1)
    span = int(round(options.window * float(info.frame_rate)))
    return first, min(n_pairs - 1, first + span - 1)


def _video_roll_rate(
    path: Path,
    info: VideoInfo,
    options: AutoSyncOptions,
    progress: Callable[[float], None] | None,
) -> tuple[np.ndarray, int, float, int]:
    """Per-frame image rotation rate in degrees/second, computing only what is new.

    Returns ``(rate, first_pair, analysed_seconds, frames)``. Values already in the
    on-disk cache are reused; only the pairs never computed before are decoded (see
    :mod:`telemetry_overlay.flow_cache`), so overlapping or repeated requests --
    autosync and manualsync over the same footage, most obviously -- cost nothing
    the second time.
    """
    first, last = _pair_range(info, options)
    if last < first:
        return np.array([], dtype=np.float64), first, 0.0, 0

    params = FlowParams(options.analysis_width, options.max_features)
    cache = RollRateCache.load(path, info, params)
    merge_gap = int(round(DEFAULT_MERGE_GAP_SECONDS * float(info.frame_rate)))
    todo = cache.missing_ranges(first, last, merge_gap=merge_gap)

    if todo:
        missing = sum(b - a + 1 for a, b in todo)
        log.info(
            "optical flow: %d of %d frame pairs already cached, computing %d",
            (last - first + 1) - missing,
            last - first + 1,
            missing,
        )
        _fill_missing(cache, path, info, options, todo, progress)
        cache.save()
    elif progress:
        progress(1.0)

    rate = cache.slice(first, last)
    return rate, first, len(rate) * info.frame_interval, len(rate)


def flow_is_cached(video: str | Path, info: VideoInfo, start: float, end: float) -> bool:
    """Whether every optical-flow value for ``[start, end]`` is already on disk.

    Lets a caller (the GUI, on opening a tab) decide whether running the analysis
    right now would cost nothing -- reading the cache header is cheap, decoding
    video is not. Uses the same default analysis parameters ``estimate_log_delay``/
    ``estimate_sync``/``compute_manual_diagnostics`` do, since none of the GUI tabs
    expose ``analysis_width``/``max_features`` as options.
    """
    options = AutoSyncOptions(start=start, window=max(0.0, end - start))
    first, last = _pair_range(info, options)
    if last < first:
        return True
    params = FlowParams(options.analysis_width, options.max_features)
    cache = RollRateCache.load(Path(video), info, params)
    return not cache.missing_ranges(first, last)


def _fill_missing(
    cache: RollRateCache,
    path: Path,
    info: VideoInfo,
    options: AutoSyncOptions,
    ranges: list[tuple[int, int]],
    progress: Callable[[float], None] | None,
) -> None:
    """Compute and store each missing range, reporting one continuous progress bar."""
    total = sum(b - a + 1 for a, b in ranges)
    done = 0
    for a, b in ranges:
        count = b - a + 1

        def report(fraction: float, base: int = done, size: int = count) -> None:
            if progress:
                progress((base + fraction * size) / total)

        cache.store(a, _compute_pair_range(path, info, a, b, options, report))
        done += count
    if progress:
        progress(1.0)


def _compute_pair_range(
    path: Path,
    info: VideoInfo,
    first_pair: int,
    last_pair: int,
    options: AutoSyncOptions,
    progress: Callable[[float], None] | None,
) -> np.ndarray:
    """Image rotation for pairs ``[first_pair, last_pair]``, in degrees per second.

    Decodes frames ``first_pair`` through ``last_pair + 1``: pair ``p`` is the
    rotation between frames ``p`` and ``p + 1``, which makes every pair
    independently computable and a range filled next to existing data seam-free.
    """
    import cv2

    from .video.reader import FrameReader

    scale = options.analysis_width / info.width
    count = last_pair - first_pair + 1
    rates = np.full(count, np.nan, dtype=np.float64)
    previous: np.ndarray | None = None
    produced = 0
    feature_args = {
        "maxCorners": options.max_features,
        "qualityLevel": 0.01,
        "minDistance": 8,
    }

    # A fresh reader per range, deliberately: reusing one container across several
    # seeks while a Qt event loop runs in the same process is the pathology
    # documented in CLAUDE.md. Ranges are few and long, so the extra container
    # opens cost nothing next to the decoding.
    with FrameReader(path, info) as reader:
        for index in range(first_pair, last_pair + 2):
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
                rates[produced] = _rotation_between(cv2, previous, small, feature_args)
                produced += 1
            previous = small
            if progress and index % 30 == 0:
                progress((index - first_pair) / max(1, count))

    rates = rates[:produced] * float(info.frame_rate)
    # A failed frame pair yields NaN; fill so the correlation stays defined. This is
    # a real result ("nothing trackable here"), so it is cached rather than retried.
    nans = ~np.isfinite(rates)
    if nans.any():
        rates[nans] = 0.0
    return rates


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
    *,
    video_time0: float,
) -> AutoSyncResult:
    """Slide the log's roll rate against the video's and take the best lag.

    ``video_time0`` is the video time of ``video_rate[0]``, and every video-time
    quantity here is measured from it: the reported log delay, the search-range
    translation, and the diagnostics' timestamps. It is passed in rather than taken
    from ``options.start`` because the first sample sits at the *frame* nearest that
    request, not exactly at it -- a sub-frame difference, but one that used to leave
    the returned log delay and the plotted timestamps disagreeing by one frame
    interval.
    """
    dt = info.frame_interval
    log_times, log_roll, log_rate = _log_roll_signal(roll, dt)

    a = _normalise(video_rate)
    if a is None or not np.any(np.abs(log_rate - log_rate.mean()) > 1e-9):
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "one of the signals is flat"
        )

    correlation = _sliding_normalised_correlation(log_rate, a)
    if correlation.size == 0:
        return AutoSyncResult(
            0.0, 0.0, 0.0, analysed, frames, "the analysed window is longer than the log"
        )

    lags = log_times[: correlation.size]
    # Default the search to log delays where the analysed window actually falls inside the
    # log. Without this the peak can land somewhere that leaves the clip hanging off
    # the end of the flight -- an answer that is impossible rather than merely wrong.
    lo, hi = _default_search_range(roll, options)
    # ``lags`` are log times at which the *window* begins, but ``search_min``/``search_max``
    # are documented (and used elsewhere, e.g. ``probe``'s "valid log delays") as bounds on
    # the log delay -- the log time matching *video time zero*. Translate by the window's
    # own start so a fixed pair of bounds means the same thing regardless of where in the
    # clip the window sits (needed for windows spread across a clip in `estimate_sync`).
    if options.search_min is not None:
        lo = options.search_min + video_time0
    if options.search_max is not None:
        hi = options.search_max + video_time0
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

    log_delay = float(lags[best] - video_time0)
    warning = ""
    if peak < 0.35:
        warning = "weak correlation: the estimate is probably meaningless"
    elif confidence < 1.5:
        warning = "several candidate log delays score alike: verify by eye"

    diagnostics = None
    if options.collect_diagnostics:
        video_times = video_time0 + dt * np.arange(len(video_rate))
        diagnostics = AutoSyncDiagnostics(
            video_times=video_times,
            video_roll_rate=video_rate,
            log_times=log_times,
            log_roll=log_roll,
            log_roll_rate=log_rate,
        )

    return AutoSyncResult(
        log_delay=log_delay,
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
            f"no log data between {xlim[0]:.1f}s and {xlim[1]:.1f}s: check --log-delay, "
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
    log_delay: float,
    output_dir: Path,
    *,
    scale: float = 1.0,
    filename: str = "autosync_diagnostics.png",
    xlim: tuple[float, float] | None = None,
) -> Path:
    """Save the signals behind a sync estimate as one two-panel PNG.

    ``log_delay``/``scale`` shift the video times onto the log's timebase (``log_time =
    log_delay + video_time * scale``), so the two roll-rate traces in the bottom panel
    line up the way the estimate claims they do. ``xlim``, in log seconds, crops both
    panels to a range of interest (e.g. the slice a manual sync was checked against).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(_LIGHT_PLOT_STYLE):
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
            log_delay + diagnostics.video_times * scale,
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
                    (log_delay + diagnostics.video_times * scale, diagnostics.video_roll_rate),
                ],
                xlim,
            )

        fig.tight_layout()
        path = output_dir / filename
        fig.savefig(path, dpi=300)
        plt.close(fig)
    return path


def _normalise(signal: np.ndarray) -> np.ndarray | None:
    """Zero-mean, unit-variance, so the correlation is a correlation coefficient."""
    centred = signal - signal.mean()
    norm = np.sqrt(np.mean(centred**2))
    if norm < 1e-9:
        return None
    return centred / norm


def _sliding_normalised_correlation(log_rate: np.ndarray, a: np.ndarray) -> np.ndarray:
    """Pearson correlation of ``a`` (already zero-mean/unit-variance) against every
    length-``len(a)`` slice of ``log_rate``, normalised *locally* per slice.

    A flight log is rarely stationary: a calm cruise and a violent aerobatic segment can
    sit in the same file. Normalising ``log_rate`` once, globally, before correlating (as
    a plain ``np.correlate`` would) lets the highest-amplitude segment of the log
    dominate the raw dot product at *every* lag, regardless of true alignment -- the
    correlation score ends up systematically pulled towards that segment no matter which
    part of the video is being matched. Normalising per-slice (i.e. by that slice's own
    standard deviation) keeps the score a true correlation coefficient, bounded to
    [-1, 1], comparable across lags independent of local signal energy.
    """
    length = len(a)
    if len(log_rate) < length:
        return np.array([])

    dot = np.correlate(log_rate, a, mode="valid")
    cumsum = np.cumsum(np.insert(log_rate.astype(np.float64), 0, 0.0))
    cumsum2 = np.cumsum(np.insert(log_rate.astype(np.float64) ** 2, 0, 0.0))
    total = cumsum[length:] - cumsum[:-length]
    total_sq = cumsum2[length:] - cumsum2[:-length]
    mean = total / length
    variance = np.maximum(total_sq / length - mean**2, 0.0)
    std = np.sqrt(variance)

    # ``a`` is zero-mean, so subtracting the slice's mean from ``log_rate`` before the dot
    # product would not change it; only the local std is needed to turn the raw dot
    # product into a coefficient.
    correlation = np.zeros_like(dot)
    valid = std > 1e-9
    correlation[valid] = dot[valid] / (length * std[valid])
    return correlation
