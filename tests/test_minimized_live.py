"""Live test: a minimized window must be recognised and refused, not activated.

Written after a measured defect. `_is_minimized` parsed WINDOWPLACEMENT.showCmd and tested
it against (6, 7) -- SW_MINIMIZE and SW_SHOWMINNOACTIVE -- while a normally minimized window
reports SW_SHOWMINIMIZED = 2. So the check was False for a window that was an icon,
activate_window proceeded, and the caller was told the window had become foreground while it
was still minimized.

A unit test over a fake cannot catch that: the defect is precisely that the real API returns
a value the hand-written set does not contain. So this test creates a real window, minimizes
it, and asks the real client.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import pytest

from uia_client import UIAClient

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
SW_MINIMIZE = 6


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


@WNDPROC
def _wnd_proc(hwnd, msg, wp, lp):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


@pytest.fixture
def window():
    hinst = kernel32.GetModuleHandleW(None)
    cls = f"AutopilotMinTest{int(time.time() * 1000) % 1000000}"
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = hinst
    wc.lpszClassName = cls
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, cls, "autopilot minimized test",
                                  WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  300, 300, 400, 240, None, None, hinst, None)
    if not hwnd:
        pytest.skip(f"could not create a window: {ctypes.GetLastError()}")
    try:
        yield int(hwnd)
    finally:
        user32.DestroyWindow(wintypes.HWND(hwnd))
        user32.UnregisterClassW(cls, wintypes.HINSTANCE(hinst))


class TestMinimizedIsRecognised:
    def test_a_visible_window_is_not_minimized(self, window):
        assert UIAClient()._is_minimized(window) is False

    def test_a_minimized_window_is_minimized(self, window):
        """The assertion the measured defect failed. SW_MINIMIZE produces showCmd = 2,
        which the previous enum set did not contain, so a real icon read as not
        minimized."""
        user32.ShowWindow(wintypes.HWND(window), SW_MINIMIZE)
        time.sleep(0.35)
        assert bool(user32.IsIconic(wintypes.HWND(window))) is True, (
            "the test could not actually minimize the window")
        assert UIAClient()._is_minimized(window) is True

    def test_activation_is_refused_with_a_pointer_to_restore(self, window):
        """Reporting success here told the caller to move on to an element that was still an
        icon -- the same shape of false success this project has removed four times."""
        user32.ShowWindow(wintypes.HWND(window), SW_MINIMIZE)
        time.sleep(0.35)
        result = UIAClient().activate_window(window)
        assert result.ok is False
        assert result.verified is False
        assert "restore" in result.message

    def test_restoring_makes_it_activatable_again(self, window):
        """The refusal must be about the state, not about the window being unusable."""
        client = UIAClient()
        user32.ShowWindow(wintypes.HWND(window), SW_MINIMIZE)
        time.sleep(0.35)
        assert client._is_minimized(window) is True
        user32.ShowWindow(wintypes.HWND(window), 9)   # SW_RESTORE
        time.sleep(0.35)
        assert client._is_minimized(window) is False

    def test_a_dead_handle_is_not_minimized(self):
        assert UIAClient()._is_minimized(0) is False
        assert UIAClient()._is_minimized(1) is False
