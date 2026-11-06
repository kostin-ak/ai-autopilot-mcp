"""Tests for virtual-desktop reporting.

Only the documented part of IVirtualDesktopManager is used: three methods, none of which
switches desktops. Switching lives in an undocumented interface that changes shape between
Windows builds, so it is deliberately absent and no test looks for it.

The readings are live where they can be -- the COM object exists on this machine -- and the
failure paths are exercised by replacing the manager.
"""
from __future__ import annotations

import ctypes
import sys

import pytest

import virtual_desktop as vd

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")


def foreground():
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return int(user32.GetForegroundWindow() or 0)


class TestHandleValidation:
    def test_handle_one_is_not_a_window(self):
        """Measured: the COM method answers TRUE for handle 1, which is not a window. It
        reports "not on another desktop" rather than "no such window", so without the
        guard a stale handle yields a confident True."""
        assert vd._is_window(1) is False

    def test_zero_is_not_a_window(self):
        assert vd._is_window(0) is False

    def test_the_foreground_window_is_a_window(self):
        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        assert vd._is_window(fg) is True

    @pytest.mark.parametrize("handle", [0, 1, 2, 12345])
    def test_an_invalid_handle_yields_none_not_a_guess(self, handle):
        assert vd.is_on_current_desktop(handle) is None
        assert vd.desktop_id(handle) is None


class TestLiveReadings:
    def test_a_real_window_reports_a_boolean(self):
        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        value = vd.is_on_current_desktop(fg)
        if value is None:
            pytest.skip("virtual desktop manager unavailable")
        assert value is True, "the foreground window is by definition on the current desktop"

    def test_a_real_window_has_a_desktop_id(self):
        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        wid = vd.desktop_id(fg)
        if wid is None:
            pytest.skip("virtual desktop manager unavailable")
        assert wid.startswith("{") and wid.endswith("}")
        assert len(wid) == 38, wid

    def test_reading_the_same_window_twice_gives_the_same_id(self):
        """Stability, which is what makes the identifier usable for comparison at all.

        Deliberately not comparing two different windows: that would pass or fail depending
        on whether the desktop happens to hold more than one of them, which is ambient
        state rather than a property of this module.
        """
        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        first = vd.desktop_id(fg)
        if first is None:
            pytest.skip("virtual desktop manager unavailable")
        assert vd.desktop_id(fg) == first


class TestUnavailableManager:
    def test_everything_returns_none_when_the_manager_is_missing(self, monkeypatch):
        """Not an error: a machine without the object is a machine where callers simply get
        None, and the field is omitted rather than set to a wrong boolean."""
        monkeypatch.setattr(vd, "manager", lambda: None)
        assert vd.is_on_current_desktop(foreground() or 1) is None
        assert vd.desktop_id(foreground() or 1) is None

    def test_annotate_leaves_records_untouched_when_unknown(self, monkeypatch):
        monkeypatch.setattr(vd, "manager", lambda: None)
        rows = [{"handle": foreground() or 1, "title": "x"}]
        vd.annotate(rows)
        assert "on_current_desktop" not in rows[0]
        assert "desktop_id" not in rows[0]


class TestAnnotate:
    def test_it_adds_the_field_for_a_real_window(self):
        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        rows = [{"handle": fg, "title": "x"}]
        vd.annotate(rows)
        if "on_current_desktop" not in rows[0]:
            pytest.skip("virtual desktop manager unavailable")
        assert rows[0]["on_current_desktop"] is True

    def test_it_skips_records_without_a_handle(self):
        rows = [{"title": "no handle"}, {"handle": 0, "title": "zero"}]
        out = vd.annotate(rows)
        assert out is rows, "annotate returns the same list it was given"
        for r in rows:
            assert "on_current_desktop" not in r

    def test_it_ignores_non_window_handles(self):
        rows = [{"handle": 1, "title": "stale"}]
        vd.annotate(rows)
        assert "on_current_desktop" not in rows[0]

    def test_an_absent_field_reads_as_unknown_not_false(self):
        """The distinction the whole module turns on: a window whose desktop could not be
        determined must not look like a window on another desktop."""
        rows = [{"handle": 1}]
        vd.annotate(rows)
        assert "on_current_desktop" not in rows[0]
        assert rows[0].get("on_current_desktop") is not False

    def test_it_works_on_objects_with_attributes(self):
        class W:
            def __init__(self, handle):
                self.handle = handle

        fg = foreground()
        if not fg:
            pytest.skip("no foreground window")
        w = W(fg)
        vd.annotate([w])
        # Either it was set, or the manager was unavailable; it must not raise.
        assert getattr(w, "on_current_desktop", None) in (True, False, None)


