"""Deterministic tests for the safety layer: risk classification, host-tree guard,
and the audit trail.

None of these can be validated against a live desktop, because the interesting
cases are the ones that must NOT happen: a click that lands on the host's own chat
window, a batch that runs past an irreversible step, an escalation nobody can
explain afterwards. So they are pinned here against synthetic inputs, where the
absence of an action is as checkable as its presence.
"""
from __future__ import annotations

import json

import pytest

import audit as au
import host_guard as hg
import risk as rk


# ----------------------------------------------------------------- risk levels
class TestRiskStems:
    """Matching is word-boundary based, and the difference is not theoretical."""

    @pytest.mark.parametrize("name", [
        "Send", "Send message", "Отправить", "Отправка", "DELETE", "Удалить",
        "Purchase now", "Купить", "Submit", "Опубликовать", "Format disk",
        "Uninstall", "Drop table", "Оплатить счёт", "Подтвердить",
    ])
    def test_destructive_names_are_irreversible(self, name):
        r = rk.classify_action("invoke", control_name=name)
        assert r.level == rk.IRREVERSIBLE, name
        assert name.lower()[:6].lower() in r.reason.lower() or r.reason

    @pytest.mark.parametrize("name", [
        # The false positives a substring rule produces, and that would make the
        # classifier unusable if they were classified as destructive.
        "Dropdown", "Drop-down list", "Sender", "Sent items",
        "Поиск", "Filter", "Настройки", "Отмена", "Открыть",
    ])
    def test_lookalikes_are_not_destructive(self, name):
        r = rk.classify_action("invoke", control_name=name)
        assert r.level != rk.IRREVERSIBLE, f"{name!r} misclassified as irreversible"

    @pytest.mark.parametrize("name", ["Удалённые файлы", "Delete key help"])
    def test_known_over_match_is_conservative_not_permissive(self, name):
        """Two labels the stem rules cannot separate from the destructive ones.

        "Удалённые файлы" is a folder and opening it is navigation, but the Cyrillic
        stem "удал" is open on the right so it also fires here. "Delete key help"
        names the key, not an action. Both are pinned as known over-matches rather
        than hidden, because the direction is the safe one: an over-classified
        action is refused or kept out of a batch, an under-classified one is
        executed. Fixing them needs a lexicon of every noun form in every
        application, which is not what a stem list is for.
        """
        r = rk.classify_action("invoke", control_name=name)
        assert r.level == rk.IRREVERSIBLE
        assert r.reason, "an over-match must still say which rule fired"

    def test_dropdown_is_not_drop(self):
        """`drop` is on the destructive list; `Dropdown` must not match it."""
        assert rk.classify_action("invoke", control_name="Dropdown").level != rk.IRREVERSIBLE
        assert rk.classify_action("invoke", control_name="Drop").level == rk.IRREVERSIBLE

    def test_english_plural_forms_still_match(self):
        for name in ("Sends", "Deletes"):
            assert rk.classify_action("invoke", control_name=name).level == rk.IRREVERSIBLE

    def test_cyrillic_stem_covers_inflected_forms(self):
        for form in ("Отправить", "Отправка", "Отправит", "Отправлено"):
            assert rk.classify_action("invoke", control_name=form).level == rk.IRREVERSIBLE, form


class TestRiskVerbs:
    @pytest.mark.parametrize("verb", sorted(rk.REVERSIBLE_VERBS))
    def test_reversible_verbs_start_reversible(self, verb):
        assert rk.classify_action(verb).level == rk.REVERSIBLE

    @pytest.mark.parametrize("verb", sorted(rk.MEDIUM_VERBS))
    def test_writing_verbs_start_medium(self, verb):
        assert rk.classify_action(verb).level == rk.MEDIUM

    def test_invoke_without_a_name_is_medium_not_reversible(self):
        """A press whose target is unknown cannot be called free to repeat."""
        assert rk.classify_action("invoke").level == rk.MEDIUM

    def test_unknown_verb_defaults_to_medium(self):
        """An unrecognized verb is not a reason to lower the level."""
        assert rk.classify_action("detonate").level == rk.MEDIUM

    def test_a_destructive_name_tightens_a_reversible_verb(self):
        """Selecting the row named 'Удалить' is not the same as selecting a row."""
        r = rk.classify_action("select", control_name="Удалить всё")
        assert r.level == rk.IRREVERSIBLE


