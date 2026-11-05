"""Tests for how event-based waiting is wired into the daemon and the capture path.

event_waiter itself is covered by tests/test_event_waiter.py against a real hook. What
is checked here is the integration: that the daemon anchors waits correctly, that a
timeout is reported as an outcome rather than as a fault, and above all that every way
the event path can be unavailable degrades to frame differencing instead of returning
a frame captured mid-animation.
"""
from __future__ import annotations

import asyncio

import config
import event_waiter as ew
from autopilot_daemon import NON_INPUT_ACTIONS, AutopilotDaemon
from vision_engine import VisionEngine


# ------------------------------------------------------------------ fixtures
class FakeWaitResult:
    def __init__(self, matched=True, error="", elapsed_ms=12.0, quiescent_ms=130.0,
                 waited_events=3, discarded_stale=1, event=None):
        self.matched = matched
        self.error = error
        self.elapsed_ms = elapsed_ms
        self.quiescent_ms = quiescent_ms
        self.waited_events = waited_events
        self.discarded_stale = discarded_stale
        self.event = event


class FakeStats:
    delivered = 41
    buffer_len = 7
    hook = 12345


class FakeWatcher:
    def __init__(self, result=None, raise_on_wait=False, running=True, start_err=""):
        self.result = result if result is not None else FakeWaitResult()
        self.raise_on_wait = raise_on_wait
        self._running = running
        self._start_err = start_err
        self.calls = []

    @property
    def running(self):
        return self._running

    def start(self):
        return self._start_err

    def snapshot_stats(self):
        return FakeStats()

    def wait_for_quiescence(self, since_tick, hwnd=0, quiet_ms=0.0, timeout_s=0.0,
                            events=None):
        self.calls.append(("quiescence", since_tick, hwnd, quiet_ms, timeout_s, events))
        if self.raise_on_wait:
            raise RuntimeError("pump died")
        return self.result

    def wait_for_event(self, since_tick, hwnd=0, events=None, timeout_s=0.0):
        self.calls.append(("event", since_tick, hwnd, events, timeout_s))
        if self.raise_on_wait:
            raise RuntimeError("pump died")
        return self.result


def daemon_with(fake_watcher, monkeypatch):
    d = AutopilotDaemon.__new__(AutopilotDaemon)
    d.audit = type("A", (), {"record": staticmethod(lambda r: None)})()
    d.host_tree = None
    monkeypatch.setattr(ew, "watcher", lambda: fake_watcher)
    return d


def call_wait(d, params):
    return asyncio.run(d._action_wait_for_event(params))


# --------------------------------------------------------- daemon wait action
class TestWaitForEventAction:
    def test_unknown_condition_is_rejected_with_the_valid_set(self, monkeypatch):
        d = daemon_with(FakeWatcher(), monkeypatch)
        out = call_wait(d, {"condition": "settled"})
        assert out["status"] == "error"
        assert "opened" in out["message"] and "value_changed" in out["message"]

    def test_disabled_watcher_says_so_and_names_alternatives(self, monkeypatch):
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", False)
        d = daemon_with(FakeWatcher(), monkeypatch)
        out = call_wait(d, {"condition": "opened"})
        assert out["status"] == "error"
        assert "EVENT_WATCHER_ENABLED" in out["message"]
        assert "wait_for_text" in out["message"]

    def test_a_watcher_that_will_not_start_is_an_error_not_a_hang(self, monkeypatch):
        d = daemon_with(FakeWatcher(running=False, start_err="hook null"), monkeypatch)
        out = call_wait(d, {"condition": "opened"})
        assert out["status"] == "error"
        assert "hook null" in out["message"]

    def test_quiescence_mode_is_chosen_when_quiet_ms_is_given(self, monkeypatch):
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        out = call_wait(d, {"quiet_ms": 150, "timeout_s": 2.0})
        assert out["data"]["mode"] == "quiescence"
        assert out["data"]["matched"] is True
        assert w.calls[0][0] == "quiescence"
        assert w.calls[0][3] == 150.0

    def test_event_mode_uses_the_named_condition(self, monkeypatch):
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        call_wait(d, {"condition": "value_changed"})
        assert w.calls[0][0] == "event"
        assert w.calls[0][3] == ew.CONDITIONS["value_changed"]

    def test_raw_event_codes_pass_through(self, monkeypatch):
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        call_wait(d, {"events": [ew.EVENT_OBJECT_SHOW, ew.EVENT_OBJECT_CREATE]})
        assert w.calls[0][3] == frozenset({ew.EVENT_OBJECT_SHOW,
                                           ew.EVENT_OBJECT_CREATE})

    def test_condition_wins_over_raw_codes(self, monkeypatch):
        """Two selectors at once is a caller mistake; resolving it silently would hide
        the mistake, so the named condition is the one that is used and the other is
        ignored rather than unioned."""
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        call_wait(d, {"condition": "opened", "events": [ew.EVENT_OBJECT_HIDE]})
        assert w.calls[0][3] == ew.CONDITIONS["opened"]

    def test_timeout_is_an_outcome_not_an_error(self, monkeypatch):
        """Nothing happening is the normal result of waiting. Reporting it as a tool
        fault would push the caller to treat an ordinary idle UI as a broken daemon."""
        w = FakeWatcher(FakeWaitResult(matched=False, error="не наступило"))
        d = daemon_with(w, monkeypatch)
        out = call_wait(d, {"condition": "opened", "timeout_s": 0.5})
        assert out["status"] == "ok"
        assert out["data"]["matched"] is False
        assert "не наступило" in out["data"]["message"]

    def test_an_explicit_tick_is_honoured(self, monkeypatch):
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        call_wait(d, {"condition": "opened", "since_tick": 424242})
        assert w.calls[0][1] == 424242

    def test_a_missing_tick_anchors_at_the_call(self, monkeypatch):
        """Defaulting to the buffer's whole contents would match stale events, which is
        the failure the watcher exists to prevent."""
        w = FakeWatcher()
        d = daemon_with(w, monkeypatch)
        before = ew.tick_now()
        out = call_wait(d, {"condition": "opened"})
        assert out["data"]["since_tick"] >= before - 1000
        assert w.calls[0][1] == out["data"]["since_tick"]

    def test_a_raising_waiter_is_reported_not_propagated(self, monkeypatch):
        """A broken pump is a different report from "the condition was not met", and
        neither may take the action down."""
        w = FakeWatcher(raise_on_wait=True)
        d = daemon_with(w, monkeypatch)
        out = call_wait(d, {"condition": "opened"})
        assert out["status"] == "error"
        assert "pump died" in out["message"]

    def test_stats_travel_with_the_result(self, monkeypatch):
        d = daemon_with(FakeWatcher(), monkeypatch)
        out = call_wait(d, {"condition": "opened"})
        assert out["data"]["watcher"] == {"delivered": 41, "buffer": 7, "hook": 12345}

    def test_waiting_does_not_take_the_input_lock(self):
        assert "wait_for_event" in NON_INPUT_ACTIONS


