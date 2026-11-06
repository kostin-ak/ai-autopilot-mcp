"""A capture that made no comparison must not publish a previous capture's verdict.

take_control captures with wait_stabilization=False and is in FRAME_CHANGE_ACTIONS, so it
attaches a screen_change block to its response. Before the reset, that block carried
whatever the preceding action had concluded -- and it read as evidence take_control had
produced, on a frame take_control had just taken. The value is stale but the presentation
is authoritative, which is the combination that misleads.
"""
from __future__ import annotations

import inspect

import pytest

import vision_engine
from autopilot_daemon import FRAME_CHANGE_ACTIONS, AutopilotDaemon


def _engine(**fields):
    ve = vision_engine.VisionEngine.__new__(vision_engine.VisionEngine)
    ve.last_stabilization_ms = 0.0
    for k, v in fields.items():
        setattr(ve, k, v)
    return ve


def _signal(ve):
    """Runs the daemon's own block builder against a given engine."""
    d = AutopilotDaemon.__new__(AutopilotDaemon)
    d.vision = ve
    return d._change_signal()


class TestReset:
    def test_it_clears_every_change_field(self):
        ve = _engine(last_changed=True, last_diff=0.42, last_events_seen=7,
                     last_change_source="events")
        ve._reset_change_fields()
        assert ve.last_changed is None
        assert ve.last_diff is None
        assert ve.last_events_seen is None
        assert ve.last_change_source == "none"

    def test_it_is_idempotent(self):
        ve = _engine(last_changed=None, last_diff=None, last_events_seen=None,
                     last_change_source="")
        ve._reset_change_fields()
        ve._reset_change_fields()
        assert ve.last_change_source == "none"

    def test_the_block_then_reports_not_compared(self):
        """changed=None is the claim that matters: it is a different statement from
        changed=False, and only the first one says no comparison happened."""
        ve = _engine()
        ve._reset_change_fields()
        sig = _signal(ve)
        assert sig["changed"] is None
        assert sig["source"] == "none"
        assert sig["diff"] is None and sig["events"] is None

    def test_the_unstabilised_branch_actually_calls_it(self):
        """The reset existing is not the same as the reset being wired. This is the
        assertion that catches it being defined and never called."""
        src = inspect.getsource(vision_engine.VisionEngine.capture_screen)
        assert "_reset_change_fields()" in src

    def test_the_call_follows_the_mode_being_set_to_none(self):
        """So the two cannot disagree: a response claiming stabilization "none" while
        carrying a change verdict is exactly what the reset removes."""
        src = inspect.getsource(vision_engine.VisionEngine.capture_screen)
        assert src.index("_reset_change_fields()") > src.index(
            'self.last_stabilization = "none"')


class TestTheActionItProtects:
    def test_take_control_captures_without_stabilization(self):
        src = inspect.getsource(AutopilotDaemon._action_take_control)
        assert "wait_stabilization=False" in src

    def test_take_control_still_reports_a_change_block(self):
        """Both halves of the defect: it captures without comparing, and it publishes a
        block. Removing either would make the reset unnecessary -- and this test is the
        signal that it has become so."""
        assert "take_control" in FRAME_CHANGE_ACTIONS

    @pytest.mark.parametrize("action", sorted(FRAME_CHANGE_ACTIONS))
    def test_every_change_reporting_action_has_a_handler(self, action):
        """A name in FRAME_CHANGE_ACTIONS with no handler would attach a block built from
        fields nothing ever set."""
        assert hasattr(AutopilotDaemon, f"_action_{action}"), action

    def test_the_block_is_attached_in_one_place(self):
        """Attached in dispatch_action rather than in each response builder, so the
        builders cannot drift apart -- the same failure the unified frame shape removed."""
        src = inspect.getsource(AutopilotDaemon.dispatch_action)
        assert "_change_signal()" in src
        assert "FRAME_CHANGE_ACTIONS" in src


class TestResettingDoesNotBecomeNeverReporting:
    def test_the_frame_path_still_reports_its_verdict(self):
        ve = _engine(last_changed=False, last_diff=0.0, last_events_seen=None,
                     last_change_source="frames")
        sig = _signal(ve)
        assert sig["changed"] is False
        assert sig["source"] == "frames"
        assert sig["diff"] == 0.0

    def test_the_event_path_still_reports(self):
        ve = _engine(last_changed=True, last_diff=None, last_events_seen=4,
                     last_change_source="events")
        sig = _signal(ve)
        assert sig["changed"] is True
        assert sig["source"] == "events" and sig["events"] == 4

    def test_a_changed_screen_is_still_distinguishable_from_an_unchanged_one(self):
        """The whole point of the signal: a dead click looks like a successful one without
        it. Resetting the fields must not collapse that distinction."""
        changed = _signal(_engine(last_changed=True, last_diff=0.31,
                                  last_events_seen=None, last_change_source="frames"))
        unchanged = _signal(_engine(last_changed=False, last_diff=0.0,
                                    last_events_seen=None,
                                    last_change_source="frames"))
        assert changed["changed"] is True and unchanged["changed"] is False
        assert changed["diff"] > unchanged["diff"]

    def test_a_missing_engine_does_not_break_the_response(self):
        """The action already happened; losing a diagnostic to an AttributeError would be
        the worse failure."""
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        sig = d._change_signal()
        assert sig["changed"] is None
        assert sig["source"] == "unavailable"
