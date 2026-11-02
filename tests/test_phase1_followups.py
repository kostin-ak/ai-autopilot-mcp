"""Regression tests for the coordinate-space and escape-hatch fixes.

Found by running the daemon live on a two-monitor setup where the secondary
sits to the left of the primary, so the virtual desktop origin is (-1920, 0).
"""

import pytest

import autopilot_daemon as daemon
import config
from vision_engine import MonitorInfo

PRIMARY = MonitorInfo(1, "Display 1", 0, 0, 2560, 1440, True)
SECONDARY = MonitorInfo(2, "Display 2", -1920, 351, 1920, 1080, False)
VIRTUAL = MonitorInfo(0, "All Displays", -1920, 0, 4480, 1440, False)


# --------------------------------------------------------------------------
# Overlay: desktop coordinates must be converted into widget-local space
# --------------------------------------------------------------------------
class TestOverlayCoordinates:
    """The HUD window lives at virtualGeometry(), whose origin is not (0, 0).

    Painting a desktop-coordinate rect directly offsets it by that origin. With
    the secondary monitor to the left at x = -1920, the frame for the primary
    landed at global x in [-1920, 640], so only a 640px strip of it was visible
    on the primary -- which reads as a frame of the wrong size.
    """

    class _OriginStub:
        def __init__(self, x, y):
            self._x, self._y = x, y

        def geometry(self):
            from PyQt6.QtCore import QRect

            return QRect(self._x, self._y, 4480, 1440)

    def test_primary_frame(self):
        from PyQt6.QtCore import QRect

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(-1920, 0)
        local = HUDOverlayWindow._global_to_local_rect(stub, QRect(0, 0, 2560, 1440))
        assert (local.x(), local.y(), local.width(), local.height()) == (1920, 0, 2560, 1440)

    def test_secondary_frame_maps_to_origin(self):
        from PyQt6.QtCore import QRect

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(-1920, 0)
        local = HUDOverlayWindow._global_to_local_rect(stub, QRect(-1920, 351, 1920, 1080))
        assert (local.x(), local.y()) == (0, 351)

    def test_virtual_desktop_frame_maps_to_origin(self):
        from PyQt6.QtCore import QRect

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(-1920, 0)
        local = HUDOverlayWindow._global_to_local_rect(stub, QRect(-1920, 0, 4480, 1440))
        assert (local.x(), local.y(), local.width(), local.height()) == (0, 0, 4480, 1440)

    def test_ripple_point(self):
        from PyQt6.QtCore import QPoint

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(-1920, 0)
        p = HUDOverlayWindow._global_to_local_point(stub, QPoint(1280, 720))
        assert (p.x(), p.y()) == (3200, 720)

    def test_zero_origin_is_identity(self):
        """On a single-monitor setup the conversion must be a no-op."""
        from PyQt6.QtCore import QRect

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(0, 0)
        local = HUDOverlayWindow._global_to_local_rect(stub, QRect(0, 0, 2560, 1440))
        assert (local.x(), local.y()) == (0, 0)

    def test_round_trip_is_consistent(self):
        """local + origin must reproduce the original global rect for every monitor."""
        from PyQt6.QtCore import QRect

        from overlay_ui import HUDOverlayWindow

        stub = self._OriginStub(-1920, 0)
        for mon in (PRIMARY, SECONDARY, VIRTUAL):
            global_rect = QRect(mon.left, mon.top, mon.width, mon.height)
            local = HUDOverlayWindow._global_to_local_rect(stub, global_rect)
            assert (local.x() + (-1920), local.y() + 0) == (global_rect.x(), global_rect.y())
            assert (local.width(), local.height()) == (global_rect.width(), global_rect.height())