class TestRiskMonotonicity:
    def test_policy_only_tightens(self):
        base = rk.classify_action("select", control_name="Item")
        tight = rk.classify_action("select", control_name="Item",
                                   app="AyuGram", critical_apps=("ayugram",))
        assert rk._ORDER[tight.level] >= rk._ORDER[base.level]

    def test_a_reversible_stem_never_rescues_a_destructive_name(self):
        """'Отменить удаление' still names the destructive act."""
        r = rk.classify_action("invoke", control_name="Отменить удаление")
        assert r.level == rk.IRREVERSIBLE

    def test_critical_app_lifts_at_least_to_medium(self):
        r = rk.classify_action("select", control_name="Chat", app="Telegram",
                               critical_apps=("telegram",))
        assert r.level == rk.MEDIUM
        assert r.critical_app is True

    def test_non_critical_app_stays_reversible(self):
        r = rk.classify_action("select", control_name="Chat", app="notepad",
                               critical_apps=("telegram",))
        assert r.level == rk.REVERSIBLE
        assert r.critical_app is False


class TestRiskHotkeys:
    def test_enter_in_a_critical_app_is_irreversible(self):
        """The exact harm ADR-0004 describes: text in the wrong chat, then Enter."""
        r = rk.classify_hotkey("enter", app="AyuGram", critical_apps=("ayugram",))
        assert r.level == rk.IRREVERSIBLE
        assert "отправляет" in r.reason

    def test_enter_elsewhere_is_medium(self):
        assert rk.classify_hotkey("enter").level == rk.MEDIUM

    def test_ctrl_c_is_reversible(self):
        assert rk.classify_hotkey("ctrl+c").level == rk.REVERSIBLE

    def test_alt_f4_is_medium(self):
        assert rk.classify_hotkey("alt+f4").level == rk.MEDIUM

    def test_delete_is_irreversible(self):
        assert rk.classify_hotkey("delete").level == rk.IRREVERSIBLE

    def test_modifier_parsing_does_not_split_on_plus_inside_a_name(self):
        r = rk.classify_hotkey("ctrl+shift+d")
        assert r.level == rk.IRREVERSIBLE


class TestRiskClicks:
    def test_a_coordinate_click_is_never_reversible(self):
        """Nothing resolves the target before the press, so nothing can vouch for it."""
        assert rk.classify_click().level == rk.MEDIUM
        assert rk.classify_click().batchable is False

    def test_only_reversible_steps_are_batchable(self):
        assert rk.classify_action("select", control_name="Row").batchable is True
        assert rk.classify_action("invoke", control_name="Send").batchable is False


class TestRiskExplanations:
    def test_every_level_carries_a_reason(self):
        """An escalation the operator cannot explain is one they will disable."""
        for verb in ("invoke", "select", "set_value", "toggle"):
            r = rk.classify_action(verb, control_name="Something")
            assert r.reason.strip(), verb


# ------------------------------------------------------------------ host guard
def table_of(*rows):
    """rows: (pid, ppid, name)."""
    return {pid: (ppid, name) for pid, ppid, name in rows}


