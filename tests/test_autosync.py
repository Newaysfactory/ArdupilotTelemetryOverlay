"""Cross-correlation of the automatic sync, on signals with a known offset."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from telemetry_overlay.autosync import (
    AutoSyncOptions,
    AutoSyncResult,
    SyncFitOptions,
    WindowFit,
    _correlate,
    _fit_sync,
    _normalise,
    _select_active_windows,
)
from telemetry_overlay.telemetry.model import Channel
from telemetry_overlay.units import ANGLE
from telemetry_overlay.video.probe import VideoInfo


def fake_info(fps=30) -> VideoInfo:
    return VideoInfo(
        path=Path("fake.mp4"),
        width=1920,
        height=1080,
        frame_rate=Fraction(fps, 1),
        frame_count=fps * 10,
        duration=10.0,
        time_base=Fraction(1, 60000),
        codec="h264",
        pix_fmt="yuv420p",
        color_range=0,
        colorspace=0,
        rotation=0,
        has_audio=False,
        audio_codec=None,
        audio_rate=None,
        creation_time=None,
    )


def roll_channel(t: np.ndarray, roll: np.ndarray) -> Channel:
    return Channel("roll", ANGLE, t, roll, np.ones(t.shape, bool), "ATT.Roll")


def _rolling_flight(duration=120.0, dt=0.02, seed=0):
    """A roll history with enough structure to correlate against.

    Deliberately aperiodic (smoothed noise, not sinusoids): a periodic signal has many
    equally good alignments, which the confidence score is supposed to reject.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(0.0, duration, dt)
    raw = rng.normal(0.0, 1.0, t.size)
    kernel = np.hanning(int(round(1.5 / dt)))
    kernel /= kernel.sum()
    roll = np.convolve(raw, kernel, mode="same") * 400.0
    return t, roll


class TestCorrelate:
    def test_recovers_a_known_offset(self):
        info = fake_info()
        dt = info.frame_interval
        t, roll = _rolling_flight()
        channel = roll_channel(t, roll)

        true_offset = 41.0
        window = 30.0
        video_times = np.arange(0.0, window, dt)
        # What the camera would have seen: the log's roll rate, shifted.
        log_rate = np.gradient(roll, t[1] - t[0])
        video_rate = np.interp(true_offset + video_times, t, log_rate)

        result = _correlate(video_rate, info, channel, AutoSyncOptions(), window, 0)
        assert result.log_delay == pytest.approx(true_offset, abs=0.1)
        assert result.correlation > 0.9
        assert result.trustworthy

    def test_tolerates_noise_and_a_start_offset(self):
        info = fake_info()
        dt = info.frame_interval
        t, roll = _rolling_flight(seed=3)
        channel = roll_channel(t, roll)
        rng = np.random.default_rng(7)

        true_offset = 33.5
        start = 10.0
        window = 30.0
        video_times = np.arange(0.0, window, dt)
        log_rate = np.gradient(roll, t[1] - t[0])
        # The analysis window begins at `start` inside the clip, so the reported offset
        # must still refer to video time zero.
        video_rate = np.interp(
            true_offset + start + video_times, t, log_rate
        ) + rng.normal(0, 20.0, video_times.size)

        options = AutoSyncOptions(start=start, window=window)
        result = _correlate(video_rate, info, channel, options, window, 0)
        assert result.log_delay == pytest.approx(true_offset, abs=0.2)

    def test_flat_signal_is_rejected_not_guessed(self):
        info = fake_info()
        t, roll = _rolling_flight()
        channel = roll_channel(t, roll)
        flat = np.zeros(300)
        result = _correlate(flat, info, channel, AutoSyncOptions(), 10.0, 0)
        assert not result.trustworthy
        assert result.warning

    def test_unrelated_signal_is_flagged(self):
        info = fake_info()
        t, roll = _rolling_flight()
        channel = roll_channel(t, roll)
        rng = np.random.default_rng(11)
        noise = rng.normal(0, 50, 900)
        result = _correlate(noise, info, channel, AutoSyncOptions(), 30.0, 0)
        assert not result.trustworthy

    def test_search_range_is_honoured(self):
        info = fake_info()
        dt = info.frame_interval
        t, roll = _rolling_flight()
        channel = roll_channel(t, roll)
        log_rate = np.gradient(roll, t[1] - t[0])
        video_times = np.arange(0.0, 30.0, dt)
        video_rate = np.interp(41.0 + video_times, t, log_rate)

        options = AutoSyncOptions(search_min=60.0, search_max=80.0)
        result = _correlate(video_rate, info, channel, options, 30.0, 0)
        assert 60.0 <= result.log_delay <= 80.0
        # Forced away from the true peak, the result must not claim to be reliable.
        assert not result.trustworthy


