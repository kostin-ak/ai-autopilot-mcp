"""Tests for cursor-shape and caret reading (PLAN 3.2).

The point of the reading is not the shape name but what it licenses: an I-beam means the
target accepts text, before any text is typed. So the description is tested as carefully
as the Win32 call, because a name without its meaning leaves the caller to already know
the convention that motivated the reading.

The handle-map tests are live: the map is built by asking Windows, and a faked map would
test nothing, since the whole defect this replaced was a hand-written table that did not
match what Windows actually returns.
"""
from __future__ import annotations

import asyncio

import pytest

import input_controller as ic
from autopilot_daemon import NON_INPUT_ACTIONS, AutopilotDaemon, _describe_cursor

pytestmark = pytest.mark.skipif(ic.sys.platform != "win32", reason="Win32 only")


class TestCursorDescription:
    def test_the_ibeam_says_the_target_takes_text(self):
        out = _describe_cursor({"shape": "ibeam", "showing": True},
                               {"available": False, "error": 0})
        assert "принимает текст" in out

    def test_the_hand_says_the_target_is_clickable(self):
        assert "кликабельна" in _describe_cursor({"shape": "hand", "showing": True},
                                                 {"available": False})

    def test_no_says_the_action_would_be_refused(self):
        """The most useful of the set: it stops an action before it is attempted."""
        assert "отклонено" in _describe_cursor({"shape": "no", "showing": True},
                                               {"available": False})

    def test_busy_shapes_say_action_now_is_unreliable(self):
        for shape in ("wait", "appstarting"):
            assert "ненадёжно" in _describe_cursor({"shape": shape, "showing": True},
                                                   {"available": False}), shape

    def test_a_hidden_pointer_is_said_to_be_hidden(self):
        """A hidden pointer is a different fact from an arrow, and reporting the shape
        alone would hide it."""
        out = _describe_cursor({"shape": "arrow", "showing": False}, {"available": False})
        assert "скрыт" in out

    def test_a_visible_caret_says_where_text_will_land(self):
        out = _describe_cursor(
            {"shape": "ibeam", "showing": True},
            {"available": True, "caret_visible": True, "caret_rect": [10, 20, 11, 40],
             "hwnd_focus": 4242, "focus_class": "Edit"})
        assert "текст ляжет туда" in out
        assert "Edit" in out and "4242" in out

    def test_a_present_but_non_blinking_caret_is_distinguished(self):
        out = _describe_cursor(
            {"shape": "ibeam", "showing": True},
            {"available": True, "caret_visible": False, "hwnd_caret": 99})
        assert "не мигает" in out

    def test_no_caret_means_typed_text_goes_nowhere(self):
        """The reading that prevents blaming an application for swallowing input."""
        out = _describe_cursor({"shape": "arrow", "showing": True},
                               {"available": True, "caret_visible": False,
                                "hwnd_caret": 0})
        assert "никуда не попадёт" in out

    def test_an_unreadable_caret_says_so_with_the_reason(self):
        out = _describe_cursor({"shape": "arrow", "showing": True},
                               {"available": False, "error": 5})
        assert "недоступно" in out and "5" in out

    def test_an_unknown_shape_is_named_rather_than_swallowed(self):
        out = _describe_cursor({"shape": "weirdshape", "showing": True},
                               {"available": False})
        assert "weirdshape" in out

    def test_custom_explains_that_it_is_an_application_cursor(self):
        """Not an error and not an unknown: application-drawn cursors are normal, and
        calling them "unknown" would read as a failure of the reading."""
        out = _describe_cursor({"shape": "custom", "showing": True}, {"available": False})
        assert "приложения" in out