class TestHostTree:
    # The chain measured on this machine, plus the shell that launched it.
    REAL = table_of(
        (15640, 38824, "powershell.exe"),
        (38824, 29916, "kilo.exe"),
        (29916, 900, "kilo-desktop.exe"),
        (900, 800, "explorer.exe"),
        (800, 700, "winlogon.exe"),
    )

    def test_ancestors_are_protected_but_the_shell_is_not(self):
        tree = hg.build_host_tree(15640, table=self.REAL)
        assert tree.usable
        assert 15640 in tree.pids and 38824 in tree.pids and 29916 in tree.pids
        assert 900 not in tree.pids, "explorer.exe owns the desktop; claiming it " \
                                     "would refuse ordinary clicks on empty space"
        assert 800 not in tree.pids

    def test_the_walk_stops_at_the_first_infrastructure_process(self):
        tree = hg.build_host_tree(15640, table=self.REAL)
        assert tree.ancestors == [15640, 38824, 29916]

    def test_descendants_of_the_host_are_protected(self):
        """The shells the host spawns are its tool channel; typing into one corrupts
        the channel the agent is talking through."""
        t = table_of(
            (10, 0, "explorer.exe"),
            (20, 10, "kilo-desktop.exe"),
            (30, 20, "kilo.exe"),
            (40, 30, "powershell.exe"),
            (50, 40, "python.exe"),
            (60, 10, "notepad.exe"),
        )
        tree = hg.build_host_tree(30, table=t)
        assert {20, 30, 40, 50} <= tree.pids
        assert 60 not in tree.pids, "a sibling of the host is not part of it"
        assert 10 not in tree.pids

    def test_a_terminal_launched_from_the_desktop_stays_automatable(self):
        """The distinction that keeps the guard from blocking legitimate work."""
        t = table_of(
            (10, 0, "explorer.exe"),
            (20, 10, "kilo-desktop.exe"),
            (30, 20, "kilo.exe"),
            (70, 10, "WindowsTerminal.exe"),
        )
        tree = hg.build_host_tree(30, table=t)
        assert 70 not in tree.pids

    def test_a_dead_parent_ends_the_walk(self):
        """kilo-desktop.exe's own parent had already exited when this was measured,
        so the chain has to end there rather than fail or climb forever."""
        tree = hg.build_host_tree(30, table={30: (999999, "kilo.exe")})
        assert tree.usable
        assert tree.ancestors == [30]

    def test_a_cycle_does_not_hang(self):
        t = {1: (2, "a.exe"), 2: (1, "b.exe")}
        tree = hg.build_host_tree(1, table=t)
        assert tree.usable
        assert set(tree.ancestors) == {1, 2}

    def test_unknown_root_fails_closed(self):
        tree = hg.build_host_tree(424242, table=self.REAL)
        assert tree.error
        assert tree.blocks(1) is True, "an unbuilt guard must not become permissive"

    def test_empty_table_fails_closed(self):
        tree = hg.build_host_tree(1, table={})
        assert tree.error
        assert tree.blocks(1) is True

    @pytest.mark.parametrize("pid", [None, 0])
    def test_an_unresolvable_owner_fails_closed(self, pid):
        tree = hg.build_host_tree(15640, table=self.REAL)
        assert tree.blocks(pid or None) is True

    def test_describe_names_the_process(self):
        tree = hg.build_host_tree(15640, table=self.REAL)
        assert "kilo.exe" in tree.describe(38824)
        assert "не определён" in tree.describe(None)

    def test_describe_reports_why_an_unusable_guard_refused(self):
        tree = hg.build_host_tree(1, table={})
        assert "не построено" in tree.describe(5)

    def test_ancestor_depth_is_bounded(self):
        """A runaway climb on a pathological snapshot costs more than it protects."""
        rows = [(i, i - 1, f"p{i}.exe") for i in range(2, 200)]
        rows.append((1, 0, "root.exe"))
        t = table_of(*rows)
        tree = hg.build_host_tree(199, table=t)
        assert len(tree.ancestors) <= hg.MAX_ANCESTOR_DEPTH + 1
        assert tree.truncated

    def test_pid_of_window_returns_none_for_a_null_handle(self):
        assert hg.pid_of_window(0) is None

    def test_a_failed_snapshot_is_reported_as_a_failure(self, monkeypatch):
        """INVALID_HANDLE_VALUE is a full 64-bit -1.

        ctypes returns c_int when restype is undeclared, so the handle came back
        truncated and the comparison never matched: a failed snapshot was not detected
        as failed, and the guard reported "empty process snapshot" instead of the real
        cause. Same truncation the elevation spike hit in GetCurrentProcess.
        """
        import ctypes

        fake = ctypes.c_void_p(-1).value

        def boom(*_a, **_k):
            return fake

        monkeypatch.setattr(ctypes.windll.kernel32, "CreateToolhelp32Snapshot", boom,
                            raising=False)
        tree = hg.build_host_tree(1)
        assert tree.error, "an invalid handle must not be mistaken for a snapshot"
        assert "CreateToolhelp32Snapshot" in tree.error
        assert tree.blocks(1) is True, "must fail closed"

    def test_real_snapshot_resolves_this_process(self):
        """Ends the unit-test chain with one real Win32 call, so the ctypes struct
        layout is not merely plausible."""
        import os

        tree = hg.build_host_tree(os.getpid())
        if tree.error and "отсутствует" in tree.error:
            pytest.skip("process snapshot did not include this pid")
        assert tree.usable, tree.error
        assert os.getpid() in tree.pids
        assert tree.blocks(os.getpid()) is True


