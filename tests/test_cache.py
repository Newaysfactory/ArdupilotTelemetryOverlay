"""Cache layout, and the partial-coverage logic behind the optical-flow reuse."""

from __future__ import annotations

import json
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from telemetry_overlay import cache
from telemetry_overlay.flow_cache import FlowParams, RollRateCache
from telemetry_overlay.sync import SyncModel
from telemetry_overlay.video.probe import VideoInfo


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Never touch the repo's real cache/ directory from a test."""
    monkeypatch.setenv("TELEMETRY_OVERLAY_CACHE", str(tmp_path / "cache"))


def fake_info(frame_count: int, fps: int = 30) -> VideoInfo:
    return VideoInfo(
        path=Path("fake.mp4"),
        width=1920,
        height=1080,
        frame_rate=Fraction(fps, 1),
        frame_count=frame_count,
        duration=frame_count / fps,
        time_base=Fraction(1, 60000),
        codec="h264",
        pix_fmt="yuv420p",
        color_range=0,
        colorspace=0,
        bit_rate=0,
        rotation=0,
        has_audio=False,
        audio_codec=None,
        audio_rate=None,
        creation_time=None,
    )


def make_video(tmp_path: Path, name: str = "clip.mp4") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_bytes(b"not really a video, but it has a size and an mtime")
    return path


class TestCacheDirectories:
    def test_same_name_in_different_folders_does_not_collide(self, tmp_path):
        a = make_video(tmp_path / "flight-a", "clip.mp4")
        b = make_video(tmp_path / "flight-b", "clip.mp4")
        assert cache.cache_dir_for(a) != cache.cache_dir_for(b)

    def test_directory_name_stays_readable(self, tmp_path):
        video = make_video(tmp_path, "montagna.MP4")
        assert cache.cache_dir_for(video).name.startswith("montagna-MP4-")

    def test_is_stable_across_calls(self, tmp_path):
        video = make_video(tmp_path)
        assert cache.cache_dir_for(video) == cache.cache_dir_for(video)


class TestClearCache:
    def test_removes_derived_files_but_keeps_the_sync(self, tmp_path):
        video = make_video(tmp_path)
        directory = cache.ensure_cache_dir_for(video)
        (directory / "roll-rate.npz").write_bytes(b"derived")
        (directory / "telemetry.npz").write_bytes(b"derived")
        (directory / "plots").mkdir()
        (directory / "plots" / "autosync_fit.png").write_bytes(b"derived")
        (directory / "sync.json").write_text('{"log_delay": 1.0}')

        removed = cache.clear_cache_for(video)

        assert {p.name for p in removed} == {"roll-rate.npz", "telemetry.npz", "plots"}
        assert not (directory / "roll-rate.npz").exists()
        assert not (directory / "plots").exists()
        # The alignment the user made by hand must survive a cache wipe.
        assert (directory / "sync.json").exists()

    def test_removes_the_directory_when_nothing_is_preserved(self, tmp_path):
        video = make_video(tmp_path)
        directory = cache.ensure_cache_dir_for(video)
        (directory / "roll-rate.npz").write_bytes(b"derived")

        cache.clear_cache_for(video)

        assert not directory.exists()

    def test_is_a_no_op_when_nothing_was_cached(self, tmp_path):
        assert cache.clear_cache_for(make_video(tmp_path)) == []


class TestSyncStorage:
    def test_round_trips_through_the_cache_directory(self, tmp_path):
        video = make_video(tmp_path)
        SyncModel(log_delay=63.089, scale=1.0015).save(SyncModel.path_for(video))

        loaded = SyncModel.load_for(video)

        assert loaded is not None
        assert loaded.log_delay == pytest.approx(63.089)
        assert loaded.scale == pytest.approx(1.0015)

    def test_records_a_creation_timestamp(self, tmp_path):
        video = make_video(tmp_path)
        path = SyncModel(log_delay=1.0).save(SyncModel.path_for(video))

        created = json.loads(path.read_text())["created"]

        # ISO 8601 with an offset, e.g. 2026-08-27T14:32:11+02:00.
        assert created
        assert datetime.fromisoformat(created).tzinfo is not None

    def test_creation_timestamp_survives_a_re_save(self, tmp_path):
        video = make_video(tmp_path)
        path = SyncModel.path_for(video)
        SyncModel(log_delay=1.0).save(path)
        first = json.loads(path.read_text())["created"]

        # A fresh model, as the GUI builds on every drag, must not reset the date.
        SyncModel(log_delay=2.0).save(path)

        assert json.loads(path.read_text())["created"] == first

    def test_updated_timestamp_is_rewritten_on_every_save(self, tmp_path):
        video = make_video(tmp_path)
        path = SyncModel.path_for(video)
        SyncModel(log_delay=1.0).save(path)
        first = json.loads(path.read_text())

        # Timestamps are second-resolution, so pin the clock rather than sleeping.
        later = "2030-01-01T00:00:00+00:00"

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(later)

        with mock.patch("telemetry_overlay.sync.datetime", _FixedDatetime):
            SyncModel(log_delay=2.0).save(path)

        second = json.loads(path.read_text())
        assert second["created"] == first["created"]
        # Compare instants, not strings: the stamp is written in local time, so the
        # same moment renders differently depending on the machine's zone.
        assert datetime.fromisoformat(second["updated"]) == datetime.fromisoformat(later)
        assert second["updated"] != first["updated"]

    def test_both_timestamps_round_trip(self, tmp_path):
        video = make_video(tmp_path)
        SyncModel(log_delay=1.0).save(SyncModel.path_for(video))

        loaded = SyncModel.load_for(video)

        assert loaded.created
        assert loaded.updated
        assert datetime.fromisoformat(loaded.updated).tzinfo is not None

    def test_falls_back_to_the_pre_cache_location(self, tmp_path):
        video = make_video(tmp_path)
        legacy = SyncModel.legacy_path_for(video)
        legacy.write_text('{"log_delay": 414.13, "scale": 1.0}')

        loaded = SyncModel.load_for(video)

        assert loaded is not None
        assert loaded.log_delay == pytest.approx(414.13)