def trustworthy_result(log_delay: float) -> AutoSyncResult:
    return AutoSyncResult(
        log_delay=log_delay,
        confidence=3.0,
        correlation=0.9,
        analysed=20.0,
        samples=600,
    )


def untrustworthy_result() -> AutoSyncResult:
    return AutoSyncResult(0.0, 0.0, 0.0, 20.0, 600, "the image barely rotates")


class TestFitSync:
    def test_recovers_known_delay_and_scale(self):
        true_delay = 414.13
        true_scale = 1.0021
        starts = [0.0, 40.0, 80.0, 120.0, 160.0, 200.0]
        windows = [
            WindowFit(start=s, result=trustworthy_result(true_delay + (true_scale - 1) * s))
            for s in starts
        ]

        result = _fit_sync(windows, SyncFitOptions())

        assert result.log_delay == pytest.approx(true_delay, abs=1e-6)
        assert result.scale == pytest.approx(true_scale, abs=1e-6)
        assert result.trustworthy
        assert len(result.used) == len(starts)

    def test_scale_one_when_no_drift(self):
        starts = [0.0, 50.0, 100.0, 150.0]
        windows = [WindowFit(start=s, result=trustworthy_result(10.0)) for s in starts]

        result = _fit_sync(windows, SyncFitOptions())

        assert result.log_delay == pytest.approx(10.0, abs=1e-6)
        assert result.scale == pytest.approx(1.0, abs=1e-6)
        assert result.trustworthy

    def test_falls_back_when_too_few_trustworthy_windows(self):
        windows = [
            WindowFit(start=0.0, result=trustworthy_result(10.0)),
            WindowFit(start=50.0, result=untrustworthy_result()),
            WindowFit(start=100.0, result=untrustworthy_result()),
        ]

        result = _fit_sync(windows, SyncFitOptions())

        assert result.scale == 1.0
        assert result.log_delay == pytest.approx(10.0)
        assert not result.trustworthy
        assert result.warning

    def test_prefers_a_trustworthy_window_over_a_higher_raw_correlation(self):
        # A non-stationary log can give an untrustworthy window a higher raw
        # correlation than a distinct, trustworthy peak elsewhere (the failure mode
        # `_sliding_normalised_correlation` guards against). The fallback must still
        # report the trustworthy one, not whichever scores highest regardless.
        high_but_ambiguous = AutoSyncResult(
            log_delay=150.0, confidence=1.2, correlation=0.5, analysed=20.0, samples=600,
            warning="several candidate log delays score alike: verify by eye",
        )
        lower_but_trustworthy = AutoSyncResult(
            log_delay=92.0, confidence=2.0, correlation=0.4, analysed=20.0, samples=600,
        )
        windows = [
            WindowFit(start=0.0, result=high_but_ambiguous),
            WindowFit(start=50.0, result=lower_but_trustworthy),
        ]

        result = _fit_sync(windows, SyncFitOptions())

        assert result.log_delay == pytest.approx(92.0)
        assert windows[1].used
        assert not windows[0].used

    def test_ransac_rejects_a_single_outlier_window(self):
        # Four windows on a consistent line, plus one trustworthy window that matched a
        # different manoeuvre entirely (its delay is nowhere near the line the other
        # four agree on). A plain weighted regression through all five would be pulled
        # off by the outlier; RANSAC should find the line the four agree on and exclude
        # the fifth from the fit.
        true_delay = 414.13
        true_scale = 1.0021
        starts = [0.0, 40.0, 80.0, 160.0]
        windows = [
            WindowFit(start=s, result=trustworthy_result(true_delay + (true_scale - 1) * s))
            for s in starts
        ]
        outlier = WindowFit(start=120.0, result=trustworthy_result(250.0))
        windows.insert(3, outlier)

        result = _fit_sync(windows, SyncFitOptions())

        assert result.log_delay == pytest.approx(true_delay, abs=1e-3)
        assert result.scale == pytest.approx(true_scale, abs=1e-3)
        assert result.trustworthy
        assert not outlier.used
        assert all(w.used for w in windows if w is not outlier)

    def test_rejects_windows_with_no_consistent_line(self):
        # Three "trustworthy" windows whose delays do not actually share a common
        # (delay, scale) line -- each matched a different, unrelated manoeuvre. RANSAC
        # must find no pair whose line stays within the plausible-scale bound and
        # therefore refuse to fit a scale at all, rather than reporting whichever line
        # a plain regression would draw through all three.
        windows = [
            WindowFit(start=300.0, result=trustworthy_result(184.487)),
            WindowFit(start=316.7, result=trustworthy_result(173.120)),
            WindowFit(start=366.7, result=trustworthy_result(92.120)),
        ]

        result = _fit_sync(windows, SyncFitOptions())

        assert not result.trustworthy
        assert result.scale == 1.0
        assert "no consistent" in result.warning

    def test_warns_when_used_windows_are_too_close_together(self):
        # Three trustworthy windows clustered near the start, plus untrustworthy ones
        # spread out to 200s: the configured span is wide but the fit only has signal
        # from a small slice of it, so the slope (scale) should not be trusted.
        windows = [
            WindowFit(start=0.0, result=trustworthy_result(10.0)),
            WindowFit(start=5.0, result=trustworthy_result(10.0)),
            WindowFit(start=10.0, result=trustworthy_result(10.0)),
            WindowFit(start=80.0, result=untrustworthy_result()),
            WindowFit(start=140.0, result=untrustworthy_result()),
            WindowFit(start=200.0, result=untrustworthy_result()),
        ]

        result = _fit_sync(windows, SyncFitOptions())

        assert not result.trustworthy
        assert "cover" in result.warning


