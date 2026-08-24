"""Telemetry resampling, validity handling and the message track."""

from __future__ import annotations

import numpy as np
import pytest

from telemetry_overlay.telemetry.events import MessageTrack, MessageTrackOptions
from telemetry_overlay.telemetry.model import Channel, MessageEvent, TelemetryLog
from telemetry_overlay.telemetry.timeline import (
    SamplingOptions,
    build_timeline,
    debounce,
    sample_channel,
    sample_steps,
)
from telemetry_overlay.units import LENGTH


def channel(t, v, valid=None, name="agl") -> Channel:
    t = np.asarray(t, dtype=float)
    v = np.asarray(v, dtype=float)
    valid = np.ones(t.shape, bool) if valid is None else np.asarray(valid, bool)
    return Channel(name, LENGTH, t, v, valid, "TEST.Field")


class TestSampleChannel:
    def test_interpolates_between_samples(self):
        c = channel([0, 1, 2], [0, 10, 20])
        values, ok = sample_channel(c, [0.0, 0.5, 1.5, 2.0])
        assert ok.all()
        assert values == pytest.approx([0, 5, 15, 20])

    def test_outside_the_span_is_invalid(self):
        """Before the log starts and after it ends there is nothing to show."""
        c = channel([10, 11], [1, 2])
        _, ok = sample_channel(c, [9.99, 10.0, 11.0, 11.01])
        assert list(ok) == [False, True, True, False]

    def test_does_not_interpolate_across_a_long_gap(self):
        """A hole in the log must read as "no data", not as a straight line."""
        c = channel([0, 1, 8, 9], [0, 10, 80, 90])
        _, ok = sample_channel(c, [0.5, 4.0, 8.5], max_gap=1.0)
        assert list(ok) == [True, False, True]

    def test_invalid_samples_are_excluded_from_interpolation(self):
        # The 999 spike is flagged invalid by the autopilot and must not leak into
        # neighbouring frames.
        c = channel([0, 1, 2], [0, 999, 20], valid=[True, False, True])
        values, ok = sample_channel(c, [1.0], max_gap=5.0)
        assert ok[0]
        assert values[0] == pytest.approx(10.0)

    def test_all_invalid_channel(self):
        c = channel([0, 1], [5, 6], valid=[False, False])
        values, ok = sample_channel(c, [0.5])
        assert not ok.any()
        assert values[0] == 0.0

    def test_single_sample_channel(self):
        c = channel([5.0], [42.0])
        values, ok = sample_channel(c, [4.5, 5.0, 7.0], max_gap=1.0)
        assert list(ok) == [True, True, False]
        assert values[1] == 42.0


class TestDebounce:
    def test_short_flicker_is_suppressed(self):
        # 50 Hz rangefinder alternating in and out of range: the readout must not
        # strobe between a number and NO AGL.
        valid = np.array([True, False, True, False, True, True, True, True])
        out = debounce(valid, dt=0.02, hold=0.3)
        assert out.all()

    def test_sustained_change_is_followed_after_the_hold(self):
        valid = np.array([True] * 5 + [False] * 30)
        out = debounce(valid, dt=0.1, hold=0.3)
        assert out[:5].all()
        assert not out[-1]
        # The transition is delayed by the hold, which is the point of the filter.
        assert out[6] and not out[9]

    def test_zero_hold_is_a_passthrough(self):
        valid = np.array([True, False, True])
        assert list(debounce(valid, 0.1, 0.0)) == [True, False, True]


class TestSampleSteps:
    def test_index_of_active_step(self):
        changes = np.array([10.0, 20.0, 30.0])
        idx = sample_steps(changes, np.array([5.0, 10.0, 25.0, 99.0]))
        assert list(idx) == [-1, 0, 1, 2]

    def test_no_steps_at_all(self):
        idx = sample_steps(np.array([]), np.array([1.0, 2.0]))
        assert list(idx) == [-1, -1]