class TestSerializationCarriesTheField:
    """The defect this class exists to prevent.

    The daemon builds the windows payload from a fixed field list, so an attribute set on
    the WindowInfo object by annotate() does not reach the response on its own. Before the
    spread was added, the field was computed and discarded at serialization -- dead while
    every test that exercised annotate still passed. A test over annotate alone cannot see
    that; only the serialized dict can.
    """

    def _daemon_with_windows(self, windows):
        import asyncio

        from autopilot_daemon import AutopilotDaemon

        d = AutopilotDaemon.__new__(AutopilotDaemon)
        d.vision = type("V", (), {
            "list_monitors": lambda self: [],
            "list_windows": lambda self: windows,
        })()
        return asyncio.run(d._action_list_windows_and_monitors())

    def test_a_determined_value_reaches_the_payload(self):
        class W:
            handle = 1
            title = "t"
            rect = (0, 0, 1, 1)
            monitor_index = 1
            on_monitor = True
            class_name = "C"
            pid = 1
            is_active = False
            on_current_desktop = True

        data = self._daemon_with_windows([W()])["data"]
        assert data["windows"][0]["on_current_desktop"] is True

    def test_an_absent_value_stays_absent(self):
        """Unknown must not become false in the payload, the same rule as in the text."""
        class W:
            handle = 2
            title = "t"
            rect = (0, 0, 1, 1)
            monitor_index = 1
            on_monitor = True
            class_name = "C"
            pid = 1
            is_active = False

        data = self._daemon_with_windows([W()])["data"]
        assert "on_current_desktop" not in data["windows"][0]
        assert "desktop_id" not in data["windows"][0]

    def test_a_false_value_is_preserved_not_dropped(self):
        """The conditional spread must not mistake False for absent."""
        class W:
            handle = 3
            title = "t"
            rect = (0, 0, 1, 1)
            monitor_index = 1
            on_monitor = True
            class_name = "C"
            pid = 1
            is_active = False
            on_current_desktop = False
            desktop_id = "{11111111-2222-3333-4444-555555555555}"

        row = self._daemon_with_windows([W()])["data"]["windows"][0]
        assert row["on_current_desktop"] is False
        assert row["desktop_id"] == "{11111111-2222-3333-4444-555555555555}"


class TestTheFieldIsVisible:
    """A field that exists in the data but not in the formatted text is a field nobody
    reads. The model sees the text, so the mark is where the reading has to appear."""

    def test_a_window_on_another_desktop_is_marked_in_the_listing(self, monkeypatch):
        """Driven through the tool itself with the transport replaced, so the assertion is
        about the text the model actually receives rather than about a helper."""
        import asyncio

        import mcp_server

        payload = {
            "status": "ok",
            "data": {
                "monitors": [{"index": 1, "name": "D1", "bounds": [0, 0, 100, 100],
                              "is_primary": True}],
                "windows": [
                    {"handle": 1, "title": "here", "monitor_index": 1,
                     "bounds": [0, 0, 10, 10], "on_monitor": True,
                     "on_current_desktop": True},
                    {"handle": 2, "title": "elsewhere", "monitor_index": 1,
                     "bounds": [0, 0, 10, 10], "on_monitor": True,
                     "on_current_desktop": False},
                    {"handle": 3, "title": "unknown", "monitor_index": 1,
                     "bounds": [0, 0, 10, 10], "on_monitor": True},
                ],
            },
        }

        async def fake_request(_action, _params):
            return payload

        monkeypatch.setattr(mcp_server, "_send_daemon_request", fake_request)
        text = asyncio.run(mcp_server.list_windows_and_monitors())

        lines = [ln for ln in text.splitlines() if ln.startswith("- handle=")]
        assert len(lines) == 3, lines
        assert "[на другом виртуальном столе]" in lines[1]
        assert "[на другом виртуальном столе]" not in lines[0]
        # An absent field must not read as "on another desktop".
        assert "[на другом виртуальном столе]" not in lines[2]


class TestWindowInfoContract:
    """annotate sets an attribute on whatever list_windows returns.

    If that object ever becomes frozen or gains __slots__, the assignment is suppressed and
    the feature silently stops working while the window list keeps looking fine. This test
    fails loudly instead.
    """

    def test_window_info_accepts_the_attribute(self):
        from vision_engine import VisionEngine, WindowInfo

        sample = WindowInfo(handle=1, title="x", class_name="C", pid=1, rect=(0, 0, 1, 1),
                            monitor_index=1, on_monitor=True, is_active=False)
        sample.on_current_desktop = True
        assert sample.on_current_desktop is True
        assert VisionEngine().list_windows() is not None


class TestNoSwitching:
    def test_the_interface_exposes_only_the_documented_methods(self):
        """Switching desktops lives in an undocumented interface that changes between
        Windows builds. Offering it would be a promise the module cannot keep."""
        iface = vd._build_interface()
        # comtypes stores each method as (restype, name, argtypes...), so the name is the
        # second element. Indexing the first yields the HRESULT class.
        names = {m[1] for m in iface._methods_}
        assert names == {
            "IsWindowOnCurrentVirtualDesktop",
            "GetWindowDesktopId",
            "MoveWindowToDesktop",
        }
        assert not any("Switch" in n or n == "Current" for n in names)
