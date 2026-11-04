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
# Coordinate grid must not occlude the frame it annotates
# --------------------------------------------------------------------------
class TestGridOcclusion:
    """The grid used to paint two semi-opaque black bars: the full width of the
    top 14px and the full height of the left 28px.

    A live inspection of the active window returned its only four elements at
    y=6 -- "Система" at [6, 6] and the minimize/restore/close buttons at
    [1221, 6] / [1244, 6] / [1268, 6]. With the grid on, those were painted over,
    so the agent could not see the close button of the window it was inspecting.
    """

    WIDTH, HEIGHT = 1280, 720

    def _gridded(self, base):
        from PIL import Image

        from vision_engine import VisionEngine

        img = Image.new("RGB", (self.WIDTH, self.HEIGHT), base)
        return VisionEngine()._draw_coordinate_grid(img)

    def test_title_bar_band_is_not_painted_over(self):
        """(6, 6) is where the system menu button sits on a scaled frame."""
        out = self._gridded((255, 255, 255))
        r, g, b = out.getpixel((6, 6))
        # The old bar was fill=(0,0,0,150) over white, which lands near 105.
        assert min(r, g, b) > 200, f"title-bar pixel darkened to {(r, g, b)}"

    def test_window_button_positions_are_not_painted_over(self):
        out = self._gridded((240, 240, 240))
        for x in (1221, 1244, 1268):
            r, g, b = out.getpixel((x, 6))
            assert min(r, g, b) > 190, f"button column x={x} darkened to {(r, g, b)}"

    def test_left_ruler_band_is_not_painted_over(self):
        """x in [0, 28] used to be a continuous bar. Sampled at x=10, clear of
        the 4px ticks and of the labels drawn at x=2."""
        out = self._gridded((255, 255, 255))
        for y in (250, 350, 550):
            r, g, b = out.getpixel((10, y))
            assert min(r, g, b) > 200, f"left band darkened at y={y}: {(r, g, b)}"

    def test_grid_is_still_drawn(self):
        """Guard against the fix degenerating into a no-op.

        Uses a dark base: the grid lines are white at alpha 40, so over a white
        background they are invisible and the assertion would be meaningless.
        """
        out = self._gridded((60, 60, 90))
        on_line = out.getpixel((200, 300))
        off_line = out.getpixel((203, 303))
        assert on_line != off_line, "grid lines no longer visible"
        assert sum(on_line) > sum(off_line), "grid line is not lighter than its background"

    def test_labels_are_still_present(self):
        """Labels moved from a backing bar to a stroke; they must still render."""
        out = self._gridded((255, 255, 255))
        region = out.crop((100, 0, 140, 14))
        colors = region.getcolors(maxcolors=100000)
        assert colors is not None
        assert len(colors) > 1, "no label pixels rendered near x=100"
        cyan = any(
            b > 150 and r < 130 and g > 150 for _count, (r, g, b) in colors
        )
        assert cyan, "cyan label text not found on the top edge"


# --------------------------------------------------------------------------
# Monitor attribution when displays do not tile the virtual desktop
# --------------------------------------------------------------------------
class TestMonitorAttribution:
    def test_distance_is_zero_inside(self):
        from vision_engine import _distance_to_rect

        assert _distance_to_rect(100, 100, PRIMARY) == 0.0

    def test_distance_in_the_gap_below_secondary(self):
        """Secondary covers y in [351, 1431] at x < 0; y=100 there is a gap."""
        from vision_engine import _distance_to_rect

        d_secondary = _distance_to_rect(-1000, 100, SECONDARY)
        d_primary = _distance_to_rect(-1000, 100, PRIMARY)
        assert d_secondary > 0, "point is inside the gap, not on the secondary"
        assert d_secondary == pytest.approx(251.0)
        assert d_secondary < d_primary, "secondary is nearer than the primary here"

    def test_nearest_monitor_is_chosen_for_a_gap_point(self):
        from vision_engine import _distance_to_rect

        candidates = [PRIMARY, SECONDARY]
        nearest = min(candidates, key=lambda m: _distance_to_rect(-1000, 100, m))
        assert nearest is SECONDARY

    def test_window_info_can_express_approximate_attribution(self):
        from vision_engine import WindowInfo

        w = WindowInfo(
            handle=1, title="t", rect=(0, 0, 10, 10),
            monitor_index=2, is_active=False, on_monitor=False,
        )
        assert w.on_monitor is False
        assert w.monitor_index == 2