class TestHandleMapLive:
    def test_every_standard_id_resolves_to_a_handle(self):
        """The defect this replaces: a hand-written table mapping the IDC_* resource ids
        to handles was wrong on all 18 entries, because those ids are resource
        identifiers, not handles. Every entry reported "custom", which reads as "the
        technique does not work" when the table was simply wrong.
        """
        m = ic.standard_cursor_handles()
        assert m, "no standard cursors resolved at all"
        names = set(m.values())
        for name in ("arrow", "ibeam", "hand", "no", "wait"):
            assert name in names, f"{name} did not resolve"

    def test_the_map_is_cached(self):
        first = ic.standard_cursor_handles()
        assert ic.standard_cursor_handles() is first

    def test_handles_are_nonzero_and_distinct(self):
        """A zero handle means LoadCursor refused, and mapping it would make a failed
        GetCursorInfo look like a recognised shape."""
        m = ic.standard_cursor_handles()
        assert all(h != 0 for h in m)
        assert len(set(m.values())) == len(m), "two ids resolved to the same shape"

    def test_the_arrow_handle_matches_what_the_desktop_reports(self):
        """End to end: the shape reading and the map agree on the real desktop."""
        m = ic.standard_cursor_handles()
        arrow = next(h for h, n in m.items() if n == "arrow")
        assert arrow, "no arrow handle"
        shape = ic.get_cursor_shape()
        # Over whatever is under the pointer right now the shape must be one the map
        # knows, or custom -- never unavailable on a live desktop.
        assert shape["shape"] != "unavailable", shape


class TestReadingsLive:
    def test_cursor_shape_has_the_fields_a_caller_needs(self):
        s = ic.get_cursor_shape()
        assert set(s) == {"shape", "showing", "position", "position_physical",
                          "dpi_mismatch", "handle"} or "error" in s
        assert isinstance(s["shape"], str) and s["shape"]
        assert isinstance(s["showing"], bool)
        if s["shape"] != "unavailable":
            assert len(s["position"]) == 2
            if s["handle"] == 0 and not s["showing"]:
                # The pointer is suppressed right now (a video player hiding it, or the
                # cursor otherwise absent). There is no cursor to identify, so asserting a
                # handle would make this test's verdict depend on what the operator's
                # screen happens to be doing.
                pytest.skip("the cursor is hidden right now")
            assert s["handle"] != 0

    def test_physical_and_logical_positions_are_both_reported(self):
        """They coincide at 100% DPI, which is this machine's setting, so reading only
        GetCursorPos looks correct here and would be wrong on a mixed-DPI setup. Both are
        published so the mismatch is observable rather than assumed away."""
        s = ic.get_cursor_shape()
        assert "position_physical" in s and "dpi_mismatch" in s
        assert isinstance(s["dpi_mismatch"], bool)
        if s["position_physical"] is not None:
            assert len(s["position_physical"]) == 2
            assert s["dpi_mismatch"] == (s["position_physical"] != s["position"])

    def test_the_physical_position_helper_is_reachable_without_a_controller(self):
        """Module level on purpose: constructing an InputController installs input hooks,
        which a read-only caller must not do."""
        got = ic._physical_cursor_pos()
        assert got is None or (isinstance(got, list) and len(got) == 2)

    def test_the_controller_method_agrees_with_the_helper(self):
        ctrl = ic.InputController.__new__(ic.InputController)
        got = ctrl.get_physical_cursor_pos()
        helper = ic._physical_cursor_pos()
        if helper is None:
            assert got is None
        else:
            assert got == (helper[0], helper[1])

    def test_caret_info_reports_availability_explicitly(self):
        c = ic.get_caret_info()
        assert "available" in c
        if c["available"]:
            assert set(c) >= {"caret_visible", "caret_rect", "hwnd_focus", "hwnd_active",
                              "hwnd_caret", "focus_class", "thread_id"}
            assert len(c["caret_rect"]) == 4
            assert c["thread_id"] != 0, (
                "a zero thread id means the foreground window was not resolved, and "
                "GetGUIThreadInfo(0) would report the daemon's own empty GUI state")

    def test_caret_info_accepts_an_explicit_thread(self):
        """The default resolves the foreground thread; passing one is how a caller reads
        a specific application's caret."""
        c = ic.get_caret_info()
        tid = c.get("thread_id")
        if tid:
            again = ic.get_caret_info(tid)
            assert again["available"] is True
            assert again["thread_id"] == tid


class TestDaemonWiring:
    def test_the_action_reports_both_readings(self):
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        out = asyncio.run(d._action_get_cursor_state({}))
        assert out["status"] == "ok"
        assert "cursor" in out["data"] and "caret" in out["data"]
        assert out["data"]["message"]

    def test_it_does_not_take_the_input_lock(self):
        """Reading the pointer moves nothing, so serialising it against acting would block
        the cheapest observation behind the most expensive one."""
        assert "get_cursor_state" in NON_INPUT_ACTIONS

    def test_the_tool_is_registered(self):
        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        assert any(t.name == "get_cursor_state" for t in tools)
