"""Tests for batch execution and pre-flight assertions (PLAN 4.1, 4.2).

The step verbs themselves are covered elsewhere; what is checked here is the
orchestration: where a batch stops, that a high-risk step is stopped before it runs
rather than after, that a pre-flight mismatch reports both sides of the comparison, and
that one observation serves the whole batch.
"""
from __future__ import annotations

import asyncio

import pytest

import risk as rk
from autopilot_daemon import (
    BATCH_STOP_MODES,
    MAX_BATCH_STEPS,
    NON_INPUT_ACTIONS,
    AutopilotDaemon,
)


class FakeUIA:
    def __init__(self, names=None):
        self.names = names or {}
        self.identity_calls = []

    def node_identity(self, hwnd, path):
        self.identity_calls.append((hwnd, path))
        return self.names.get(path, {"name": "", "role": "", "pid": None})


class FakeVision:
    def __init__(self, raise_on_capture=False):
        self.captures = 0
        self.raise_on_capture = raise_on_capture
        self.last_stabilization = "events"
        self.last_stabilization_ms = 12.0

    def capture_screen(self, **kwargs):
        self.captures += 1
        if self.raise_on_capture:
            raise RuntimeError("capture failed")
        return ("B64", 1280, 720, 1.5, 1.5, None)


class FakeInput:
    def get_cursor_pos(self):
        return (10, 10)


def make_daemon(*, step_results=None, names=None, capture_raises=False,
                handle=42):
    """A daemon whose step verbs return canned envelopes.

    `_action_element_action` and `_action_window_action` are replaced wholesale: they
    carry their own tests, and re-running the guard and classification here would make
    these tests about the wrong thing.
    """
    d = AutopilotDaemon.__new__(AutopilotDaemon)
    d.uia = FakeUIA(names)
    d.vision = FakeVision(raise_on_capture=capture_raises)
    d.input_ctrl = FakeInput()
    d.calls = []

    async def resolve(_params):
        return handle, "hwnd"

    d._resolve_target = resolve

    queue = list(step_results or [])
    # Indexed by step, not popped per call: with a risk stop mode every step makes TWO
    # calls, a dry run to classify and then the real one. Popping per call would hand
    # the dry run the next step's envelope and desynchronise the whole sequence.
    state = {"i": 0}

    async def fake_step(kind, step, base, dry_run):
        d.calls.append((kind, step.get("action"), step.get("path"), dry_run))
        i = state["i"]
        item = queue[i] if i < len(queue) else None
        if isinstance(item, tuple):
            item = item[0] if dry_run else item[1]
        if item is None:
            item = {"status": "ok",
                    "_risk": rk.Risk(level=rk.REVERSIBLE, reason="ok"),
                    "data": {"ok": True, "verified": True,
                             "action": step.get("action"), "path": step.get("path"),
                             "message": "done"}}
        if dry_run:
            # A dry run reports the classification and nothing else; the production
            # handler does the same, so the batch reads `_risk` and `resolved_name`.
            return {"status": item.get("status", "ok"),
                    "dry_run": True,
                    "_risk": item.get("_risk"),
                    "data": item.get("data", {})}
        state["i"] += 1
        return item

    d._run_batch_step = fake_step
    return d


def ok_envelope(action, path, level=rk.REVERSIBLE, verified=True, name=""):
    return {
        "status": "ok",
        "_risk": rk.Risk(level=level, reason="test"),
        "data": {"ok": True, "verified": verified, "action": action, "path": path,
                 "resolved_name": name, "message": "done"},
    }


def failed_envelope(action, path, verified=False):
    return {
        "status": "ok",
        "_risk": rk.Risk(level=rk.REVERSIBLE, reason="test"),
        "data": {"ok": False, "verified": verified, "action": action, "path": path,
                 "message": "not verified"},
    }


def run(d, params):
    return asyncio.run(d._action_execute_batch(params))


def steps(n, verb="select"):
    return [{"kind": "element", "action": verb, "path": f"1.{i}"} for i in range(n)]