# --------------------------------------------------------------------------
# Off-monitor UI elements must not carry a bogus click_target
# --------------------------------------------------------------------------
class TestFrameBoundsRejection:
    """Out-of-frame coordinates used to be scaled, clamped, and clicked anyway.

    A live check with (-99999, -99999) reported "cursor now at (0, 0)": the corner of
    the monitor was clicked. Silent clamping is the worst available outcome, because
    the action succeeds at a predictable point unrelated to the request and nothing
    tells the caller its coordinates were wrong.
    """

    def _daemon_stub(self, width=1920, height=1080):
        class Vision:
            def get_monitor_bounds(self, idx=None):
                return ({"left": -1920, "top": 351,
                         "width": width, "height": height}, None)

        stub = daemon.AutopilotDaemon.__new__(daemon.AutopilotDaemon)
        stub.vision = Vision()
        return stub

    def test_frame_size_follows_the_monitor_aspect(self):
        assert self._daemon_stub()._frame_size(None) == (1280, 720)
        assert self._daemon_stub(2560, 1440)._frame_size(None) == (1280, 720)
        assert self._daemon_stub(4480, 1440)._frame_size(None) == (1280, 411)

    def test_coordinates_far_outside_are_rejected(self):
        err = self._daemon_stub()._reject_out_of_frame(
            None, [("Координаты", -99999, -99999)])
        assert err is not None and err["status"] == "error"
        assert "вне кадра" in err["message"]
        assert err["frame"] == [1280, 720]

    def test_one_pixel_beyond_is_rejected(self):
        d = self._daemon_stub()
        assert d._reject_out_of_frame(None, [("Координаты", 1281, 10)]) is not None
        assert d._reject_out_of_frame(None, [("Координаты", 10, 721)]) is not None
        assert d._reject_out_of_frame(None, [("Координаты", -1, 10)]) is not None

    def test_coordinates_inside_are_accepted(self):
        d = self._daemon_stub()
        assert d._reject_out_of_frame(None, [("Координаты", 0, 0)]) is None
        assert d._reject_out_of_frame(None, [("Координаты", 1280, 720)]) is None
        assert d._reject_out_of_frame(None, [("Координаты", 640, 360)]) is None

    def test_drag_endpoint_is_checked_too(self):
        err = self._daemon_stub()._reject_out_of_frame(
            None, [("Конечные координаты", 5000, 10)])
        assert err is not None and "Конечные" in err["message"]

    def test_missing_endpoint_is_skipped(self):
        """A drag with no explicit endpoint defaults to its start, which is checked."""
        d = self._daemon_stub()
        assert d._reject_out_of_frame(None, [("Конечные", None, None)]) is None

    def test_message_points_at_the_likely_cause(self):
        """A wrong monitor_index is the usual reason coordinates look impossible."""
        err = self._daemon_stub()._reject_out_of_frame(
            None, [("Координаты", 2000, 100)])
        assert "monitor_index" in err["message"]


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

    def _patch_desktop(self, monkeypatch, controls, windows=None):
        """Replaces pywinauto.Desktop with a double exposing the given controls."""

        class _Win:
            """Faithful double: inspect_ui_elements reads target metadata off it."""

            handle = 555

            def descendants(self_inner):
                return controls

            def window_text(self):
                return "double window"

            def class_name(self):
                return "DoubleClass"

            def process_id(self):
                return 4242

            def rectangle(self):
                from PyQt6.QtCore import QRect

                return QRect(0, 0, 800, 600)

        class _Wrapper:
            def __init__(self, handle, title, cls, rect):
                self.handle = handle
                self._title, self._cls, self._rect = title, cls, rect

            def window_text(self):
                return self._title

            def class_name(self):
                return self._cls

            def process_id(self):
                return 4242

            def rectangle(self):
                from PyQt6.QtCore import QRect

                x0, y0, x1, y1 = self._rect
                return QRect(x0, y0, x1 - x0, y1 - y0)

        class _Desktop:
            def __init__(self_inner, backend=None):
                pass

            def window(self_inner, **kw):
                return _Win()

            def windows(self_inner, **kw):
                return windows if windows is not None else [_Wrapper(1, "t", "c", (0, 0, 100, 100))]

        monkeypatch.setattr("pywinauto.Desktop", _Desktop)
        return _Wrapper

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
        self._patch_desktop(monkeypatch, [inside, outside])

        result = eng.inspect_ui_elements(None, 1)
        assert len(result.elements) == 1
        assert result.skipped_off_monitor == 1
        assert result.error is None
        assert result.elements[0].scaled_center_x >= 0
        assert result.elements[0].scaled_center_y >= 0

    def test_result_reports_which_window_was_inspected(self, monkeypatch):
        """An empty element list and a mis-resolved window look identical without this."""
        from vision_engine import VisionEngine

        eng = VisionEngine()
        monkeypatch.setattr(
            eng, "get_monitor_bounds", lambda idx=None: (
                {"left": 0, "top": 0, "width": 2560, "height": 1440}, PRIMARY
            )
        )
        self._patch_desktop(monkeypatch, [])

        result = eng.inspect_ui_elements(None, 1)
        assert result.elements == []
        assert result.target is not None
        assert result.target.handle == 555
        assert result.target.class_name == "DoubleClass"
        assert result.target.pid == 4242
        assert result.target.resolution == "foreground"

    def test_unresolvable_target_is_an_error_not_an_empty_list(self, monkeypatch):
        """The old code returned ([], 0) here, indistinguishable from a real
        window that simply exposes no controls."""
        from vision_engine import VisionEngine

        eng = VisionEngine()
        monkeypatch.setattr(
            eng, "get_monitor_bounds", lambda idx=None: (
                {"left": 0, "top": 0, "width": 2560, "height": 1440}, PRIMARY
            )
        )
        self._patch_desktop(monkeypatch, [], windows=[])

        result = eng.inspect_ui_elements("NoSuchWindowTitle", 1)
        assert result.target is None
        assert result.error is not None
        # Wording is Russian; assert on the stable parts that make the message
        # actionable rather than on the sentence itself.
        assert "list_windows_and_monitors" in result.error
        assert "handle" in result.error

    def test_invalid_handle_is_distinguished_from_no_match(self, monkeypatch):
        """Dead handle and nothing-matched need different follow-up actions."""
        import ctypes

        from vision_engine import VisionEngine

        eng = VisionEngine()
        monkeypatch.setattr(ctypes.windll.user32, "IsWindow", lambda h: 0)
        monkeypatch.setattr(
            eng, "get_monitor_bounds", lambda idx=None: (
                {"left": 0, "top": 0, "width": 2560, "height": 1440}, PRIMARY
            )
        )
        self._patch_desktop(monkeypatch, [], windows=[])

        result = eng.inspect_ui_elements(None, 1, hwnd=999999)
        assert result.target is None
        assert result.error is not None
        # Names the handle and the Win32 check that rejected it, which the
        # "nothing matched" message does not.
        assert "999999" in result.error
        assert "IsWindow" in result.error


