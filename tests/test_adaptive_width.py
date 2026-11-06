"""Tests for the adaptive frame width (PLAN 3.4).

The invariant that matters: the bounds check that rejects out-of-frame coordinates runs
BEFORE the capture, so it has to predict the frame the capture is about to produce. If the
two computations disagreed by one pixel, a coordinate on the frame edge would be accepted
and then land outside, or rejected while being perfectly valid. Both go through
vision_engine.resolve_image_width, and these tests pin that agreement.
"""
from __future__ import annotations

import pytest

import config
from vision_engine import VisionEngine, resolve_image_width


class TestResolve:
    def test_none_is_the_default_not_zero(self):
        assert resolve_image_width(None) == config.TARGET_IMAGE_WIDTH

    def test_a_value_in_range_is_used_verbatim(self):
        assert resolve_image_width(config.TARGET_IMAGE_WIDTH_MIN) == \
            config.TARGET_IMAGE_WIDTH_MIN
        assert resolve_image_width(config.TARGET_IMAGE_WIDTH_MAX) == \
            config.TARGET_IMAGE_WIDTH_MAX
        mid = (config.TARGET_IMAGE_WIDTH_MIN + config.TARGET_IMAGE_WIDTH_MAX) // 2
        assert resolve_image_width(mid) == mid

    @pytest.mark.parametrize("bad", [0, 1, 640, config.TARGET_IMAGE_WIDTH_MIN - 1,
                                     config.TARGET_IMAGE_WIDTH_MAX + 1, 99999])
    def test_out_of_range_raises_rather_than_clamps(self, bad):
        """A silently clamped width would produce a frame the caller did not ask for and
        coordinates interpreted against the wrong space."""
        with pytest.raises(ValueError, match="вне допустимого диапазона"):
            resolve_image_width(bad)

    @pytest.mark.parametrize("bad", ["abc", "1280.5x", object()])
    def test_a_non_integer_raises_with_the_value_named(self, bad):
        with pytest.raises(ValueError):
            resolve_image_width(bad)

    def test_a_numeric_string_is_accepted(self):
        """Params arrive over JSON, where a caller may send "2000". Rejecting that would
        punish a transport detail rather than a wrong request."""
        assert resolve_image_width("2000") == 2000


class TestCaptureHonoursIt:
    def test_capture_computes_its_width_through_the_same_function(self):
        """Capture is the other half of the agreement. Asserted on the source because a
        live capture would need a display and a stabilisation wait; the wiring is the part
        that can drift, and this is the assertion that catches it."""
        import inspect

        src = inspect.getsource(VisionEngine.capture_screen)
        assert "resolve_image_width(image_width)" in src
        assert "scaled_w = config.TARGET_IMAGE_WIDTH" not in src


class TestBoundsCheckAgrees:
    def _daemon(self):
        from autopilot_daemon import AutopilotDaemon

        d = AutopilotDaemon.__new__(AutopilotDaemon)

        class V:
            @staticmethod
            def get_monitor_bounds(_idx):
                return {"left": 0, "top": 0, "width": 2560, "height": 1440}, None

        d.vision = V()
        return d

    def test_frame_size_uses_the_requested_width(self):
        d = self._daemon()
        w, h = d._frame_size(1, 2000)
        assert w == 2000
        assert h == int(round(1440 * (2000 / 2560)))

    def test_frame_size_default_matches_the_default_capture(self):
        d = self._daemon()
        w, _h = d._frame_size(1, None)
        assert w == config.TARGET_IMAGE_WIDTH

    def test_rejection_uses_the_requested_width(self):
        """The point of the whole feature: a coordinate that is valid for a 2000-wide frame
        but outside a 1280-wide one must be accepted when 2000 was requested, and rejected
        when it was not."""
        d = self._daemon()
        # 2000-wide frame is 1125 high; a point at (1900, 500) is inside it.
        assert d._reject_out_of_frame(1, [("P", 1900, 500)], 2000) is None
        rejected = d._reject_out_of_frame(1, [("P", 1900, 500)], None)
        assert rejected is not None
        assert "вне кадра" in rejected["message"]

    def test_rejection_still_rejects_garbage_at_any_width(self):
        d = self._daemon()
        for width in (None, 1280, 2560):
            r = d._reject_out_of_frame(1, [("P", -99999, -99999)], width)
            assert r is not None, width