# ------------------------------------------------------------------ audit trail
class TestAuditTrail:
    def test_record_is_one_json_line(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        trail.record(au.TraceRecord(action="invoke", target="1.0.2", ok=True,
                                    verified=True, klass=au.CLASS_B))
        lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["action"] == "invoke"
        assert rec["verified"] is True
        assert rec["seq"] == 1
        assert "ts" in rec

    def test_verified_none_survives_as_null(self):
        """None means 'this action has no readable state', which is a different
        claim from False. Flattening it would erase the distinction the trace
        exists to preserve."""
        rec = json.loads(au.TraceRecord(action="invoke", verified=None).to_json(1))
        assert "verified" in rec
        assert rec["verified"] is None

    def test_verified_false_is_distinguishable_from_none(self):
        a = json.loads(au.TraceRecord(action="x", verified=False).to_json(1))
        b = json.loads(au.TraceRecord(action="x", verified=None).to_json(1))
        assert a["verified"] is False and b["verified"] is None

    def test_empty_optional_fields_are_omitted(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        trail.record(au.TraceRecord(action="get_snapshot"))
        rec = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))
        assert "target" not in rec
        assert "frame_before" not in rec

    def test_cyrillic_is_not_escaped(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        trail.record(au.TraceRecord(action="invoke", message="Отказано: дерево хоста"))
        raw = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
        assert "Отказано" in raw

    def test_sequence_increases_across_records(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        for _ in range(3):
            trail.record(au.TraceRecord(action="x"))
        seqs = [r["seq"] for r in trail.read_back()]
        assert seqs == [1, 2, 3]

    def test_disabled_trail_writes_nothing(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trail = au.AuditTrail(path, enabled=False)
        trail.record(au.TraceRecord(action="x"))
        assert not path.exists()
        assert trail.records == 0

    def test_rotation_bounds_the_file(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trail = au.AuditTrail(path, max_bytes=400, keep=3)
        for i in range(200):
            trail.record(au.TraceRecord(action="invoke", target=f"1.0.{i}",
                                        message="x" * 40))
        assert path.stat().st_size <= 400 + 200
        generations = sorted(p.name for p in tmp_path.glob("t.*.jsonl"))
        assert len(generations) <= 3, generations

    def test_rotation_drops_the_oldest_generation(self, tmp_path):
        path = tmp_path / "t.jsonl"
        trail = au.AuditTrail(path, max_bytes=200, keep=2)
        for _ in range(300):
            trail.record(au.TraceRecord(action="a", message="y" * 60))
        assert not (tmp_path / "t.2.jsonl").exists(), "generations beyond keep must go"

    def test_a_failed_write_does_not_raise(self, tmp_path, monkeypatch):
        """A full or locked disk is a bookkeeping failure. Turning it into a failed
        click would be worse than losing the record."""
        trail = au.AuditTrail(tmp_path / "t.jsonl")

        def boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("builtins.open", boom)
        trail.record(au.TraceRecord(action="invoke"))  # must not raise
        assert trail.write_failures == 1
        assert trail.records == 0

    def test_repeated_failures_are_counted_not_raised(self, tmp_path, monkeypatch):
        trail = au.AuditTrail(tmp_path / "t.jsonl")

        def boom(*_a, **_k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr("builtins.open", boom)
        for _ in range(10):
            trail.record(au.TraceRecord(action="x"))
        assert trail.write_failures == 10

    def test_read_back_on_a_missing_file_is_empty(self, tmp_path):
        assert au.AuditTrail(tmp_path / "nope.jsonl").read_back() == []

    def test_read_back_skips_a_corrupt_line(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"action":"a"}\nNOT JSON\n{"action":"b"}\n', encoding="utf-8")
        got = au.AuditTrail(path).read_back()
        assert [r["action"] for r in got] == ["a", "b"]

    def test_frame_hash_is_stable_and_short(self):
        h = au.frame_hash(b"payload")
        assert h and len(h) == 16
        assert h == au.frame_hash(b"payload")
        assert h != au.frame_hash(b"payloae")

    def test_no_frame_has_no_hash(self):
        assert au.frame_hash(None) is None
        assert au.frame_hash(b"") is None

    def test_extra_fields_reach_the_record(self, tmp_path):
        trail = au.AuditTrail(tmp_path / "t.jsonl")
        trail.record(au.TraceRecord(action="mouse_action",
                                    extra={"requested": [10, 20], "cursor": [10, 20]}))
        rec = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))
        assert rec["requested"] == [10, 20]
