"""Tests for process DPI awareness and independent thread environment setup.

Awareness can be set once per process and the first call wins, so the order of attempts
is the whole behaviour. These tests drive fakes rather than the real API: calling it for
real would lock the test process into whatever mode ran first and make every later case
in the same session untestable.
"""
from __future__ import annotations

import ctypes

import pytest

import config


class FakeUser32:
    def __init__(self, context_ok=True, context_raises=False, system_ok=True):
        self.calls = []
        outer = self

        def SetProcessDpiAwarenessContext(handle):
            outer.calls.append("context")
            if context_raises:
                raise OSError("not available on this build")
            return context_ok

        def SetProcessDPIAware():
            outer.calls.append("system")
            return system_ok

        self.SetProcessDpiAwarenessContext = SetProcessDpiAwarenessContext
        self.SetProcessDPIAware = SetProcessDPIAware


class FakeShcore:
    def __init__(self, v1_hr=0):
        self.calls = []
        self.v1_hr = v1_hr
        outer = self

        def SetProcessDpiAwareness(value):
            outer.calls.append("awareness")
            return outer.v1_hr

        self.SetProcessDpiAwareness = SetProcessDpiAwareness


@pytest.fixture
def reset_mode():
    """The module caches its answer, and importing the daemon in another test sets it."""
    saved = config.DPI_MODE
    config.DPI_MODE = "unset"
    yield
    config.DPI_MODE = saved


def install(monkeypatch, user32, shcore):
    monkeypatch.setattr(ctypes.windll, "user32", user32, raising=False)
    monkeypatch.setattr(ctypes.windll, "shcore", shcore, raising=False)


class TestOrdering:
    def test_v2_is_tried_first_and_v1_is_never_reached(self, monkeypatch, reset_mode):
        """The defect this exists to prevent: calling the V1 API first locks the process
        at V1 forever, and the later V2 call is refused with ACCESS_DENIED."""
        u = FakeUser32(context_ok=True)
        s = FakeShcore()
        install(monkeypatch, u, s)
        assert config.set_process_dpi_awareness() == "per-monitor-v2"
        assert u.calls == ["context"]
        assert s.calls == [], "V1 must not be attempted once V2 succeeded"

    def test_the_result_is_cached(self, monkeypatch, reset_mode):
        u = FakeUser32()
        s = FakeShcore()
        install(monkeypatch, u, s)
        first = config.set_process_dpi_awareness()
        second = config.set_process_dpi_awareness()
        assert first == second == "per-monitor-v2"
        assert u.calls == ["context"], "a second call must not touch Win32 again"


class TestFallbacks:
    def test_a_refused_v2_falls_back_to_v1(self, monkeypatch, reset_mode):
        u = FakeUser32(context_ok=False)
        s = FakeShcore(v1_hr=0)
        install(monkeypatch, u, s)
        assert config.set_process_dpi_awareness() == "per-monitor-v1"
        assert u.calls == ["context"] and s.calls == ["awareness"]

    def test_a_missing_v2_api_falls_back_to_v1(self, monkeypatch, reset_mode):
        """An absent function raises AttributeError inside the suppressed block, which
        must degrade rather than propagate -- the daemon has to start on Windows 8.1."""
        u = FakeUser32(context_raises=True)
        s = FakeShcore(v1_hr=0)
        install(monkeypatch, u, s)
        assert config.set_process_dpi_awareness() == "per-monitor-v1"

    def test_v1_failure_falls_back_to_system_aware(self, monkeypatch, reset_mode):
        u = FakeUser32(context_ok=False, system_ok=True)
        s = FakeShcore(v1_hr=0x80070005)
        install(monkeypatch, u, s)
        assert config.set_process_dpi_awareness() == "system"
        assert u.calls == ["context", "system"]

    def test_everything_failing_is_reported_not_silenced(self, monkeypatch, reset_mode,
                                                        caplog):
        """Unaware processes get bitmap-scaled screenshots, which is what makes OCR
        unreliable. That must be loud: every coordinate derived from such a frame is
        suspect, and a silent fallback would leave the failure invisible."""
        u = FakeUser32(context_ok=False, system_ok=False)
        s = FakeShcore(v1_hr=0x80070005)
        install(monkeypatch, u, s)
        with caplog.at_level("ERROR", logger="config"):
            assert config.set_process_dpi_awareness() == "unavailable"
        assert any("bitmap-scaled" in r.message for r in caplog.records)

    def test_the_v1_fallback_warns_about_what_is_lost(self, monkeypatch, reset_mode,
                                                      caplog):
        """Not just "fell back" -- what the fallback costs, so the difference between V1
        and V2 is visible to whoever reads the log rather than being folklore."""
        u = FakeUser32(context_ok=False)
        s = FakeShcore(v1_hr=0)
        install(monkeypatch, u, s)
        with caplog.at_level("WARNING", logger="config"):
            config.set_process_dpi_awareness()
        assert any("V1" in r.message for r in caplog.records)