# --------------------------------------------------------------------------
# Cursor must never be reported as a coordinate outside the frame
# --------------------------------------------------------------------------
class TestCursorInFrame:
    def test_on_monitor_maps_correctly(self):
        assert daemon.AutopilotDaemon._cursor_in_frame((1280, 720), PRIMARY, 2.0, 2.0) == [640, 360]

    def test_off_monitor_to_the_left_returns_none(self):
        """Was reported as (-536, 470): a valid-looking but nonexistent coordinate."""
        assert daemon.AutopilotDaemon._cursor_in_frame((-1072, 940), PRIMARY, 2.0, 2.0) is None

    def test_off_monitor_below_returns_none(self):
        assert daemon.AutopilotDaemon._cursor_in_frame((100, 1600), PRIMARY, 2.0, 2.0) is None

    def test_top_left_corner_is_inclusive(self):
        assert daemon.AutopilotDaemon._cursor_in_frame((0, 0), PRIMARY, 2.0, 2.0) == [0, 0]

    def test_bottom_right_edge_is_exclusive(self):
        # 2560 is one past the last column of a 2560-wide monitor.
        assert daemon.AutopilotDaemon._cursor_in_frame((2560, 720), PRIMARY, 2.0, 2.0) is None

    def test_negative_origin_monitor(self):
        mon = SECONDARY
        sx = mon.width / config.TARGET_IMAGE_WIDTH
        scaled_h = round(mon.height * (config.TARGET_IMAGE_WIDTH / mon.width))
        sy = mon.height / scaled_h
        assert daemon.AutopilotDaemon._cursor_in_frame((-1920, 351), mon, sx, sy) == [0, 0]

    def test_frame_max_is_within_scaled_width(self):
        mon = SECONDARY
        sx = mon.width / config.TARGET_IMAGE_WIDTH
        scaled_h = round(mon.height * (config.TARGET_IMAGE_WIDTH / mon.width))
        sy = mon.height / scaled_h
        x, y = daemon.AutopilotDaemon._cursor_in_frame(
            (mon.left + mon.width - 1, mon.top + mon.height - 1), mon, sx, sy
        )
        assert x < config.TARGET_IMAGE_WIDTH
        assert y < scaled_h


# --------------------------------------------------------------------------
# release_control must always be reachable
# --------------------------------------------------------------------------
class TestEscapeHatch:
    """Blocking release_control under USER_OVERRIDE left the HUD frame stuck on.

    The override gate stopped the only action that clears the frame, so a pause
    had no exit short of reset_override or killing the process.
    """

    def test_release_control_is_reachable_during_override(self):
        assert "release_control" in daemon.NON_INPUT_ACTIONS

    def test_reset_override_is_reachable_during_override(self):
        assert "reset_override" in daemon.NON_INPUT_ACTIONS

    def test_take_control_is_still_gated(self):
        assert "take_control" not in daemon.NON_INPUT_ACTIONS

    @pytest.mark.parametrize("action", ["mouse_action", "keyboard_action", "scroll_screen"])
    def test_input_actions_are_gated(self, action):
        assert action not in daemon.NON_INPUT_ACTIONS

    @pytest.mark.parametrize(
        "action",
        ["get_screen_state", "list_windows_and_monitors", "get_ui_elements"],
    )
    def test_observation_stays_available(self, action):
        assert action in daemon.NON_INPUT_ACTIONS


# --------------------------------------------------------------------------
# Off-monitor UI elements must not carry a bogus click_target
# --------------------------------------------------------------------------
class TestUiElementFiltering:
    def _fake_ctrl(self, left, top, right, bottom, cls="Button", text="OK"):
        class _Rect:
            def __init__(self):
                self.left, self.top, self.right, self.bottom = left, top, right, bottom

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        class _Ctrl:
            def rectangle(self_inner):
                return _Rect()

            def friendly_class_name(self_inner):
                return cls

            def window_text(self_inner):
                return text

            def automation_id(self_inner):
                return "auto"

        return _Ctrl()

    def test_element_outside_monitor_is_skipped(self, monkeypatch):
        from vision_engine import VisionEngine

        eng = VisionEngine()
        monkeypatch.setattr(
            eng, "get_monitor_bounds", lambda idx=None: (
                {"left": 0, "top": 0, "width": 2560, "height": 1440}, PRIMARY
            )
        )
        inside = self._fake_ctrl(100, 100, 200, 140)
        outside = self._fake_ctrl(-900, 300, -800, 340)

        class _Win:
            def descendants(self_inner):
                return [inside, outside]

        monkeypatch.setattr(
            "pywinauto.Desktop", lambda backend=None: type(
                "D", (), {"window": lambda s, **kw: _Win()}
            )()
        )

        elements, skipped = eng.inspect_ui_elements(None, 1)
        assert len(elements) == 1
        assert skipped == 1
        assert elements[0].scaled_center_x >= 0
        assert elements[0].scaled_center_y >= 0