class TestSelectActiveWindows:
    def test_picks_the_highest_energy_slices(self):
        rate = np.zeros(1000)
        rate[100:120] = 50.0   # a burst of rotation
        rate[500:520] = 80.0   # a bigger burst
        rate[800:820] = 30.0   # a smaller burst

        indices = _select_active_windows(rate, length=20, n_windows=2, min_gap=20)

        # The two strongest bursts, not the weakest one.
        assert any(480 <= i <= 500 for i in indices)
        assert any(80 <= i <= 100 for i in indices)
        assert not any(780 <= i <= 800 for i in indices)

    def test_respects_minimum_gap(self):
        rate = np.zeros(200)
        rate[50:70] = 100.0
        rate[55:65] = 200.0  # an even hotter sub-slice right next to it

        indices = _select_active_windows(rate, length=20, n_windows=5, min_gap=30)

        indices = sorted(indices)
        assert all(b - a >= 30 for a, b in zip(indices, indices[1:]))

    def test_falls_back_to_a_single_window_when_signal_is_shorter_than_length(self):
        rate = np.ones(10)
        assert _select_active_windows(rate, length=20, n_windows=3, min_gap=20) == [0]


class TestNormalise:
    def test_zero_mean_unit_variance(self):
        out = _normalise(np.array([1.0, 2.0, 3.0]))
        assert out is not None
        assert out.mean() == pytest.approx(0.0)
        assert np.sqrt((out**2).mean()) == pytest.approx(1.0)

    def test_constant_signal_returns_none(self):
        assert _normalise(np.ones(10)) is None