class TestMessageTrack:
    def test_instant_message_stays_the_minimum_time(self):
        track = MessageTrack.from_events(
            [MessageEvent(10.0, "one")],
            MessageTrackOptions(max_lines=1, min_duration=1.0, duration=1.0),
        )
        assert track.at(10.0) == ("one",)
        assert track.at(10.9) == ("one",)
        assert track.at(11.1) == ()

    def test_burst_is_queued_so_nothing_is_lost(self):
        """Four messages in the same millisecond, one line: each still gets its second.

        These timings come from a real log, where "Landing sequence start" is followed
        by three more messages within one frame.
        """
        events = [MessageEvent(553.59, f"m{i}") for i in range(4)]
        track = MessageTrack.from_events(
            events, MessageTrackOptions(max_lines=1, min_duration=1.0, duration=1.0)
        )
        assert track.at(553.6) == ("m0",)
        assert track.at(554.6) == ("m1",)
        assert track.at(555.6) == ("m2",)
        assert track.at(556.6) == ("m3",)

    def test_multiple_lines_show_together_newest_last(self):
        events = [MessageEvent(100.0, "a"), MessageEvent(100.5, "b")]
        track = MessageTrack.from_events(
            events, MessageTrackOptions(max_lines=3, min_duration=1.0, duration=4.0)
        )
        assert track.at(101.0) == ("a", "b")

    def test_a_full_display_delays_the_next_message(self):
        # With two lines, the third message may only appear once the first has had its
        # guaranteed second on screen.
        events = [MessageEvent(0.0, "a"), MessageEvent(0.0, "b"), MessageEvent(0.0, "c")]
        track = MessageTrack.from_events(
            events, MessageTrackOptions(max_lines=2, min_duration=1.0, duration=4.0)
        )
        assert track.at(0.5) == ("a", "b")
        assert track.at(1.5) == ("b", "c")

    def test_boot_messages_are_dropped(self):
        log = TelemetryLog(
            path="x",
            messages=[
                MessageEvent(1.0, "ChibiOS: nonsense"),
                MessageEvent(1.0, "GPS 1: probing for u-blox"),
                MessageEvent(20.0, "Reached waypoint #4"),
            ],
            arm_time=10.0,
        )
        track = MessageTrack.from_log(log)
        assert track.texts == ["Reached waypoint #4"]

    def test_exclude_patterns(self):
        log = TelemetryLog(
            path="x",
            messages=[MessageEvent(20.0, "Mission: 12 WP"), MessageEvent(21.0, "keep")],
            arm_time=10.0,
        )
        track = MessageTrack.from_log(
            log, MessageTrackOptions(exclude=("Mission:.*",))
        )
        assert track.texts == ["keep"]


class TestBuildTimeline:
    def _log(self) -> TelemetryLog:
        from telemetry_overlay.telemetry import fields

        return TelemetryLog(
            path="x",
            channels={
                fields.AGL: channel(
                    [0, 1, 2, 3], [1, 2, 3, 4], [True, True, False, False], fields.AGL
                )
            },
            arm_time=1.0,
            t_start=0.0,
            t_end=3.0,
            missing=[fields.IAS],
        )

    def test_missing_channels_are_never_valid(self):
        from telemetry_overlay.telemetry import fields

        times = np.arange(0, 3, 0.5)
        tl = build_timeline(self._log(), times, times, options=SamplingOptions())
        assert not tl.valid[fields.IAS].any()
        assert tl.state(0).value(fields.IAS) is None

    def test_flight_time_is_nan_before_arming(self):
        times = np.arange(0, 3, 0.5)
        tl = build_timeline(self._log(), times, times)
        assert tl.state(0).flight_time is None  # t=0.0, armed at 1.0
        assert tl.state(4).flight_time == pytest.approx(1.0)  # t=2.0

    def test_state_reports_messages_and_mode(self):
        from telemetry_overlay.telemetry.model import ModeChange

        log = self._log()
        log.modes = [ModeChange(0.5, 10, "AUTO")]
        times = np.arange(0, 3, 0.5)
        tl = build_timeline(log, times, times)
        assert tl.state(0).mode is None
        assert tl.state(2).mode == "AUTO"
