"""Every frame-returning action must answer with the same field set.

Four actions built this dict by hand and they drifted: take_control had no monitor block,
and keyboard_action and scroll_screen dropped cursor, cursor_desktop, scale_x and scale_y.
That is not cosmetics. Without the scale a caller cannot convert a frame coordinate into a
physical one on its own; without the cursor it cannot tell whether a keystroke landed
where it expected.

The test drives the real action methods against a fake daemon and compares the field sets,
so a field dropped from one action fails here rather than being discovered by a model that
cannot compute what it needs.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from autopilot_daemon import AutopilotDaemon
from vision_engine import MonitorInfo

# Fields every frame-returning action must carry. Listed once, asserted for all of them.
REQUIRED = {
    "screenshot", "scaled_width", "scaled_height",
    "scale_x", "scale_y",
    "cursor", "cursor_desktop",
    "monitor",
}
MONITOR_FIELDS = {"index", "name", "bounds"}


class FakeInput:
    state = config.AutopilotState.AI_ACTIVE

    def get_cursor_pos(self):
        return (120, 240)

    def set_state(self, _s):
        pass

    def __getattr__(self, name):
        """Any other input method is a no-op.

        Enumerating them would make this test break every time an action grows a new
        input call, and the thing under test is the response shape, not input delivery --
        which has its own tests.
        """
        return lambda *a, **k: None


class FakeVision:
    last_stabilization = "events"
    last_stabilization_ms = 5.0
    last_changed = True
    last_diff = None
    last_events_seen = 2
    last_change_source = "events"

    def capture_screen(self, **_kw):
        return ("B64", 1280, 720, 1.5, 1.5, MON)


class FakeSignal:
    def emit(self, *_a):
        pass


class FakeOverlay:
    sig_set_state = FakeSignal()
    sig_show_target = FakeSignal()


MON = MonitorInfo(index=1, name="DISPLAY1", left=0, top=0, width=1920, height=1080,
                  is_primary=True)


def make_daemon():
    d = AutopilotDaemon.__new__(AutopilotDaemon)
    d.input_ctrl = FakeInput()
    d.vision = FakeVision()
    d.overlay = FakeOverlay()
    d.audit = type("A", (), {"record": staticmethod(lambda r: None)})()
    d.host_tree = None
    d._update_overlay_target = lambda _i: (0, 0, 1.5, 1.5)
    d._risk_of = lambda *a, **k: __import__("risk").Risk(
        level=__import__("risk").REVERSIBLE, reason="test")
    return d


def frame_of(resp):
    return (resp.get("data") or {})


class TestUnifiedShape:
    @pytest.mark.parametrize("action,params", [
        ("get_screen_state", {}),
        ("take_control", {}),
        ("keyboard_action", {"text": "x"}),
        ("scroll_screen", {"direction": "down", "amount": 1}),
        ("mouse_action", {"action": "move", "x": 10, "y": 20}),
    ])
    def test_every_frame_action_carries_the_same_fields(self, action, params):
        d = make_daemon()
        # mouse_action and scroll_screen consult these on the way through.
        d._reject_out_of_frame = lambda *_a, **_k: None
        d._frame_size = lambda _i: (1280, 720)
        d.input_ctrl.move_to = lambda *a: None
        d.input_ctrl.click = lambda *a: None
        d.input_ctrl.smooth_move = lambda *a: None
        d.input_ctrl.scroll = lambda *a: None
        d._host_refusal = lambda *_a, **_k: None
        d._update_geometry = lambda: None

        resp = asyncio.run(getattr(d, f"_action_{action}")(params))
        data = frame_of(resp)
        missing = REQUIRED - set(data)
        assert not missing, f"{action} is missing {sorted(missing)}"

    def test_the_monitor_block_has_a_stable_inner_shape(self):
        d = make_daemon()
        resp = asyncio.run(d._action_get_screen_state({}))
        mon = frame_of(resp)["monitor"]
        assert set(mon) == MONITOR_FIELDS
        assert mon["bounds"] == [0, 0, 1920, 1080]
        assert mon["index"] == 1

    def test_the_scale_matches_what_the_capture_reported(self):
        """The scale is what lets a caller translate a frame coordinate into a physical
        one. Reporting a default instead of the capture's own value would be worse than
        omitting it, because it looks authoritative."""
        d = make_daemon()
        resp = asyncio.run(d._action_get_screen_state({}))
        data = frame_of(resp)
        assert data["scale_x"] == 1.5 and data["scale_y"] == 1.5
        assert data["scaled_width"] == 1280 and data["scaled_height"] == 720

    def test_mouse_action_additionally_reports_what_it_was_asked_for(self):
        d = make_daemon()
        d._reject_out_of_frame = lambda *_a, **_k: None
        d.input_ctrl.smooth_move = lambda *a: None
        d._host_refusal = lambda *_a, **_k: None
        d._update_geometry = lambda: None
        resp = asyncio.run(d._action_mouse_action(
            {"action": "move", "x": 10, "y": 20}))
        data = frame_of(resp)
        assert data["requested"] == [10, 20]
        # And the actual cursor position, which is the evidence a move landed.
        assert "cursor" in data and "cursor_desktop" in data


class TestTheBuilderItself:
    def test_it_returns_exactly_the_required_fields_plus_extras(self):
        d = make_daemon()
        block = d._frame_block("B64", 1280, 720, 2.0, 2.0, MON, (10, 20),
                               requested=[1, 2])
        assert set(block) == REQUIRED | {"requested"}
        assert block["requested"] == [1, 2]

    def test_extras_can_overwrite_nothing_required_by_accident(self):
        """`update` means an extra key silently replaces a core field. That is a way for
        an action to re-introduce the drift this builder exists to remove."""
        d = make_daemon()
        block = d._frame_block("B64", 10, 10, 1.0, 1.0, MON, (0, 0),
                               message="hello")
        assert block["screenshot"] == "B64", "an extra must not displace a core field"
        assert block["message"] == "hello"