# --------------------------------------------------------------------------
# Window target resolution: a title alone does not identify a window
# --------------------------------------------------------------------------
class TestWindowResolution:
    """AyuGram has four top-level windows titled "AyuGramDesktop": the real one
    with ~1400 descendants and three notification popups with 0-1. Taking the
    first title match lands on a popup and the agent concludes the application
    exposes nothing at all."""

    def _wrappers(self):
        from PyQt6.QtCore import QRect

        class _W:
            def __init__(self, handle, title, cls, w, h):
                self.handle = handle
                self._t, self._c = title, cls
                self._rect = QRect(0, 0, w, h)

            def window_text(self):
                return self._t

            def class_name(self):
                return self._c

            def process_id(self):
                return 7

            def rectangle(self):
                return self._rect

        return [
            _W(101, "AyuGramDesktop", "Window::Notification", 240, 90),
            _W(102, "AyuGramDesktop", "MainWindow", 1280, 800),
            _W(103, "AyuGramDesktop", "Window::Notification", 240, 90),
        ]

    def _resolve(self, monkeypatch, wrappers, **kw):
        from vision_engine import VisionEngine

        class _Desktop:
            def __init__(self, backend=None):
                pass

            def windows(self, **criteria):
                return wrappers

            def window(self, **criteria):
                return wrappers[0]

        monkeypatch.setattr("pywinauto.Desktop", _Desktop)
        return VisionEngine._resolve_window(_Desktop(), **kw)

    def test_ambiguous_title_picks_the_largest(self, monkeypatch):
        target, n, how = self._resolve(
            monkeypatch, self._wrappers(),
            hwnd=None, window_title="AyuGram", class_name=None,
        )
        assert n == 3
        assert how == "largest-of-3"
        assert target.handle == 102, "must pick the main window, not a popup"
        assert target.class_name == "MainWindow"

    def test_resolution_is_recorded_on_the_target(self, monkeypatch):
        target, n, how = self._resolve(
            monkeypatch, self._wrappers(),
            hwnd=None, window_title="AyuGram", class_name=None,
        )
        assert target.resolution == how == "largest-of-3"

    def test_unique_match_is_labelled_unique(self, monkeypatch):
        wrappers = self._wrappers()[:1]
        target, n, how = self._resolve(
            monkeypatch, wrappers,
            hwnd=None, window_title="AyuGram", class_name=None,
        )
        assert n == 1 and how == "unique"
        assert target.handle == 101

    def test_no_match_returns_none_with_a_reason(self, monkeypatch):
        target, n, how = self._resolve(
            monkeypatch, [],
            hwnd=None, window_title="NoSuchWindow", class_name=None,
        )
        assert target is None
        assert n == 0
        assert how == "none"

    def test_explicit_hwnd_bypasses_title_matching(self, monkeypatch):
        import ctypes

        monkeypatch.setattr(ctypes.windll.user32, "IsWindow", lambda h: 1)
        target, n, how = self._resolve(
            monkeypatch, self._wrappers(),
            hwnd=999, window_title=None, class_name=None,
        )
        assert how == "hwnd"
        assert n == 1

    def test_target_carries_pid_and_class_for_disambiguation(self, monkeypatch):
        target, _n, _how = self._resolve(
            monkeypatch, self._wrappers(),
            hwnd=None, window_title="AyuGram", class_name=None,
        )
        assert target.pid == 7
        assert target.class_name == "MainWindow"

    def test_falls_back_to_win32_title_when_the_uia_name_differs(self, monkeypatch):
        """The Win32 title and the UIA Name are different strings for one window.

        Qt derives the UIA Name from the application name while GetWindowText
        returns the document title, so AyuGram's main window is "AyuGramDesktop"
        to UIA and "Избранное @ Alex Kostin" to Win32. Since
        list_windows_and_monitors reports Win32 titles, matching only by UIA Name
        means a title copied out of that list finds nothing.
        """
        import vision_engine as ve

        wrappers = self._wrappers()

        class _Desktop:
            def __init__(self, backend=None):
                pass

            def windows(self, **criteria):
                return []  # UIA Name does not match the Win32 title

            def window(self, **criteria):
                return wrappers[1]

        seen = {}

        def fake_lookup(title, cls=None):
            seen["title"] = title
            return 132212

        monkeypatch.setattr(ve, "find_hwnd_by_win32_title", fake_lookup)
        target, n, how = ve.VisionEngine._resolve_window(
            _Desktop(), hwnd=None, window_title="Избранное", class_name=None
        )
        assert how == "win32-title"
        assert n == 1
        assert target.handle == 102
        assert seen["title"] == "Избранное"

    def test_win32_lookup_of_a_nonexistent_title_returns_none(self):
        """Real EnumWindows call; read-only."""
        from vision_engine import find_hwnd_by_win32_title

        assert find_hwnd_by_win32_title("zzz_definitely_no_such_window_zzz") is None

    def test_win32_lookup_rejects_empty_input(self):
        from vision_engine import find_hwnd_by_win32_title

        assert find_hwnd_by_win32_title("") is None
        assert find_hwnd_by_win32_title("   ") is None


