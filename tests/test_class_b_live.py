"""Live Class B verbs against real native Win32 controls.

The deterministic suite proves the dispatch tables, guards and response shapes.
It cannot prove that Windows providers accept those calls: every verb here was
previously covered only by a hand-written fake tree, and the project has already
been bitten by a real API returning a value no fake would produce (IsIconic vs a
hand-written showCmd set).

Each test creates its own top-level window on a dedicated thread with a real
message pump. Nothing touches the session cursor or keyboard. Focus and
activation are gated behind AUTOPILOT_LIVE_FOCUS because those two necessarily
change which window the operator is using.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from uia_client import UIAClient

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
comctl32 = ctypes.WinDLL("comctl32")

u32.DefWindowProcW.restype = ctypes.c_ssize_t
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                               wintypes.LPARAM]
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.SendMessageW.restype = ctypes.c_ssize_t
u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM]
u32.DestroyWindow.argtypes = [wintypes.HWND]
u32.RegisterClassW.argtypes = [ctypes.c_void_p]
u32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
u32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                            wintypes.UINT]
u32.TranslateMessage.argtypes = [ctypes.c_void_p]
u32.DispatchMessageW.argtypes = [ctypes.c_void_p]
u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM]
k32.GetCurrentThreadId.restype = wintypes.DWORD
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_VSCROLL = 0x00200000
WM_CLOSE = 0x0010
WM_QUIT = 0x0012

_buffers = []
_ignore_close: set[int] = set()

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


@WNDPROC
def _wnd_proc(hwnd, msg, wp, lp):
    if msg == WM_CLOSE and int(hwnd) in _ignore_close:
        return 0
    return u32.DefWindowProcW(hwnd, msg, wp, lp)


def _send_int(handle, message, wp, lp):
    return u32.SendMessageW(handle, message, wp, ctypes.c_ssize_t(lp))


def _send_text(handle, message, text):
    buffer = ctypes.create_unicode_buffer(text)
    _buffers.append(buffer)
    return u32.SendMessageW(handle, message, 0,
                            ctypes.cast(buffer, ctypes.c_void_p).value)


class ControlWindow(threading.Thread):
    """Owns native windows and their message pump."""

    _serial = 0

    def __init__(self):
        super().__init__(daemon=True)
        ControlWindow._serial += 1
        self.class_name = f"AutopilotClassB{os.getpid()}_{ControlWindow._serial}"
        self.ready = threading.Event()
        self.thread_id = 0
        self.main = 0
        self.fixed = 0
        self.closable = 0
        self.ignoring = 0
        self.controls: list[tuple[str, int]] = []
        self.error = ""

    def run(self):
        hinst = k32.GetModuleHandleW(None)
        comctl32.InitCommonControls()
        wc = WNDCLASSW()
        wc.lpfnWndProc = _wnd_proc
        wc.hInstance = hinst
        wc.lpszClassName = self.class_name
        if not u32.RegisterClassW(ctypes.byref(wc)):
            self.error = f"RegisterClassW failed: {ctypes.GetLastError()}"
            self.ready.set()
            return
        try:
            self.thread_id = int(k32.GetCurrentThreadId())
            self.main = self._create("Autopilot Class B probe",
                                     WS_OVERLAPPEDWINDOW | WS_VISIBLE, 100, 100, 720, 520)
            self.fixed = self._create("Autopilot fixed-size probe",
                                      WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_VISIBLE,
                                      850, 100, 500, 400, hinst)
            self.closable = self._create("Autopilot closable probe",
                                         WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                         850, 520, 420, 260, hinst)
            self.ignoring = self._create("Autopilot ignoring-close probe",
                                         WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_VISIBLE,
                                         1300, 520, 420, 260, hinst)
            _ignore_close.add(self.ignoring)
            self._build_controls(hinst)
            self.ready.set()
            msg = MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # pragma: no cover - reported through the fixture
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
        finally:
            _ignore_close.discard(self.ignoring)
            for _kind, handle in self.controls:
                u32.DestroyWindow(handle)
            for handle in (self.main, self.fixed, self.closable, self.ignoring):
                if handle:
                    u32.DestroyWindow(handle)
            u32.UnregisterClassW(self.class_name, hinst)

    def _create(self, title, style, x, y, width, height, hinst=None):
        hinst = hinst or k32.GetModuleHandleW(None)
        handle = u32.CreateWindowExW(0, self.class_name, title, style, x, y,
                                     width, height, None, None, hinst, None)
        if not handle:
            raise OSError(f"CreateWindowExW({title}) failed: {ctypes.GetLastError()}")
        return int(handle)

    def _child(self, kind, title, style, x, y, width, height, ident, hinst):
        handle = u32.CreateWindowExW(0, kind, title, WS_CHILD | WS_VISIBLE | style,
                                     x, y, width, height, wintypes.HWND(self.main),
                                     wintypes.HMENU(ident), hinst, None)
        if not handle:
            raise OSError(f"CreateWindowExW({kind}) failed: {ctypes.GetLastError()}")
        self.controls.append((kind, int(handle)))
        return int(handle)

    def _build_controls(self, hinst):
        edit = self._child("Edit", "before", 0x0080, 20, 20, 220, 28, 101, hinst)
        check = self._child("Button", "Probe checkbox", 0x0003, 20, 65, 180, 28, 102, hinst)
        invoke = self._child("Button", "Probe invoke", 0, 220, 65, 140, 28, 103, hinst)
        combo = self._child("ComboBox", "", 0x0003, 20, 110, 220, 120, 104, hinst)
        listbox = self._child("ListBox", "", 0x0001 | WS_VSCROLL, 20, 250, 220, 100,
                              105, hinst)
        track = self._child("msctls_trackbar32", "", 0x0001, 280, 120, 300, 40, 106,
                            hinst)
        for value in ("one", "two", "three"):
            _send_text(combo, 0x0143, value)  # CB_ADDSTRING
        for i in range(30):
            _send_text(listbox, 0x0180, f"item-{i:02d}")  # LB_ADDSTRING
        _send_int(track, 0x0407, 1, 0)    # TBM_SETRANGEMIN
        _send_int(track, 0x0408, 1, 100)  # TBM_SETRANGEMAX
        _send_int(track, 0x0405, 1, 0)    # TBM_SETPOS
        for handle in (edit, check, invoke, combo, listbox, track):
            u32.ShowWindow(wintypes.HWND(handle), 5)
        u32.ShowWindow(wintypes.HWND(self.main), 5)
        u32.UpdateWindow(wintypes.HWND(self.main))
        time.sleep(0.25)

    def stop(self):
        if self.thread_id:
            u32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(timeout=10)


@pytest.fixture
def window():
    thread = ControlWindow()
    thread.start()
    if not thread.ready.wait(10):
        pytest.fail("the native window thread did not start")
    if thread.error:
        pytest.fail(thread.error)
    try:
        yield SimpleNamespace(thread=thread, hwnd=thread.main, client=UIAClient())
    finally:
        thread.stop()


def _snapshot(window, hwnd=None):
    snap = window.client.snapshot(hwnd or window.hwnd, max_depth=8, max_nodes=400,
                                  time_budget_s=5.0)
    assert not snap.truncated, (snap.truncated_reason, snap.warnings)
    return snap


def _by_text(snap, text):
    return next(node for node in snap.nodes if node.text == text)


def test_snapshot_exposes_real_patterns_and_offscreen_state(window):
    snap = _snapshot(window)
    assert {"Value", "Invoke", "Toggle", "SelectionItem", "ExpandCollapse",
            "RangeValue"} <= {pattern for node in snap.nodes for pattern in node.patterns}
    edit = _by_text(snap, "Edit")
    checkbox = _by_text(snap, "Probe checkbox")
    combo = _by_text(snap, "ComboBox")
    slider = _by_text(snap, "msctls_trackbar32")
    item = _by_text(snap, "item-00")
    offscreen = _by_text(snap, "item-29")
    assert edit.role == "Edit" and "Value" in edit.patterns
    assert checkbox.role == "CheckBox" and "Toggle" in checkbox.patterns
    assert combo.role == "ComboBox" and "ExpandCollapse" in combo.patterns
    assert slider.role == "Slider" and "RangeValue" in slider.patterns
    assert item.role == "ListItem" and "SelectionItem" in item.patterns
    assert offscreen.offscreen is True


def test_set_value_is_confirmed_by_read_back(window):
    snap = _snapshot(window)
    edit = _by_text(snap, "Edit")
    result = window.client.set_value(window.hwnd, edit.path, "after")
    assert result.ok and result.verified
    assert result.after == "after"
    assert _by_text(_snapshot(window), "Edit").text == "Edit"


def test_toggle_and_selection_are_confirmed(window):
    snap = _snapshot(window)
    checkbox = _by_text(snap, "Probe checkbox")
    item = _by_text(snap, "item-00")
    toggled = window.client.toggle(window.hwnd, checkbox.path)
    selected = window.client.select_item(window.hwnd, item.path)
    assert toggled.ok and toggled.verified and toggled.after == "On"
    assert selected.ok and selected.verified and selected.after == "True"


def test_expand_and_collapse_are_confirmed(window):
    snap = _snapshot(window)
    combo = _by_text(snap, "ComboBox")
    expanded = window.client.expand_collapse(window.hwnd, combo.path, True)
    collapsed = window.client.expand_collapse(window.hwnd, combo.path, False)
    assert expanded.ok and expanded.verified and expanded.after == "Expanded"
    assert collapsed.ok and collapsed.verified and collapsed.after == "Collapsed"


def test_range_value_is_confirmed_and_quantization_is_reported(window):
    snap = _snapshot(window)
    slider = _by_text(snap, "msctls_trackbar32")
    exact = window.client.set_range_value(window.hwnd, slider.path, 37)
    assert exact.ok and exact.verified
    assert exact.after == "37"

    # A trackbar quantizes to whole pixels. Asking for a fraction inside the range
    # must not be reported as confirmed: the read-back is a different number.
    track = next(handle for kind, handle in window.thread.controls
                 if kind == "msctls_trackbar32")
    _send_int(track, 0x0407, 1, 0)   # TBM_SETRANGEMIN
    _send_int(track, 0x0408, 1, 10)  # TBM_SETRANGEMAX
    _send_int(track, 0x0405, 1, 0)   # TBM_SETPOS
    time.sleep(0.1)
    snapped = window.client.set_range_value(window.hwnd, slider.path, 3.7)
    assert snapped.ok is False
    assert snapped.verified is False
    assert "квантуют" in snapped.message


def test_invoke_reports_that_the_effect_is_not_verifiable(window):
    snap = _snapshot(window)
    invoke = _by_text(snap, "Probe invoke")
    result = window.client.invoke(window.hwnd, invoke.path)
    assert result.ok is True
    assert result.verified is None
    assert "наблюдением" in result.message


def test_scroll_into_view_brings_an_offscreen_item_into_view(window):
    snap = _snapshot(window)
    offscreen = _by_text(snap, "item-29")
    assert offscreen.offscreen is True
    result = window.client.scroll_into_view(window.hwnd, offscreen.path)
    assert result.ok and result.verified
    assert _by_text(_snapshot(window), "item-29").offscreen is False


def test_window_state_move_and_resize_are_confirmed(window):
    client = window.client
    maximized = client.set_window_state(window.hwnd, "maximize")
    restored = client.set_window_state(window.hwnd, "normal")
    moved = client.move_window(window.hwnd, 130, 130)
    resized = client.resize_window(window.hwnd, 700, 500)
    assert maximized.ok and maximized.verified and maximized.after == "Maximized"
    assert restored.ok and restored.verified and restored.after == "Normal"
    assert moved.ok and moved.verified
    assert resized.ok and resized.verified
    assert resized.after == "(130, 130, 700, 500)"


def test_resize_refuses_a_window_that_cannot_resize(window):
    result = window.client.resize_window(window.thread.fixed, 450, 350)
    assert result.ok is False
    assert result.verified is False
    assert "CanResize=0" in result.message


def test_close_reports_still_open_when_the_application_ignores_close(window):
    result = window.client.close_window(window.thread.ignoring)
    assert result.ok is False
    assert result.verified is False
    assert "осталось открытым" in result.message


def test_close_removes_a_cooperative_window(window):
    result = window.client.close_window(window.thread.closable)
    assert result.ok and result.verified
    assert result.after == "closed"


@pytest.mark.skipif(os.environ.get("AUTOPILOT_LIVE_FOCUS") != "1",
                    reason="activation changes the operator's foreground window")
def test_focus_and_activate_are_confirmed(window):
    client = window.client
    snap = _snapshot(window)
    edit = _by_text(snap, "Edit")
    previous = client._foreground_window()
    try:
        focused = client.focus_element(window.hwnd, edit.path)
        activated = client.activate_window(window.hwnd)
        assert focused.ok and focused.verified
        assert activated.ok and activated.verified
    finally:
        if previous and previous != window.hwnd:
            client.activate_window(previous)
