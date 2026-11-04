"""Tests for the safety layer as wired into the daemon.

The pure modules have their own tests; what is checked here is the wiring, because a
refusal path that exists but is never reached is not a safety property. The cases
that matter are the ones where the guard must fire *before* anything happens and the
ones where it must fail closed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import audit as au
import config
import host_guard as hg
import risk as rk
from autopilot_daemon import AutopilotDaemon
from uia_client import ActionResult


# --------------------------------------------------------------------- helpers
class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)

    @property
    def last(self):
        return self.records[-1] if self.records else None


class FakeUIA:
    """Enough of UIAClient for one element verb."""

    def __init__(self, name="Target", pid=500, ignore=False):
        self.calls = []
        self._name = name
        self._pid = pid
        self._ignore = ignore

    def node_identity(self, hwnd, path):
        return {"name": self._name, "role": "Button", "pid": self._pid}

    def invoke(self, hwnd, path):
        self.calls.append(("invoke", hwnd, path))
        return ActionResult(True, "invoke", path, None,
                            message="Вызов принят. Эффект не проверяем.")

    def select_item(self, hwnd, path):
        self.calls.append(("select", hwnd, path))
        return ActionResult(True, "select_item", path, True, before="False",
                            after="True", message="Выбор подтверждён.")

    def target_info(self, hwnd):
        return {"handle": hwnd, "title": "Some window", "class_name": "X",
                "pid": self._pid}


def make_daemon(*, tree=None, uia=None, audit=None):
    d = AutopilotDaemon.__new__(AutopilotDaemon)
    d.uia = uia or FakeUIA()
    d.audit = audit or RecordingAudit()
    d.host_tree = tree
    d._input_lock = asyncio.Lock()
    d._active_monitor_index = None

    async def resolve(_params):
        return 42, "hwnd"

    d._resolve_target = resolve
    return d


def tree_protecting(*pids):
    return hg.HostTree(root_pid=min(pids) if pids else 0,
                       pids=frozenset(pids),
                       names={p: f"proc{p}.exe" for p in pids})


# ---------------------------------------------------------------- host refusal
class TestHostRefusal:
    def test_refuses_a_protected_pid(self):
        d = make_daemon(tree=tree_protecting(500))
        out = d._host_refusal(500, False, "узел 0", "element_action")
        assert out is not None
        assert out["status"] == "refused"
        assert out["guard"] == "host_process_tree"
        assert "proc500.exe" in out["message"]

    def test_allows_an_unprotected_pid(self):
        d = make_daemon(tree=tree_protecting(500))
        assert d._host_refusal(999, False, "узел 0", "element_action") is None

    def test_fails_closed_when_the_tree_was_never_armed(self):
        """A gateway that never sent its PID must get a refusal, not a free pass.

        This is the direction that is easy to get wrong: "no guard configured" reads
        like "nothing to check", and the result is text typed into the window that
        renders the conversation.
        """
        d = make_daemon(tree=None)
        out = d._host_refusal(999, False, "узел 0", "element_action")
        assert out is not None
        assert out["status"] == "refused"
        assert "не вооружена" in out["message"]

    def test_override_is_recorded_rather_than_silent(self):
        audit = RecordingAudit()
        d = make_daemon(tree=tree_protecting(500), audit=audit)
        assert d._host_refusal(500, True, "узел 0", "element_action") is None
        rec = audit.last
        assert rec is not None
        assert rec.kind == "host_override"
        assert rec.ok is True

    def test_disabling_the_guard_allows_everything(self, monkeypatch):
        monkeypatch.setattr(config, "HOST_GUARD_ENABLED", False)
        d = make_daemon(tree=tree_protecting(500))
        assert d._host_refusal(500, False, "узел 0", "element_action") is None

    def test_unusable_tree_refuses(self):
        d = make_daemon(tree=hg.HostTree(root_pid=1, error="empty snapshot"))
        out = d._host_refusal(500, False, "узел 0", "element_action")
        assert out is not None and out["status"] == "refused"


class TestLearnHostPid:
    def test_builds_the_tree_once(self, monkeypatch):
        calls = []

        def fake_build(pid):
            calls.append(pid)
            return tree_protecting(pid)

        monkeypatch.setattr(hg, "build_host_tree", fake_build)
        d = make_daemon(tree=None)
        d._learn_host_pid(500)
        d._learn_host_pid(600)
        assert calls == [500], "the tree is session state, not a per-call lookup"
        assert d.host_tree.root_pid == 500

    @pytest.mark.parametrize("bad", [None, "abc", 0, -5, ""])
    def test_ignores_junk_without_arming(self, bad, monkeypatch):
        monkeypatch.setattr(hg, "build_host_tree",
                            lambda pid: pytest.fail("must not build on junk input"))
        d = make_daemon(tree=None)
        d._learn_host_pid(bad)
        assert d.host_tree is None

    def test_accepts_a_numeric_string(self, monkeypatch):
        monkeypatch.setattr(hg, "build_host_tree", lambda pid: tree_protecting(pid))
        d = make_daemon(tree=None)
        d._learn_host_pid("777")
        assert d.host_tree is not None and d.host_tree.root_pid == 777


# --------------------------------------------------------------- element verbs
class TestElementActionGuards:
    def test_refusal_happens_before_the_handler(self):
        """The distinction that makes the guard a guard rather than a log line."""
        uia = FakeUIA(pid=500)
        d = make_daemon(tree=tree_protecting(500), uia=uia)
        out = asyncio.run(d._action_element_action(
            {"action": "invoke", "path": "0.1", "hwnd": 42}))
        assert out["status"] == "refused"
        assert uia.calls == [], "nothing may be invoked on a refused target"

    def test_unprotected_target_is_executed(self):
        uia = FakeUIA(pid=999)
        d = make_daemon(tree=tree_protecting(500), uia=uia)
        out = asyncio.run(d._action_element_action(
            {"action": "invoke", "path": "0.1", "hwnd": 42}))
        assert out["status"] == "ok"
        assert uia.calls == [("invoke", 42, "0.1")]

    def test_dry_run_resolves_and_classifies_but_does_not_act(self):
        uia = FakeUIA(pid=999, name="Send")
        d = make_daemon(tree=tree_protecting(500), uia=uia)
        out = asyncio.run(d._action_element_action(
            {"action": "invoke", "path": "0.1", "hwnd": 42, "dry_run": True}))
        assert out["status"] == "ok" and out["dry_run"] is True
        assert uia.calls == [], "a dry run must not press anything"
        data = out["data"]
        assert data["resolved_name"] == "Send"
        assert data["risk_level"] == rk.IRREVERSIBLE
        assert data["batchable"] is False

    def test_risk_comes_from_the_resolved_element_not_the_request(self):
        """The model asked for a harmless verb; the path landed on Send."""
        uia = FakeUIA(pid=999, name="Отправить")
        d = make_daemon(tree=tree_protecting(500), uia=uia)
        out = asyncio.run(d._action_element_action(
            {"action": "select", "path": "0.1", "hwnd": 42}))
        assert out["data"]["risk_level"] == rk.IRREVERSIBLE
        assert out["data"]["resolved_name"] == "Отправить"

    def test_risk_travels_with_the_envelope_for_the_trace(self):
        """dispatch_action pops `_risk` to build the record, so it must be present."""
        d = make_daemon(tree=tree_protecting(500), uia=FakeUIA(pid=999))
        envelope = asyncio.run(d._action_element_action(
            {"action": "invoke", "path": "0.1", "hwnd": 42}))
        assert "_risk" in envelope

    def test_a_missing_path_still_names_the_verbs(self):
        d = make_daemon(tree=tree_protecting(500))
        out = asyncio.run(d._action_element_action({"action": "invoke"}))
        assert out["status"] == "error"
        assert "path" in out["message"].lower()


class TestWindowActionGuards:
    def test_refusal_happens_before_the_handler(self):
        d = make_daemon(tree=tree_protecting(500))
        called = []
        d.uia = FakeUIA(pid=500)
        d.uia.close_window = lambda h: called.append(h)
        out = asyncio.run(d._action_window_action(
            {"action": "close", "hwnd": 42}))
        assert out["status"] == "refused"
        assert called == []

    def test_dry_run_does_not_close(self):
        d = make_daemon(tree=tree_protecting(500))
        called = []
        d.uia = FakeUIA(pid=999)
        d.uia.close_window = lambda h: called.append(h)
        out = asyncio.run(d._action_window_action(
            {"action": "close", "hwnd": 42, "dry_run": True}))
        assert out["dry_run"] is True
        assert called == []
        assert out["data"]["risk_level"] in (rk.MEDIUM, rk.IRREVERSIBLE)


# ----------------------------------------------------------------- trace wiring
class TestTraceWiring:
    def _daemon_with_dispatch(self, monkeypatch, resp):
        d = make_daemon(tree=tree_protecting(500))

        async def fake_dispatch(action, params):
            return dict(resp)

        d._dispatch = fake_dispatch
        monkeypatch.setattr(AutopilotDaemon, "_annotate_session",
                            staticmethod(lambda r: r))
        return d

    def test_every_action_is_traced(self, monkeypatch):
        d = self._daemon_with_dispatch(monkeypatch,
                                       {"status": "ok", "data": {"verified": True}})
        asyncio.run(d.dispatch_action("get_snapshot", {"hwnd": 42}))
        rec = d.audit.last
        assert rec.action == "get_snapshot"
        assert rec.verified is True
        assert rec.target == "42"
        assert rec.elapsed_ms is not None

    def test_refusals_are_traced_with_the_guard_named(self, monkeypatch):
        d = self._daemon_with_dispatch(
            monkeypatch, {"status": "refused", "guard": "host_process_tree",
                          "message": "no"})
        asyncio.run(d.dispatch_action("element_action", {"path": "0.1"}))
        rec = d.audit.last
        assert rec.status == "refused"
        assert rec.extra.get("guard") == "host_process_tree"

    def test_risk_is_moved_from_the_response_into_the_record(self, monkeypatch):
        level = rk.Risk(level=rk.IRREVERSIBLE, reason="send")
        d = self._daemon_with_dispatch(
            monkeypatch, {"status": "ok", "data": {"ok": True}, "_risk": level})
        out = asyncio.run(d.dispatch_action("element_action", {"path": "0.1"}))
        assert "_risk" not in out, "the internal marker must not reach the client"
        assert d.audit.last.level == rk.IRREVERSIBLE

    def test_class_is_recorded_for_input_actions(self, monkeypatch):
        d = self._daemon_with_dispatch(monkeypatch, {"status": "ok", "data": {}})
        asyncio.run(d.dispatch_action("mouse_action", {"x": 1, "y": 2}))
        assert d.audit.last.klass == au.CLASS_A
        asyncio.run(d.dispatch_action("element_action", {"path": "0.1"}))
        assert d.audit.last.klass == au.CLASS_B

    def test_a_broken_trace_does_not_break_the_action(self, monkeypatch):
        """The trace is bookkeeping. A failure in it must not fail a click."""
        d = self._daemon_with_dispatch(monkeypatch, {"status": "ok", "data": {}})

        def boom(_rec):
            raise RuntimeError("disk on fire")

        d.audit.record = boom
        out = asyncio.run(d.dispatch_action("get_snapshot", {"hwnd": 1}))
        assert out["status"] == "ok"

    def test_frame_hash_is_recorded_when_a_screenshot_came_back(self, monkeypatch):
        d = self._daemon_with_dispatch(
            monkeypatch, {"status": "ok", "data": {"screenshot": "QUJD"}})
        asyncio.run(d.dispatch_action("mouse_action", {"x": 1, "y": 2}))
        assert d.audit.last.frame_after == au.frame_hash(b"QUJD")

    def test_no_screenshot_means_no_hash(self, monkeypatch):
        d = self._daemon_with_dispatch(monkeypatch, {"status": "ok", "data": {}})
        asyncio.run(d.dispatch_action("element_action", {"path": "0.1"}))
        assert d.audit.last.frame_after is None

    def test_target_falls_back_to_coordinates(self, monkeypatch):
        d = self._daemon_with_dispatch(monkeypatch, {"status": "ok", "data": {}})
        asyncio.run(d.dispatch_action("mouse_action", {"x": 10, "y": 20}))
        assert d.audit.last.target == "10,20"


class TestTraceEndToEnd:
    def test_records_land_as_parseable_jsonl(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        d = make_daemon(tree=tree_protecting(500), audit=trail)

        async def fake_dispatch(action, params):
            return {"status": "ok", "data": {"verified": None}}

        d._dispatch = fake_dispatch
        # Synchronous, because _annotate_session is called without await.
        d._annotate_session = lambda r: r
        asyncio.run(d.dispatch_action("invoke", {"path": "0.1"}))
        lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["action"] == "invoke"
        assert rec["verified"] is None