# --------------------------------------------------------------------------
# Tool descriptions are English: they are model-facing instructions
# --------------------------------------------------------------------------
class TestToolDescriptions:
    """Descriptions are consumed by the model, not shown to the user, and
    instruction-following is more consistent in English. User-facing strings --
    HUD, tray, returned messages, documentation -- stay Russian."""

    TOOLS = {
        "take_control", "release_control", "get_screen_state", "mouse_action",
        "keyboard_action", "scroll_screen", "list_windows_and_monitors",
        "get_ui_elements", "reset_user_override", "get_hud_state",
        "probe_target", "get_snapshot", "resolve_clickable", "act_on_element",
        "window_action",
        "read_terminal", "write_terminal", "wait_terminal",
        "find_text_on_screen", "read_screen_text", "wait_for_text",
    }

    def test_element_verbs_match_the_daemon_dispatch(self):
        """The gateway's Literal and the daemon's table must name the same verbs.

        They live in different modules and there is no import linking them, so the
        only thing preventing drift is this test. A verb the gateway offers but the
        daemon cannot dispatch returns "Unknown action"; one the daemon accepts but
        the gateway does not offer is simply unreachable.
        """
        import asyncio

        import mcp_server
        from autopilot_daemon import ELEMENT_ACTIONS

        tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
        schema_enum = set(
            tools["act_on_element"].input_schema["properties"]["action"]["enum"]
        )
        assert schema_enum == set(ELEMENT_ACTIONS), (
            f"gateway offers {sorted(schema_enum - set(ELEMENT_ACTIONS))} that the "
            f"daemon cannot run; daemon accepts "
            f"{sorted(set(ELEMENT_ACTIONS) - schema_enum)} that is unreachable"
        )

    def test_window_verbs_match_the_daemon_dispatch(self):
        import asyncio

        import mcp_server
        from autopilot_daemon import WINDOW_ACTIONS

        tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
        schema_enum = set(
            tools["window_action"].input_schema["properties"]["action"]["enum"]
        )
        assert schema_enum == set(WINDOW_ACTIONS)

    def test_class_b_mutations_are_gated_during_a_pause(self):
        """Both mutating verb sets stay blocked while a human holds the machine."""
        import autopilot_daemon as daemon

        for action in ("element_action", "window_action", "write_terminal"):
            assert action not in daemon.NON_INPUT_ACTIONS, (
                f"{action} changes application state and must not run during "
                "USER_OVERRIDE"
            )

    def test_verbs_with_required_params_are_guarded_before_the_handler(self):
        """A required param must be checked by the daemon, not left to a KeyError.

        The handler receives the raw params dict, so a verb whose required list is
        empty while its body indexes a key would raise KeyError and surface as a
        generic error. Exercising the real dispatch with a stub client proves the
        guard fires first, for every verb that declares a requirement.
        """
        import asyncio

        from autopilot_daemon import ELEMENT_ACTIONS, WINDOW_ACTIONS, AutopilotDaemon

        class FakeUIA:
            def __getattr__(self, _name):
                def _fail(*_a, **_k):
                    raise AssertionError("handler ran despite a missing param")
                return _fail

        async def resolve_ok(_params):
            """Stands in for target resolution: the guard is what is under test."""
            return 1, "hwnd"

        stub = AutopilotDaemon.__new__(AutopilotDaemon)
        stub.uia = FakeUIA()
        stub._resolve_target = resolve_ok

        for table, dispatch in ((ELEMENT_ACTIONS, "_action_element_action"),
                                (WINDOW_ACTIONS, "_action_window_action")):
            for name, (_handler, required) in table.items():
                if not required:
                    continue
                params = {"action": name, "path": "1.0", "hwnd": 1}
                resp = asyncio.run(getattr(stub, dispatch)(params))
                assert resp["status"] == "error", f"{name} ran without {required}"
                for key in required:
                    assert key in resp["message"], f"{name} must name {key!r}"


    def test_every_tool_has_a_non_empty_english_description(self):
        import asyncio

        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        assert {t.name for t in tools} == self.TOOLS
        for t in tools:
            desc = (t.description or "").strip()
            assert desc, f"{t.name} has no description"
            non_ascii = [c for c in desc if ord(c) > 127]
            assert not non_ascii, (
                f"{t.name} description is not English: {non_ascii[:5]}"
            )

    def test_descriptions_name_the_class_and_its_side_effects(self):
        """The description is the only place the model learns whether an action
        consumes the single cursor and keyboard."""
        import asyncio

        import mcp_server

        tools = {t.name: (t.description or "") for t in asyncio.run(mcp_server.mcp.list_tools())}
        for name in ("mouse_action", "keyboard_action", "scroll_screen"):
            assert "Class A" in tools[name], f"{name} must state its action class"
        assert "USER_OVERRIDE" in tools["mouse_action"]
        assert "USER_OVERRIDE" in tools["keyboard_action"]

    def test_keyboard_description_matches_the_real_input_path(self):
        """It no longer types through the clipboard; a stale description here
        would make the model expect clipboard side effects that do not happen."""
        import asyncio

        import mcp_server

        tools = {t.name: (t.description or "") for t in asyncio.run(mcp_server.mcp.list_tools())}
        desc = tools["keyboard_action"]
        assert "KEYEVENTF_UNICODE" in desc
        assert "clipboard" in desc.lower()

    def test_get_ui_elements_documents_why_a_handle_is_needed(self):
        import asyncio

        import mcp_server

        tools = {t.name: (t.description or "") for t in asyncio.run(mcp_server.mcp.list_tools())}
        desc = tools["get_ui_elements"]
        assert "hwnd" in desc
        assert "largest-of-N" in desc
        assert "win32-title" in desc

    def _desc(self, name):
        import asyncio

        import mcp_server

        return {
            t.name: (t.description or "")
            for t in asyncio.run(mcp_server.mcp.list_tools())
        }[name]

    def test_probe_target_names_every_classification(self):
        """The classification decides which control path gets used, so all five
        have to be discoverable from the description alone."""
        desc = self._desc("probe_target")
        for cls in ("terminal", "rich", "partial", "opaque", "expensive"):
            assert cls in desc, f"{cls} classification undocumented"
        # The trap that made ADR-0009 necessary.
        assert "IsReadOnly" in desc

    def test_get_snapshot_documents_the_continuation_contract(self):
        desc = self._desc("get_snapshot")
        assert "token" in desc
        assert "frame space" in desc, "coordinates must be declared frame-relative"
        # Depth truncation is terminal; the description has to say so or the agent
        # will page with the same max_depth and stall.
        assert "max_depth" in desc
        assert "time_budget_ms" in desc

    def test_resolve_clickable_explains_why_the_snapshot_has_no_exact_point(self):
        """Otherwise the two tools look redundant and one gets skipped."""
        desc = self._desc("resolve_clickable")
        assert "centre" in desc
        assert "stale" in desc

    def test_act_on_element_documents_verification_and_its_one_exception(self):
        """Read-back is what separates a real action from a silent no-op, and the
        one action that cannot be read back has to say so."""
        desc = self._desc("act_on_element")
        for act in ("invoke", "set_value", "toggle", "select", "expand", "collapse"):
            assert f"'{act}'" in desc, f"{act} undocumented"
        assert "IsReadOnly" in desc, "the terminal trap must be in the description"
        assert "Not verifiable" in desc or "not verifiable" in desc.lower()

    def test_act_on_element_says_it_does_not_use_the_cursor(self):
        """That is the whole reason to prefer it over mouse_action."""
        desc = self._desc("act_on_element")
        assert "cursor" in desc.lower()
        assert "Class B" in desc

    def test_read_terminal_documents_the_ring_buffer_limit(self):
        """Past ~9000 rows the oldest output is gone for good. An agent that does
        not know this reads once at the end and silently loses the beginning."""
        desc = self._desc("read_terminal")
        assert "9000" in desc
        assert "ring" in desc.lower()
        assert "viewport" in desc.lower(), "must say it is not just the viewport"

    def test_write_terminal_documents_why_readback_is_mandatory(self):
        """ValuePattern on a terminal accepts SetValue and changes nothing."""
        desc = self._desc("write_terminal")
        assert "IsReadOnly" in desc
        assert "read back" in desc.lower() or "re-reading" in desc.lower()
        assert "input site" in desc.lower(), "the WT child window is the point"

    def test_wait_terminal_documents_the_echo_trap(self):
        """A shell echoes the command before running it, so waiting on a string that
        appears in the command proves nothing. Measured at 0.00 s versus 6.16 s."""
        desc = self._desc("wait_terminal")
        assert "after_text" in desc
        assert "echo" in desc.lower()
        assert "0.00" in desc and "6.16" in desc

    def test_find_text_on_screen_says_a_miss_is_not_proof_of_absence(self):
        """OCR substitutes individual letters, so an empty result must not be read
        as 'the control is not there'."""
        desc = self._desc("find_text_on_screen")
        assert "not proof of absence" in desc
        assert "homoglyph" in desc.lower()
        assert "read_screen_text" in desc, "must point at the way to diagnose a miss"

    def test_find_text_on_screen_says_it_is_the_fallback(self):
        """Otherwise it competes with get_snapshot and the weaker path gets chosen."""
        desc = self._desc("find_text_on_screen")
        assert "fallback" in desc.lower()
        assert "get_snapshot" in desc

    def test_wait_for_text_carries_the_same_caveat(self):
        desc = self._desc("wait_for_text")
        assert "substitutes individual letters" in desc
        assert "short" in desc.lower(), "must advise a short stable fragment"

    def test_read_screen_text_says_what_it_is_for(self):
        desc = self._desc("read_screen_text")
        assert "no accessibility tree" in desc
        assert "diagnose" in desc.lower()

    def test_mouse_action_says_out_of_frame_is_rejected(self):
        """It used to clamp and click, reporting success at the wrong point."""
        desc = self._desc("mouse_action")
        assert "rejected" in desc.lower()
        assert "clamped" in desc.lower()

    def test_keyboard_action_says_an_unknown_token_aborts(self):
        """Skipping unknown tokens once pressed a real key from malformed input."""
        desc = self._desc("keyboard_action")
        assert "aborts" in desc.lower()
        assert "sends nothing" in desc.lower()
