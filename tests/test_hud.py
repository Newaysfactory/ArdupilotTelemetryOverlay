"""Units, preset persistence and overlay geometry."""

from __future__ import annotations

import json

import pytest

from telemetry_overlay import units
from telemetry_overlay.hud.preset import ElementConfig, Preset
from telemetry_overlay.hud.theme import Theme


class TestUnits:
    @pytest.mark.parametrize(
        ("key", "si", "expected"),
        [
            ("km/h", 10.0, 36.0),
            ("kt", 10.0, 19.438444924406),
            ("mph", 10.0, 22.369362920544),
            ("m/s", 10.0, 10.0),
            ("ft", 100.0, 328.08398950131),
            ("ft/min", 5.0, 984.2519685039),
        ],
    )
    def test_conversions(self, key, si, expected):
        assert units.get_unit(key).convert(si) == pytest.approx(expected)

    def test_format_uses_the_unit_default_decimals(self):
        assert units.get_unit("V").format(11.94) == "11.9"
        # Python rounds ties to even, so pick a value away from the tie: a HUD
        # readout crossing exactly .5 for one frame is not worth special-casing.
        assert units.get_unit("m").format(376.6) == "377"

    def test_resolve_rejects_a_mismatched_quantity(self):
        with pytest.raises(ValueError, match="measures"):
            units.resolve_unit("km/h", units.LENGTH)

    def test_resolve_falls_back_to_the_kind_default(self):
        assert units.resolve_unit(None, units.SPEED).key == "km/h"

    def test_unknown_unit_is_reported_clearly(self):
        with pytest.raises(ValueError, match="unknown unit"):
            units.get_unit("furlongs")

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(0, "00:00"), (61, "01:01"), (3661, "1:01:01"), (-1, "--:--")],
    )
    def test_duration_format(self, seconds, text):
        assert units.format_duration(seconds) == text

    @pytest.mark.parametrize(
        ("degrees", "text"),
        [(0, "360"), (1, "001"), (359.6, "360"), (-10, "350"), (400, "040")],
    )
    def test_heading_format(self, degrees, text):
        """Compass convention: north reads 360, never 000."""
        assert units.format_heading(degrees) == text


class TestPreset:
    def test_round_trip(self, tmp_path):
        preset = Preset(
            name="mine",
            theme=Theme(value_color="#123456FF", value_size=0.05),
            elements=[
                ElementConfig(
                    type="readout",
                    id="ias",
                    x=0.1,
                    y=0.2,
                    anchor="center-left",
                    unit="kt",
                    decimals=1,
                    options={"channel": "ias"},
                )
            ],
        )
        path = preset.save(tmp_path / "p.json")
        loaded = Preset.load(path)
        assert loaded.name == "mine"
        assert loaded.theme.value_color == "#123456FF"
        assert loaded.theme.value_size == 0.05
        element = loaded.element("ias")
        assert element is not None
        assert (element.unit, element.decimals, element.anchor) == (
            "kt",
            1,
            "center-left",
        )

    def test_shipped_default_preset_loads(self):
        from telemetry_overlay.cli import DEFAULT_PRESET

        preset = Preset.load(DEFAULT_PRESET)
        ids = {e.id for e in preset.elements}
        # Every readout the project set out to display.
        assert {"ias", "gs", "alt", "agl", "vs", "voltage", "messages", "horizon"} <= ids

    def test_unknown_theme_keys_are_ignored(self):
        """A preset written by a newer build must still open."""
        theme = Theme.from_dict({"value_color": "#FFFFFFFF", "future_option": 3})
        assert theme.value_color == "#FFFFFFFF"

    def test_future_version_is_refused(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"version": 99, "elements": []}))
        with pytest.raises(ValueError, match="newer than this build"):
            Preset.load(path)

    def test_bad_anchor_is_reported(self):
        with pytest.raises(ValueError, match="unknown anchor"):
            ElementConfig(type="readout", anchor="middle-ish")