class TestReporting:
    def test_the_mode_is_readable_by_the_rest_of_the_process(self, monkeypatch,
                                                             reset_mode):
        u = FakeUser32()
        s = FakeShcore()
        install(monkeypatch, u, s)
        config.set_process_dpi_awareness()
        assert config.DPI_MODE == "per-monitor-v2"

    def test_unset_is_distinguishable_from_every_real_outcome(self, reset_mode):
        assert config.DPI_MODE == "unset"
        assert "unset" not in {"per-monitor-v2", "per-monitor-v1", "system",
                               "unavailable"}


class TestThreadEnvironment:
    """DPI context and desktop attachment are independent thread obligations."""

    class User32:
        def __init__(self, desktop="default"):
            self.desktop = desktop
            self.calls = []

        def SetThreadDpiAwarenessContext(self, handle):
            self.calls.append(("dpi", int(handle.value)))

        def GetThreadDesktop(self, _thread_id):
            self.calls.append(("get_desktop",))
            return 41

        def GetUserObjectInformationW(self, handle, _index, buf, _size, _out):
            assert handle == 41
            buf.value = self.desktop
            self.calls.append(("read_desktop",))
            return True

        def OpenDesktopW(self, name, _flags, _inherit, _access):
            self.calls.append(("open_desktop", name))
            return 42

        def SetThreadDesktop(self, handle):
            self.calls.append(("set_desktop", handle))
            return True

    class Kernel32:
        @staticmethod
        def GetCurrentThreadId():
            return 7

    def install_thread_fakes(self, monkeypatch, user32):
        monkeypatch.setattr(ctypes.windll, "user32", user32, raising=False)
        monkeypatch.setattr(ctypes.windll, "kernel32", self.Kernel32(), raising=False)

    def test_dpi_context_does_not_touch_the_desktop(self, monkeypatch):
        user32 = self.User32()
        self.install_thread_fakes(monkeypatch, user32)
        config.ensure_thread_dpi_context()
        assert user32.calls[0][0] == "dpi"
        assert user32.calls[0][1] & 0xFFFFFFFF == 0xFFFFFFFC

    def test_interactive_desktop_does_not_touch_dpi(self, monkeypatch):
        user32 = self.User32(desktop="default")
        self.install_thread_fakes(monkeypatch, user32)
        config.ensure_thread_interactive_desktop()
        assert user32.calls == [("get_desktop",), ("read_desktop",)]

    def test_a_non_interactive_thread_is_attached_to_default(self, monkeypatch):
        user32 = self.User32(desktop="autopilot-hidden")
        self.install_thread_fakes(monkeypatch, user32)
        config.ensure_thread_interactive_desktop()
        assert user32.calls == [
            ("get_desktop",), ("read_desktop",),
            ("open_desktop", "default"), ("set_desktop", 42),
        ]

    def test_a_failed_desktop_open_does_not_install_an_invalid_handle(self, monkeypatch):
        user32 = self.User32(desktop="autopilot-hidden")
        user32.OpenDesktopW = lambda *_args: 0
        self.install_thread_fakes(monkeypatch, user32)
        config.ensure_thread_interactive_desktop()
        assert not any(call[0] == "set_desktop" for call in user32.calls)