# --------------------------------------------------------------------------
# Session lock state must reach the agent
# --------------------------------------------------------------------------
class TestSessionState:
    """With the workstation locked, capture still succeeds and returns the lock
    screen. Measured live: get_screen_state came back looking perfectly normal.
    Without an explicit signal the agent cannot tell a locked machine from a
    closed application or a broken capture."""

    def test_returns_documented_keys_and_never_raises(self):
        import session_state as ss

        s = ss.session_state()
        assert set(s) == {"locked", "state", "session_id", "reason"}
        assert s["locked"] in (True, False, None)

    def test_structure_alignment_places_data_at_offset_8(self):
        """The union's 64-bit level-2 members force Data to offset 8.

        With Data at 4 every field reads four bytes early, SessionFlags picks up
        a padding zero, and "locked" comes out True for the wrong reason. That
        is exactly what happened before the hexdump was checked.
        """
        import ctypes

        import session_state as ss

        assert ss._WTSINFOEXW.Data.offset == 8
        assert ctypes.sizeof(ss._WTSINFOEXW) >= (
            8 + ctypes.sizeof(ss._WTSINFOEX_LEVEL1_W)
        )

    def test_describe_state(self, monkeypatch):
        import session_state as ss

        cases = [
            (True, "active", "ЗАБЛОКИРОВАНА"),
            (False, "disconnected", "ОТКЛЮЧЕНА"),
            (False, "active", ""),
            (None, "unknown", ""),
        ]
        for locked, state, expected in cases:
            monkeypatch.setattr(
                ss, "session_state",
                lambda lock=locked, st=state: {
                    "locked": lock, "state": st, "session_id": 1, "reason": "",
                },
            )
            text = ss.describe_state()
            if expected:
                assert expected in text, f"{locked}/{state} -> {text!r}"
            else:
                assert text == "", f"{locked}/{state} -> {text!r}"

    def test_annotation_reaches_ok_responses(self, monkeypatch):
        import session_state as ss
        from autopilot_daemon import AutopilotDaemon

        monkeypatch.setattr(
            ss, "session_state",
            lambda: {"locked": True, "state": "active", "session_id": 1, "reason": ""},
        )
        resp = AutopilotDaemon._annotate_session({"status": "ok", "data": {"x": 1}})
        assert resp["data"]["session"] == {"locked": True, "state": "active"}
        assert "session_warning" in resp["data"]
        assert resp["data"]["x"] == 1, "annotation must not disturb existing data"

    def test_annotation_creates_data_for_flat_responses(self, monkeypatch):
        import session_state as ss
        from autopilot_daemon import AutopilotDaemon

        monkeypatch.setattr(
            ss, "session_state",
            lambda: {"locked": False, "state": "active", "session_id": 1, "reason": ""},
        )
        resp = AutopilotDaemon._annotate_session({"status": "ok", "state": "IDLE"})
        assert resp["data"]["session"]["locked"] is False
        assert "session_warning" not in resp["data"]

    def test_annotation_leaves_errors_alone(self, monkeypatch):
        import session_state as ss
        from autopilot_daemon import AutopilotDaemon

        monkeypatch.setattr(
            ss, "session_state",
            lambda: {"locked": True, "state": "active", "session_id": 1, "reason": ""},
        )
        resp = AutopilotDaemon._annotate_session({"status": "error", "message": "boom"})
        assert "data" not in resp
        assert resp["message"] == "boom"