# ------------------------------------------------------------------ validation
class TestBatchValidation:
    @pytest.mark.parametrize("bad", [None, [], "x", {}, 5])
    def test_steps_must_be_a_non_empty_list(self, bad):
        d = make_daemon()
        out = run(d, {"steps": bad})
        assert out["status"] == "error"
        assert d.calls == [], "nothing may run when the request is invalid"

    def test_the_step_cap_is_named_in_the_error(self):
        d = make_daemon()
        out = run(d, {"steps": steps(MAX_BATCH_STEPS + 1)})
        assert out["status"] == "error"
        assert str(MAX_BATCH_STEPS) in out["message"]

    def test_a_batch_at_the_cap_is_accepted(self):
        d = make_daemon(step_results=[ok_envelope("select", "x")] * MAX_BATCH_STEPS)
        out = run(d, {"steps": steps(MAX_BATCH_STEPS)})
        assert out["status"] == "ok"
        assert out["data"]["executed"] == MAX_BATCH_STEPS

    def test_unknown_stop_on_is_rejected(self):
        d = make_daemon()
        out = run(d, {"steps": steps(1), "stop_on": "sometimes"})
        assert out["status"] == "error"
        assert sorted(BATCH_STOP_MODES)[0] in out["message"]

    def test_never_is_not_a_stop_mode(self):
        """'never' would execute an irreversible step inside a batch with no chance to
        stop, which is the one thing the risk classification exists to prevent."""
        d = make_daemon()
        out = run(d, {"steps": steps(1), "stop_on": "never"})
        assert out["status"] == "error"
        assert "never" in out["message"]

    def test_a_non_object_step_stops_the_batch(self):
        d = make_daemon()
        out = run(d, {"steps": [{"kind": "element", "action": "select", "path": "1"},
                                "not a step"]})
        assert out["data"]["stopped_at"] == 1
        assert out["data"]["executed"] == 1

    def test_class_a_steps_are_refused_with_the_reason(self):
        """Refused rather than run one at a time: batching them properly needs one input
        acquisition for the whole batch, and running them per step inside something
        called a batch would claim an atomicity it does not have."""
        d = make_daemon()
        out = run(d, {"steps": [{"kind": "mouse", "action": "click", "x": 1, "y": 2}]})
        assert out["data"]["stopped_at"] == 0
        assert out["data"]["executed"] == 0
        assert d.calls == []
        reason = out["data"]["stop_reason"]
        assert "BlockInput" in reason or "Класса A" in reason


# ------------------------------------------------------------------ happy path
class TestBatchExecution:
    def test_every_step_runs_and_one_observation_serves_the_batch(self):
        d = make_daemon(step_results=[ok_envelope("select", f"1.{i}") for i in range(8)])
        out = run(d, {"steps": steps(8)})
        data = out["data"]
        assert data["complete"] is True and data["stopped_at"] is None
        assert data["executed"] == 8 and data["steps_total"] == 8
        assert len(data["results"]) == 8
        assert d.vision.captures == 1, "one frame for the batch, not one per step"
        assert data["screenshot"] == "B64"

    def test_observation_can_be_skipped(self):
        d = make_daemon(step_results=[ok_envelope("select", "1.0")])
        out = run(d, {"steps": steps(1), "observe_at_end": False})
        assert d.vision.captures == 0
        assert "screenshot" not in out["data"]

    def test_window_steps_dispatch_to_the_window_handler(self):
        d = make_daemon(step_results=[ok_envelope("close", "42")])
        out = run(d, {"steps": [{"kind": "window", "action": "close"}]})
        assert out["data"]["executed"] == 1
        assert d.calls[0][0] == "window"

    def test_kind_defaults_to_element(self):
        d = make_daemon(step_results=[ok_envelope("select", "1.0")])
        run(d, {"steps": [{"action": "select", "path": "1.0"}]})
        assert d.calls[0][0] == "element"

    def test_each_step_reports_the_level_it_resolved_to(self):
        """How a mis-bind becomes visible: a path that landed on Send instead of Cancel
        is reported as irreversible even though the caller asked for something harmless."""
        d = make_daemon(step_results=[
            ok_envelope("invoke", "1.0", level=rk.IRREVERSIBLE, name="Отправить"),
        ])
        out = run(d, {"steps": steps(1, "invoke"), "stop_on": "failure"})
        r = out["data"]["results"][0]
        assert r["risk_level"] == rk.IRREVERSIBLE
        assert r["resolved_name"] == "Отправить"