# ------------------------------------------------- capture-path stabilisation
class FakeRaw:
    def __init__(self, w=64, h=32):
        self.size = (w, h)
        self.bgra = bytes(w * h * 4)


class FakeSct:
    def __init__(self):
        self.grabs = 0

    def grab(self, bbox):
        self.grabs += 1
        return FakeRaw()


BBOX = {"width": 64, "height": 32}


class TestStabilisationSelection:
    def test_no_callable_means_frames(self):
        v = VisionEngine()
        assert v._can_use_events(123) is False

    def test_no_tick_means_frames(self):
        """Without a tick there is no way to separate a change caused by the action from
        one already in flight, so guessing would return frames caught mid-animation."""
        v = VisionEngine(quiescence=lambda *a: FakeWaitResult())
        assert v._can_use_events(None) is False

    def test_frames_mode_is_respected(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "frames")
        v = VisionEngine(quiescence=lambda *a: FakeWaitResult())
        assert v._can_use_events(123) is False

    def test_events_mode_with_a_tick_uses_events(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")
        v = VisionEngine(quiescence=lambda *a: FakeWaitResult())
        assert v._can_use_events(123) is True

    def test_the_event_path_captures_exactly_once(self, monkeypatch):
        """The whole point: no capture per iteration."""
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")
        seen = []
        v = VisionEngine(quiescence=lambda *a: seen.append(a) or FakeWaitResult())
        sct = FakeSct()
        img = v._capture_stabilized_frame(sct, BBOX, since_tick=999)
        assert img.size == (64, 32)
        assert sct.grabs == 1
        assert v.last_stabilization == "events"
        assert seen and seen[0][0] == 999

    def test_a_failing_quiescence_falls_back_to_frames(self, monkeypatch):
        """A silent or broken event stream must degrade to the path that was working,
        not to a frame captured mid-animation."""
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")

        def boom(*_a):
            raise RuntimeError("pump died")

        v = VisionEngine(quiescence=boom)
        sct = FakeSct()
        img = v._capture_stabilized_frame(sct, BBOX, since_tick=999)
        assert img.size == (64, 32)
        assert v.last_stabilization == "frames"
        assert sct.grabs >= 2

    def test_a_quiescence_error_result_also_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")
        v = VisionEngine(quiescence=lambda *a: FakeWaitResult(matched=False,
                                                             error="watcher down"))
        sct = FakeSct()
        v._capture_stabilized_frame(sct, BBOX, since_tick=999)
        assert v.last_stabilization == "frames"

    def test_a_none_result_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")
        v = VisionEngine(quiescence=lambda *a: None)
        sct = FakeSct()
        v._capture_stabilized_frame(sct, BBOX, since_tick=999)
        assert v.last_stabilization == "frames"

    def test_missing_tick_still_stabilises_by_frames(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "events")
        calls = []
        v = VisionEngine(quiescence=lambda *a: calls.append(a) or FakeWaitResult())
        sct = FakeSct()
        v._capture_stabilized_frame(sct, BBOX, since_tick=None)
        assert calls == [], "the event path must not be entered without an anchor"
        assert v.last_stabilization == "frames"


class TestChangeSignal:
    """PLAN 3.6: the perceptual difference was already computed on every stabilisation
    pass and then thrown away. Publishing it is the cheapest available evidence about
    whether an action landed."""

    def test_the_event_path_reports_a_count_and_no_diff(self):
        """The two paths report differently and both say which they are. Publishing one
        shape with the other's field silently null would hide which measurement the
        caller is looking at."""
        monkey_v = VisionEngine(quiescence=lambda *a: FakeWaitResult(waited_events=4))
        monkey_v._capture_stabilized_frame(FakeSct(), BBOX, since_tick=1)
        sig = {
            "changed": monkey_v.last_changed,
            "source": monkey_v.last_change_source,
            "diff": monkey_v.last_diff,
            "events": monkey_v.last_events_seen,
        }
        assert sig == {"changed": True, "source": "events", "diff": None, "events": 4}

    def test_no_events_means_not_changed(self):
        v = VisionEngine(quiescence=lambda *a: FakeWaitResult(waited_events=0))
        v._capture_stabilized_frame(FakeSct(), BBOX, since_tick=1)
        assert v.last_changed is False
        assert v.last_events_seen == 0

    def test_the_frame_path_reports_a_difference(self, monkeypatch):
        monkeypatch.setattr(config, "STABILIZATION_MODE", "frames")
        v = VisionEngine()
        v._capture_stabilized_frame(FakeSct(), BBOX, since_tick=1)
        assert v.last_change_source == "frames"
        assert v.last_events_seen is None
        assert v.last_diff is not None
        assert v.last_changed is not None

    def test_a_missing_engine_does_not_break_the_response(self):
        """The action already happened and its own result is the important part; losing
        a diagnostic to an AttributeError would be the worse failure."""
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        sig = d._change_signal()
        assert sig["source"] == "unavailable"
        assert sig["changed"] is None

    def test_the_signal_is_attached_to_actions_that_captured_a_frame(self):
        from autopilot_daemon import FRAME_CHANGE_ACTIONS

        assert {"mouse_action", "keyboard_action", "scroll_screen"} <= set(
            FRAME_CHANGE_ACTIONS)
        # Pure observations already return a frame as their whole answer; annotating
        # them would suggest the frame was the result of a change.
        assert "get_snapshot" not in FRAME_CHANGE_ACTIONS
        assert "read_terminal" not in FRAME_CHANGE_ACTIONS


class TestDaemonQuiescenceBridge:
    def test_disabled_watcher_returns_none(self, monkeypatch):
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", False)
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        assert d._event_quiescence(1, 100.0, 1.0) is None

    def test_a_watcher_that_will_not_start_returns_none(self, monkeypatch):
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", True)
        monkeypatch.setattr(ew, "watcher",
                            lambda: FakeWatcher(running=False, start_err="no hook"))
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        assert d._event_quiescence(1, 100.0, 1.0) is None

    def test_a_raising_watcher_returns_none_rather_than_failing_capture(self,
                                                                       monkeypatch):
        """Returning None is what makes VisionEngine fall back. Raising would fail the
        whole capture, turning a missing optimisation into a broken tool."""
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", True)
        monkeypatch.setattr(ew, "watcher", lambda: FakeWatcher(raise_on_wait=True))
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        assert d._event_quiescence(1, 100.0, 1.0) is None

    def test_a_running_watcher_is_used_without_restarting(self, monkeypatch):
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", True)
        w = FakeWatcher(running=True)
        monkeypatch.setattr(ew, "watcher", lambda: w)
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        res = d._event_quiescence(1234, 100.0, 1.0)
        assert res is not None
        assert w.calls[0][1] == 1234

    def test_the_bridge_filters_to_content_events(self, monkeypatch):
        """Without the filter the capture path waits on pointer-tracking noise.

        Measured on a working desktop: quiet_ms=200 took 2359 ms counting every ingested
        event, against 406 ms for frame differencing on the same screen -- stricter than
        the thing it replaces, which inverts the argument for it.
        """
        monkeypatch.setattr(config, "EVENT_WATCHER_ENABLED", True)
        w = FakeWatcher(running=True)
        monkeypatch.setattr(ew, "watcher", lambda: w)
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        assert d._event_quiescence(1, 100.0, 1.0) is not None
        passed = w.calls[0][5]
        assert passed == ew.QUIESCENT_EVENTS
        assert ew.EVENT_OBJECT_LOCATIONCHANGE not in passed
