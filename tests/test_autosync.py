"""Cross-correlation of the automatic sync, on signals with a known offset."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from telemetry_overlay.autosync import AutoSyncOptions, _correlate, _normalise
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


class TestNormalise:
    def test_zero_mean_unit_variance(self):
        out = _normalise(np.array([1.0, 2.0, 3.0]))
        assert out is not None
        assert out.mean() == pytest.approx(0.0)
        assert np.sqrt((out**2).mean()) == pytest.approx(1.0)

    def test_constant_signal_returns_none(self):
        assert _normalise(np.ones(10)) is None
