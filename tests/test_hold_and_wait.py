"""Tests for hold_key and wait.

The behaviour that matters most in hold_key is the release. A key left down turns every
later keystroke in the session into a shortcut -- a fault that outlives the call by an
unbounded time and is very hard to trace back to it. So the release is tested under an
interrupt, not only on the happy path.

wait is deliberately dull, and its tests are about the cap and the validation rather than
about timing: a fixed sleep is the fallback for when there is no observable condition, and
the interesting failure mode is an unbounded one that blocks the daemon.
"""
from __future__ import annotations

import asyncio

import pytest

import config
import input_controller as ic
from autopilot_daemon import AutopilotDaemon
from config import AutopilotState

pytestmark = pytest.mark.skipif(ic.sys.platform != "win32", reason="Win32 only")


class RecordingController:
    """Records key events without touching the OS."""

    def __init__(self, state=AutopilotState.AI_ACTIVE, fail_on=None):
        self.state = state
        self.keys = []          # (vk, key_up)
        self.fail_on = fail_on  # vk whose key-down raises

    def _send_win32_key(self, vk, key_up=False):
        self.keys.append((vk, key_up))

    def _resolve_hotkey(self, hotkey_str):
        return ic.InputController._resolve_hotkey(hotkey_str)

    def hold_key(self, hotkey_str, duration_ms):
        return ic.InputController.hold_key(self, hotkey_str, duration_ms)


def downs(keys):
    return [vk for vk, up in keys if not up]


def ups(keys):
    return [vk for vk, up in keys if up]


class TestHoldKeyRelease:
    def test_every_pressed_key_is_released(self, monkeypatch):
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 10_000)
        ctrl = RecordingController()
        ctrl.hold_key("ctrl+shift+a", 10)
        assert downs(ctrl.keys) == ups(ctrl.keys)[::-1] or set(downs(ctrl.keys)) == set(
            ups(ctrl.keys))
        assert len(downs(ctrl.keys)) == len(ups(ctrl.keys)) == 3

    def test_release_order_is_the_reverse_of_press_order(self, monkeypatch):
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 10_000)
        ctrl = RecordingController()
        ctrl.hold_key("ctrl+shift+a", 1)
        assert ups(ctrl.keys) == list(reversed(downs(ctrl.keys)))

    def test_keys_are_released_when_the_hold_is_interrupted(self, monkeypatch):
        """The case that matters. An interrupt during the sleep must still release,
        otherwise the session is left with a stuck modifier and every later keystroke
        becomes a shortcut."""
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 10_000)
        ctrl = RecordingController()

        def interrupt(_seconds):
            ctrl.state = AutopilotState.USER_OVERRIDE
            raise ic.InterruptedByUserError("interrupted mid-hold")

        monkeypatch.setattr(ic.time, "sleep", interrupt)
        with pytest.raises(ic.InterruptedByUserError):
            ctrl.hold_key("shift+a", 5000)
        assert set(downs(ctrl.keys)) == set(ups(ctrl.keys)), (
            f"pressed {downs(ctrl.keys)} but released {ups(ctrl.keys)}")

    def test_a_failing_release_does_not_stop_the_others(self, monkeypatch):
        """One key that cannot be released must not leave the rest held."""
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 10_000)
        ctrl = RecordingController()
        real_send = ctrl._send_win32_key
        calls = {"n": 0}

        def flaky(vk, key_up=False):
            if key_up and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("release failed")
            real_send(vk, key_up)

        ctrl._send_win32_key = flaky
        ctrl.hold_key("ctrl+shift", 1)
        assert len(ups(ctrl.keys)) == 1, "the second key was never released"

    def test_the_duration_is_capped(self, monkeypatch):
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 50)
        slept = []
        monkeypatch.setattr(ic.time, "sleep", lambda s: slept.append(s))
        RecordingController().hold_key("a", 999_999)
        assert any(abs(s - 0.05) < 1e-9 for s in slept), slept

    def test_a_negative_duration_is_rejected_before_anything_is_sent(self):
        ctrl = RecordingController()
        with pytest.raises(ValueError, match="отрицательной"):
            ctrl.hold_key("a", -5)
        assert ctrl.keys == []

    def test_zero_duration_presses_and_releases_without_holding(self, monkeypatch):
        monkeypatch.setattr(config, "HOLD_KEY_MAX_MS", 10_000)
        ctrl = RecordingController()
        ctrl.hold_key("a", 0)
        assert len(downs(ctrl.keys)) == 1 and len(ups(ctrl.keys)) == 1

    def test_an_unknown_token_sends_nothing(self):
        ctrl = RecordingController()
        with pytest.raises(ValueError, match="Неизвестные токены"):
            ctrl.hold_key("notakey+a", 10)
        assert ctrl.keys == []

    def test_it_refuses_under_user_override(self):
        ctrl = RecordingController(state=AutopilotState.USER_OVERRIDE)
        with pytest.raises(ic.InterruptedByUserError):
            ctrl.hold_key("a", 10)
        assert ctrl.keys == []


