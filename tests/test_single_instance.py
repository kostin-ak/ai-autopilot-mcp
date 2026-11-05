"""Tests for the single-instance guard.

The same-process cases check the mechanism; the cross-process case checks the property
that actually matters, because a named mutex is per-session-and-name and the double
launch it prevents comes from a second process, not a second object.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid

import config
import single_instance as si


def unique_name():
    # A fresh name per test: a leaked handle from an earlier test would make the next
    # one report "already running" and pass for the wrong reason.
    return r"Local\autopilot_test_" + uuid.uuid4().hex[:12]


class TestMechanism:
    def test_a_fresh_name_is_acquired(self):
        g = si.InstanceGuard(unique_name())
        try:
            assert g.acquire() is True
            assert g.held is True
            assert g.already_running is False
            assert g.error == ""
        finally:
            g.release()

    def test_a_second_acquisition_of_the_same_name_is_refused(self):
        name = unique_name()
        first = si.InstanceGuard(name)
        second = si.InstanceGuard(name)
        try:
            assert first.acquire() is True
            assert second.acquire() is False
            assert second.already_running is True
            assert second.held is False, "a refused guard must not claim to hold the lock"
        finally:
            first.release()
            second.release()

    def test_releasing_lets_a_later_guard_in(self):
        """This is the crash-recovery property: the lock does not outlive its holder."""
        name = unique_name()
        first = si.InstanceGuard(name)
        assert first.acquire() is True
        first.release()
        assert first.held is False
        second = si.InstanceGuard(name)
        try:
            assert second.acquire() is True
        finally:
            second.release()

    def test_release_is_idempotent(self):
        g = si.InstanceGuard(unique_name())
        g.acquire()
        g.release()
        g.release()
        assert g.held is False

    def test_the_context_manager_releases(self):
        name = unique_name()
        with si.InstanceGuard(name) as g:
            assert g.held
        after = si.InstanceGuard(name)
        try:
            assert after.acquire() is True
        finally:
            after.release()

    def test_the_refused_guard_reports_no_error_of_its_own(self):
        """already_running and error are different facts. Conflating them would make a
        normal double launch look like a failure of the guard itself."""
        name = unique_name()
        first = si.InstanceGuard(name)
        second = si.InstanceGuard(name)
        try:
            first.acquire()
            assert second.acquire() is False
            assert second.error == ""
            assert second.already_running is True
        finally:
            first.release()
            second.release()


class TestAcrossProcesses:
    HOLDER = (
        "import sys, time;"
        "sys.path.insert(0, sys.argv[1]);"
        "import single_instance as si;"
        "g = si.InstanceGuard(sys.argv[2]);"
        "ok = g.acquire();"
        "print('HELD' if ok else 'REFUSED', flush=True);"
        "time.sleep(6)"
    )

    def test_a_second_process_is_refused_while_the_first_holds_it(self):
        name = unique_name()
        repo = str(config.TOKEN_FILE_PATH.parent)
        holder = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, repo, name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # Wait for the holder to actually have the mutex rather than sleeping a
            # fixed time and hoping; a race here would flake under load.
            deadline = time.monotonic() + 8.0
            line = ""
            while time.monotonic() < deadline:
                g = si.InstanceGuard(name)
                if g.acquire():
                    g.release()  # holder had not got there yet
                    time.sleep(0.05)
                    continue
                line = "REFUSED"
                break
            assert line == "REFUSED", (
                "a second process acquired the mutex while the first held it; "
                f"holder stdout/stderr: {holder.stdout.read()!r} "
                f"{holder.stderr.read()!r}")
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_the_lock_is_gone_once_the_holding_process_dies(self):
        """The property a lock file cannot provide. The daemon installs system-wide input
        hooks, so "it crashed and left a lock behind" is a real scenario, and a stale
        lock would block every subsequent start until someone deleted a file by hand."""
        name = unique_name()
        repo = str(config.TOKEN_FILE_PATH.parent)
        holder = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, repo, name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Confirm it is held, then kill it hard rather than letting it exit cleanly.
        deadline = time.monotonic() + 8.0
        held = False
        while time.monotonic() < deadline and not held:
            g = si.InstanceGuard(name)
            held = not g.acquire()
            g.release()
            if not held:
                time.sleep(0.05)
        assert held, "the holder never took the mutex"

        holder.kill()
        holder.wait(timeout=5)

        deadline = time.monotonic() + 5.0
        acquired = False
        while time.monotonic() < deadline and not acquired:
            g = si.InstanceGuard(name)
            acquired = g.acquire()
            if not acquired:
                g.release()
                time.sleep(0.05)
        assert acquired, "the mutex survived the death of the process that held it"
        g.release()


class TestWiring:
    def test_the_configured_name_is_session_scoped(self):
        """Global\\ would refuse a second user in their own session, and two users in
        separate sessions are not two instances fighting over one cursor."""
        assert config.INSTANCE_MUTEX_NAME.startswith("Local\\")

    def test_the_exit_code_is_distinct_from_a_crash(self):
        assert config.EXIT_ALREADY_RUNNING not in (0, 1)

    def test_main_acquires_before_building_any_gui(self):
        """Checking after QApplication would mean two processes briefly both owned a HUD
        frame and a set of low-level input hooks -- the exact state the guard prevents."""
        import inspect

        import autopilot_daemon as daemon

        src = inspect.getsource(daemon.main)
        assert "InstanceGuard" in src
        # Matched on the call, not the bare name: the comment above the guard also says
        # "QApplication", and searching for the word alone found the prose first and
        # inverted the ordering this test exists to check.
        assert src.index("InstanceGuard(") < src.index("QApplication(sys.argv)")
        assert "EXIT_ALREADY_RUNNING" in src