# ------------------------------------------------------------- stop semantics
class TestBatchStopping:
    def test_a_failed_step_stops_the_batch_and_reports_how_far_it_got(self):
        d = make_daemon(step_results=[
            ok_envelope("select", "1.0"),
            failed_envelope("set_value", "1.1"),
            ok_envelope("select", "1.2"),
        ])
        out = run(d, {"steps": steps(3, "select"), "stop_on": "failure"})
        data = out["data"]
        assert data["stopped_at"] == 1
        # Dispatched and succeeded are different numbers, and the difference is the
        # point: a step that ran and did not verify still ran.
        assert data["executed"] == 2
        assert data["succeeded"] == 1
        assert data["complete"] is False
        assert len(data["results"]) == 2, "the third step must not have run"
        assert d.calls[-1][2] == "1.1"

    def test_unverified_counts_as_failure(self):
        """verified=False is a real failure: the call was accepted and the state did not
        change. Treating it as success would report a batch that did nothing."""
        d = make_daemon(step_results=[failed_envelope("toggle", "1.0", verified=False)])
        out = run(d, {"steps": steps(1, "toggle"), "stop_on": "failure"})
        assert out["data"]["stopped_at"] == 0

    def test_verified_none_is_not_a_failure(self):
        """None means the action has no readable state, which is a weaker claim than
        success but not a failure either -- conflating them would stop every invoke."""
        d = make_daemon(step_results=[
            {"status": "ok", "_risk": rk.Risk(level=rk.REVERSIBLE, reason="t"),
             "data": {"ok": True, "verified": None, "action": "invoke", "path": "1.0",
                      "message": "accepted"}}])
        out = run(d, {"steps": steps(1, "invoke"), "stop_on": "failure"})
        assert out["data"]["complete"] is True
        assert out["data"]["results"][0]["verified"] is None

    def test_a_high_risk_step_is_stopped_before_it_runs(self):
        d = make_daemon(step_results=[
            ok_envelope("select", "1.0"),
            ok_envelope("invoke", "1.1", level=rk.IRREVERSIBLE, name="Отправить"),
        ])
        out = run(d, {"steps": steps(2), "stop_on": "both"})
        data = out["data"]
        assert data["stopped_at"] == 1
        assert data["executed"] == 1, "only the first step may have been dispatched"
        entry = data["results"][-1]
        assert entry["status"] == "stopped_before"
        assert entry["risk_level"] == rk.IRREVERSIBLE
        assert entry["resolved_name"] == "Отправить"
        # The dry run classified step 1; no real call was made for it.
        assert [c[3] for c in d.calls] == [True, False, True]

    def test_medium_risk_also_stops_the_batch(self):
        """batchable is derived as level == REVERSIBLE, so medium is not batchable
        either. Only reversible steps may appear in a batch (ADR-0004)."""
        assert rk.Risk(level=rk.MEDIUM, reason="t").batchable is False
        assert rk.Risk(level=rk.REVERSIBLE, reason="t").batchable is True
        d = make_daemon(step_results=[ok_envelope("close", "42", level=rk.MEDIUM)])
        out = run(d, {"steps": [{"kind": "window", "action": "close"}],
                      "stop_on": "high_risk"})
        assert out["data"]["results"][-1]["status"] == "stopped_before"
        assert out["data"]["executed"] == 0

    def test_batchable_cannot_disagree_with_the_level(self):
        """Pinned because the batch trusts the flag alone.

        `batchable` is a property derived from `level`, not a field, so an inconsistent
        Risk cannot be constructed. If it ever becomes a field, this test fails and the
        batch has to start checking the level as well -- trusting a flag that a single
        object could contradict would let an irreversible step run inside a batch.
        """
        import dataclasses

        assert not any(f.name == "batchable" for f in dataclasses.fields(rk.Risk)), \
            "batchable became a field; the batch must re-check level too"
        for level in (rk.REVERSIBLE, rk.MEDIUM, rk.IRREVERSIBLE):
            r = rk.Risk(level=level, reason="t")
            assert r.batchable == (level == rk.REVERSIBLE)

    def test_stop_on_failure_alone_does_not_preclassify(self):
        """With no risk stop there is nothing to gain from the extra path walk, so the
        level is read off the result instead."""
        d = make_daemon(step_results=[ok_envelope("select", "1.0")])
        run(d, {"steps": steps(1), "stop_on": "failure"})
        assert [c[3] for c in d.calls] == [False], "no dry run when it cannot stop"

    def test_a_refused_step_stops_the_batch(self):
        d = make_daemon(step_results=[
            {"status": "refused", "guard": "host_process_tree",
             "message": "отказано", "data": {}},
            ok_envelope("select", "1.1"),
        ])
        out = run(d, {"steps": steps(2), "stop_on": "failure"})
        assert out["data"]["stopped_at"] == 0
        assert out["data"]["results"][0]["guard"] == "host_process_tree"
        assert len(out["data"]["results"]) == 1