class TestGeometry:
    def test_anchor_placement(self):
        from PySide6.QtCore import QSizeF

        from telemetry_overlay.hud.elements.base import anchor_rect

        size = QSizeF(100, 40)
        assert anchor_rect(500, 300, size, "top-left").topLeft().toTuple() == (500, 300)
        assert anchor_rect(500, 300, size, "top-right").left() == 400
        assert anchor_rect(500, 300, size, "bottom-right").top() == 260
        centred = anchor_rect(500, 300, size, "center")
        assert (centred.left(), centred.top()) == (450, 280)

    def test_bands_cover_every_element_and_stay_inside_the_frame(self, qt_app):
        from telemetry_overlay.cli import DEFAULT_PRESET
        from telemetry_overlay.hud.renderer import OverlayRenderer

        renderer = OverlayRenderer(Preset.load(DEFAULT_PRESET), 1920, 1080)
        assert renderer.bands, "a preset with elements must produce bands"
        assigned = {e.config.id for b in renderer.bands for e in b.elements}
        assert assigned == {e.config.id for e in renderer.elements}
        for band in renderer.bands:
            assert band.rect.left() >= 0 and band.rect.top() >= 0
            assert band.rect.right() < 1920 and band.rect.bottom() < 1080
            # Even offsets and sizes keep chroma-subsampled compositing aligned.
            assert band.rect.left() % 2 == 0 and band.rect.top() % 2 == 0
            for element in band.elements:
                assert band.rect.contains(element.bounds().toAlignedRect())

    def test_bands_are_capped(self, qt_app):
        from telemetry_overlay.cli import DEFAULT_PRESET
        from telemetry_overlay.hud.renderer import OverlayRenderer

        renderer = OverlayRenderer(
            Preset.load(DEFAULT_PRESET), 1920, 1080, max_bands=2
        )
        assert len(renderer.bands) <= 2

    def test_layout_is_resolution_independent(self, qt_app):
        """The same preset must place elements identically at 1080p and 4K."""
        from telemetry_overlay.cli import DEFAULT_PRESET
        from telemetry_overlay.hud.renderer import OverlayRenderer

        preset = Preset.load(DEFAULT_PRESET)
        small = OverlayRenderer(preset, 1920, 1080)
        large = OverlayRenderer(preset, 3840, 2160)
        for a, b in zip(small.elements, large.elements, strict=True):
            ra, rb = a.rect(), b.rect()
            # Font pixel sizes are integers, so metrics do not scale perfectly
            # linearly; a fraction of a percent is the expected residual.
            assert rb.left() == pytest.approx(ra.left() * 2, rel=0.002)
            assert rb.width() == pytest.approx(ra.width() * 2, rel=0.02)


class TestReadout:
    def _state(self, values, valid, **kwargs):
        from telemetry_overlay.telemetry.timeline import FrameState

        defaults = {
            "frame": 0,
            "video_time": 0.0,
            "log_time": 0.0,
            "mode": None,
            "flight_time": None,
            "messages": (),
        }
        defaults.update(kwargs)
        return FrameState(values=values, valid=valid, **defaults)

    def _readout(self, qt_app, **options):
        from telemetry_overlay.hud.elements.base import RenderContext
        from telemetry_overlay.hud.elements.readout import ChannelReadout

        ctx = RenderContext(1920, 1080, Theme())
        return ChannelReadout(
            ElementConfig(type="readout", id="t", options=options), ctx
        )

    def test_no_data_placeholder(self, qt_app):
        element = self._readout(
            qt_app, channel="agl", no_data_text="NO AGL", no_data_hides_label=True
        )
        content = element.content(self._state({"agl": 5.0}, {"agl": False}))
        assert content.value == "NO AGL"
        assert content.no_data and content.hide_label

    def test_reserved_field_fits_the_no_data_text(self, qt_app):
        """The box must be wide enough for "NO AGL" or the placeholder would clip."""
        element = self._readout(qt_app, channel="agl", no_data_text="NO AGL")
        assert element.reserved_text() == "NO AGL"

    def test_warning_threshold(self, qt_app):
        element = self._readout(qt_app, channel="voltage", warn_below=10.6)
        assert element.content(self._state({"voltage": 10.2}, {"voltage": True})).warn
        assert not element.content(
            self._state({"voltage": 11.8}, {"voltage": True})
        ).warn

    def test_signed_vertical_speed(self, qt_app):
        element = self._readout(qt_app, channel="vs", signed=True)
        assert element.content(self._state({"vs": 2.4}, {"vs": True})).value == "+2.4"
        assert element.content(self._state({"vs": -2.4}, {"vs": True})).value == "-2.4"

    def test_cache_key_changes_only_with_the_drawn_text(self, qt_app):
        element = self._readout(qt_app, channel="alt")
        a = element.cache_key(self._state({"alt": 100.2}, {"alt": True}))
        b = element.cache_key(self._state({"alt": 100.4}, {"alt": True}))
        c = element.cache_key(self._state({"alt": 101.9}, {"alt": True}))
        assert a == b, "sub-display changes must not force a repaint"
        assert a != c

    def test_unknown_channel_is_reported(self, qt_app):
        with pytest.raises(ValueError, match="unknown channel"):
            self._readout(qt_app, channel="nonsense")