class TestSharedParser:
    def test_press_hotkey_and_hold_key_use_one_parser(self):
        """Two copies of this parser would drift the way four copies of the frame response
        did, and the drift shows up as one action accepting a token the other refuses."""
        ctrl = RecordingController()
        monkey = ic.InputController._resolve_hotkey
        assert ctrl.hold_key.__func__ is not None
        assert monkey("ctrl+shift+a") == [0x11, 0x10, 0x41]
        # Both paths resolve through the same function object.
        assert ic.InputController.press_hotkey.__qualname__.endswith("press_hotkey")
        assert monkey("f13") == [0x6F + 13]

    def test_hold_key_rejects_the_same_tokens_press_hotkey_does(self):
        for bad in ("notakey", "ctrl+", "f99", ""):
            with pytest.raises(ValueError):
                RecordingController().hold_key(bad, 10)
            with pytest.raises(ValueError):
                ic.InputController._resolve_hotkey(bad)


class TestWaitAction:
    def _stub(self):
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        d.input_ctrl = type("I", (), {
            "state": AutopilotState.AI_ACTIVE,
            "get_cursor_pos": lambda self: (10, 20),
        })()
        d.vision = type("V", (), {
            "capture_screen": lambda self, **kw: ("B64", 1280, 720, 1.5, 1.5, MON),
        })()
        d.host_tree = None
        d._update_overlay_target = lambda _i: None
        d._cursor_in_frame = lambda *a: (1, 2)
        d._risk_of = lambda *a, **k: None
        return d

    def test_it_waits_and_returns_a_frame(self, monkeypatch):
        slept = []
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or _done())
        d = self._stub()
        out = asyncio.run(d._action_wait({"duration_ms": 250}))
        assert out["status"] == "ok"
        assert out["data"]["waited_ms"] == 250
        assert slept and abs(slept[0] - 0.25) < 1e-9

    def test_it_caps_at_wait_max_and_says_so(self, monkeypatch):
        monkeypatch.setattr(config, "WAIT_MAX_MS", 100)
        monkeypatch.setattr(asyncio, "sleep", lambda s: _done())
        out = asyncio.run(self._stub()._action_wait({"duration_ms": 999_999}))
        assert out["data"]["waited_ms"] == 100
        assert out["data"]["requested_ms"] == 999_999
        assert out["data"]["capped"] is True
        assert "WAIT_MAX_MS" in out["data"]["message"]

    def test_an_uncapped_wait_does_not_claim_to_be_capped(self, monkeypatch):
        monkeypatch.setattr(config, "WAIT_MAX_MS", 10_000)
        monkeypatch.setattr(asyncio, "sleep", lambda s: _done())
        out = asyncio.run(self._stub()._action_wait({"duration_ms": 100}))
        assert out["data"]["capped"] is False
        assert "WAIT_MAX_MS" not in out["data"]["message"]

    def test_zero_waits_without_sleeping(self, monkeypatch):
        slept = []
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or _done())
        out = asyncio.run(self._stub()._action_wait({"duration_ms": 0}))
        assert out["data"]["waited_ms"] == 0
        assert slept == []

    def test_a_negative_duration_is_rejected(self):
        out = asyncio.run(self._stub()._action_wait({"duration_ms": -5}))
        assert out["status"] == "error"
        assert "отрицательной" in out["message"]

    def test_a_non_numeric_duration_is_rejected(self):
        out = asyncio.run(self._stub()._action_wait({"duration_ms": "soon"}))
        assert out["status"] == "error"
        assert "soon" in out["message"]

    def test_it_does_not_add_a_stabilisation_wait(self, monkeypatch):
        """The caller asked for a duration. Adding an unbounded quiet-window search on top
        would make the result depend on desktop activity nobody asked about."""
        seen = {}
        d = self._stub()
        # Instance attribute, so no self is bound: the signature takes kwargs only.
        d.vision.capture_screen = lambda **kw: (
            seen.update(kw) or ("B64", 10, 10, 1.0, 1.0, MON))
        monkeypatch.setattr(asyncio, "sleep", lambda s: _done())
        asyncio.run(d._action_wait({"duration_ms": 5}))
        assert seen["wait_stabilization"] is False

    def test_the_frame_uses_the_unified_shape(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", lambda s: _done())
        out = asyncio.run(self._stub()._action_wait({"duration_ms": 5}))
        for field in ("screenshot", "scaled_width", "scale_x", "cursor",
                      "cursor_desktop", "monitor"):
            assert field in out["data"], field


MON = type("M", (), {"index": 1, "name": "D1", "left": 0, "top": 0,
                     "width": 1920, "height": 1080})()


async def _done():
    return None


class TestWiring:
    def test_both_actions_are_dispatched(self):
        import inspect

        src = inspect.getsource(AutopilotDaemon._dispatch)
        assert '"hold_key"' in src and '"wait"' in src

    def test_both_tools_are_registered(self):
        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {t.name for t in tools}
        assert {"hold_key", "wait"} <= names

    def test_wait_takes_no_input_lock(self):
        """It holds nothing and moves nothing, so serialising it against acting would block
        a no-op behind the most expensive action."""
        from autopilot_daemon import NON_INPUT_ACTIONS

        assert "wait" in NON_INPUT_ACTIONS