# ---------------------------------------------------------- pre-flight checks
class TestPreflight:
    def test_a_mismatch_aborts_and_reports_both_sides(self):
        """Both values come back because "the assertion failed" alone leaves the caller
        unable to tell a stale path from a genuinely wrong recipient, and those need
        opposite responses."""
        d = make_daemon(names={"0.5": {"name": "Bob", "role": "Text", "pid": 9}})
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": {"path": "0.5",
                                            "name_contains": "Alice"}}]})
        data = out["data"]
        assert data["executed"] == 0
        assert data["stopped_at"] == 0
        entry = data["results"][0]
        assert entry["status"] == "preflight_mismatch"
        assert entry["actual"] == "Bob"
        assert entry["expected_contains"] == "Alice"

    def test_a_matching_assertion_lets_the_step_run(self):
        d = make_daemon(names={"0.5": {"name": "Alice Smith", "role": "Text", "pid": 9}},
                        step_results=[ok_envelope("invoke", "3.1")])
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": {"path": "0.5",
                                            "name_contains": "alice"}}]})
        assert out["data"]["executed"] == 1
        assert "preflight" not in out["data"]["results"][0]["status"]

    def test_exact_match_is_case_sensitive(self):
        d = make_daemon(names={"0.5": {"name": "Alice", "role": "Text", "pid": 9}})
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": {"path": "0.5", "name_equals": "alice"}}]})
        assert out["data"]["results"][0]["status"] == "preflight_mismatch"

    def test_role_can_be_checked_too(self):
        d = make_daemon(names={"0.5": {"name": "Send", "role": "Button", "pid": 9}})
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": {"path": "0.5", "role": "Edit"}}]})
        entry = out["data"]["results"][0]
        assert entry["status"] == "preflight_mismatch"
        assert "роль" in entry["message"]

    def test_an_assertion_without_a_path_is_an_error_not_a_pass(self):
        """Silently skipping a check the caller asked for would report a verified batch
        that was never verified."""
        d = make_daemon()
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": {"name_contains": "Alice"}}]})
        assert out["data"]["executed"] == 0
        assert out["data"]["results"][0]["status"] == "preflight_error"

    def test_a_malformed_assertion_is_reported(self):
        d = make_daemon()
        out = run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                                 "assert": "Alice"}]})
        assert out["data"]["results"][0]["status"] == "preflight_error"

    def test_the_assertion_is_read_before_the_step_executes(self):
        order = []
        d = make_daemon(names={"0.5": {"name": "Bob", "role": "T", "pid": 9}})
        original = d._run_batch_step

        async def tracked(kind, step, base, dry_run):
            order.append("step")
            return await original(kind, step, base, dry_run)

        d._run_batch_step = tracked
        run(d, {"steps": [{"kind": "element", "action": "invoke", "path": "3.1",
                           "assert": {"path": "0.5", "name_contains": "Alice"}}]})
        assert order == [], "a failed pre-flight must prevent the step entirely"


# ------------------------------------------------------------- observability
class TestBatchObservability:
    def test_a_failed_capture_does_not_erase_the_step_results(self):
        """The steps already ran. Losing their outcomes would be worse than losing a
        frame, and the caller can re-observe."""
        d = make_daemon(step_results=[ok_envelope("select", "1.0")],
                        capture_raises=True)
        out = run(d, {"steps": steps(1)})
        data = out["data"]
        assert data["executed"] == 1
        assert len(data["results"]) == 1
        assert data["screenshot"] is None
        assert "capture failed" in data["observation_error"]

    def test_the_stabilisation_mode_is_reported(self):
        d = make_daemon(step_results=[ok_envelope("select", "1.0")])
        out = run(d, {"steps": steps(1)})
        assert out["data"]["stabilization"] == "events"

    def test_the_message_states_completion_or_the_stop(self):
        d = make_daemon(step_results=[ok_envelope("select", "1.0")] * 3)
        out = run(d, {"steps": steps(3)})
        assert "полностью" in out["data"]["message"]

    def test_the_batch_takes_the_input_lock(self):
        """It mutates application state, so it must serialise against in-flight input and
        stay blocked while a human holds the machine."""
        assert "execute_batch" not in NON_INPUT_ACTIONS