class TestMissingRanges:
    """The logic that makes a partially-cached range cost only its holes."""

    def empty(self, n_pairs: int = 100) -> RollRateCache:
        return RollRateCache(Path("unused.npz"), n_pairs, key={})

    def test_everything_is_missing_when_nothing_is_cached(self):
        assert self.empty().missing_ranges(10, 20) == [(10, 20)]

    def test_nothing_is_missing_once_covered(self):
        store = self.empty()
        store.store(0, np.zeros(100))
        assert store.missing_ranges(10, 20) == []

    def test_only_the_uncovered_ends_are_returned(self):
        # The manualsync-then-autosync case: a slice in the middle is already done,
        # so a wider request must only compute what falls outside it.
        store = self.empty()
        store.store(40, np.zeros(20))  # pairs 40..59 known
        assert store.missing_ranges(0, 99) == [(0, 39), (60, 99)]

    def test_a_subrange_of_cached_data_is_free(self):
        store = self.empty()
        store.store(0, np.zeros(100))
        assert store.missing_ranges(30, 45) == []

    def test_several_holes_are_reported_separately(self):
        store = self.empty()
        store.store(20, np.zeros(10))
        store.store(60, np.zeros(10))
        assert store.missing_ranges(0, 99) == [(0, 19), (30, 59), (70, 99)]

    def test_short_holes_are_merged_to_avoid_an_extra_seek(self):
        # Two missing stretches separated by only 5 cached pairs: seeking costs more
        # than recomputing those 5, so they come back as one range.
        store = self.empty()
        store.store(20, np.zeros(5))  # pairs 20..24 known
        assert store.missing_ranges(0, 99, merge_gap=10) == [(0, 99)]

    def test_long_holes_are_not_merged(self):
        store = self.empty()
        store.store(20, np.zeros(50))  # pairs 20..69 known
        assert store.missing_ranges(0, 99, merge_gap=10) == [(0, 19), (70, 99)]

    def test_the_request_is_clamped_to_the_video(self):
        store = self.empty(n_pairs=50)
        assert store.missing_ranges(-10, 999) == [(0, 49)]

    def test_an_empty_video_has_nothing_to_compute(self):
        assert self.empty(n_pairs=0).missing_ranges(0, 10) == []


class TestRollRateCachePersistence:
    def test_survives_a_round_trip(self, tmp_path):
        video = make_video(tmp_path)
        info = fake_info(frame_count=101)
        params = FlowParams(analysis_width=480, max_features=250)

        store = RollRateCache.load(video, info, params)
        store.store(10, np.arange(5, dtype=np.float64))
        store.save()

        reloaded = RollRateCache.load(video, info, params)
        assert reloaded.missing_ranges(10, 14) == []
        assert reloaded.slice(10, 14) == pytest.approx(np.arange(5))

    def test_changed_analysis_parameters_invalidate_it(self, tmp_path):
        video = make_video(tmp_path)
        info = fake_info(frame_count=101)

        store = RollRateCache.load(video, info, FlowParams(480, 250))
        store.store(0, np.zeros(100))
        store.save()

        # A different analysis width produces different numbers: the stored ones
        # must not be reused as if they matched.
        other = RollRateCache.load(video, info, FlowParams(960, 250))
        assert other.missing_ranges(0, 99) == [(0, 99)]

    def test_a_modified_video_invalidates_it(self, tmp_path):
        video = make_video(tmp_path)
        info = fake_info(frame_count=101)
        params = FlowParams(480, 250)

        store = RollRateCache.load(video, info, params)
        store.store(0, np.zeros(100))
        store.save()

        video.write_bytes(b"a different clip entirely, re-encoded in place")

        assert RollRateCache.load(video, info, params).missing_ranges(0, 99) == [(0, 99)]

    def test_a_corrupt_cache_is_ignored_not_fatal(self, tmp_path):
        video = make_video(tmp_path)
        info = fake_info(frame_count=101)
        params = FlowParams(480, 250)
        (cache.ensure_cache_dir_for(video) / "roll-rate.npz").write_bytes(b"garbage")

        store = RollRateCache.load(video, info, params)

        assert store.missing_ranges(0, 99) == [(0, 99)]

    def test_saving_without_changes_writes_nothing(self, tmp_path):
        video = make_video(tmp_path)
        info = fake_info(frame_count=101)
        store = RollRateCache.load(video, info, FlowParams(480, 250))
        assert store.save() is None
