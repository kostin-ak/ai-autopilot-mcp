"""Which virtual desktop a window lives on.

Why this is worth a module: a window on another virtual desktop looks like a window that
is present and broken. `IsWindowVisible` is true, the handle is valid, the rectangle is
where it should be -- and a capture of that monitor does not show it, and clicks aimed at
its coordinates land on whatever occupies that spot on the *current* desktop. Without this
reading the symptom is indistinguishable from a rendering fault or a bad coordinate, and
an agent will spend its attempts on the wrong hypothesis.

Only the documented part of the interface is used. `IVirtualDesktopManager` exposes three
methods and none of them switches desktops -- switching lives in an undocumented interface
that changes shape between Windows builds. So this reports and moves; it does not switch,
and it must not claim to.

Everything fails soft. The object can be missing (older Windows), COM can be unavailable on
the calling thread, or a handle can be stale. A caller that cannot learn the desktop gets
None and says nothing about it, because "unknown" is the honest answer and a wrong boolean
would be worse than an absent one.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import sys

logger = logging.getLogger("virtual_desktop")

CLSID_VIRTUAL_DESKTOP_MANAGER = "{AA509086-5CA9-4C25-8F95-589D3C07B48A}"
IID_IVIRTUAL_DESKTOP_MANAGER = "{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"

_manager = None
_unavailable = False


def _build_interface():
    """Defines the three documented methods through comtypes.

    Imported inside the function so a machine without comtypes costs nothing at import
    time and so the module can be imported on a platform where COM does not exist.
    """
    from ctypes import POINTER, c_int, c_void_p

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IVirtualDesktopManager(IUnknown):
        _iid_ = GUID(IID_IVIRTUAL_DESKTOP_MANAGER)
        _methods_ = [
            COMMETHOD([], HRESULT, "IsWindowOnCurrentVirtualDesktop",
                      (["in"], c_void_p, "topLevelWindow"),
                      (["out"], POINTER(c_int), "onCurrentDesktop")),
            COMMETHOD([], HRESULT, "GetWindowDesktopId",
                      (["in"], c_void_p, "topLevelWindow"),
                      (["out"], POINTER(GUID), "desktopId")),
            COMMETHOD([], HRESULT, "MoveWindowToDesktop",
                      (["in"], c_void_p, "topLevelWindow"),
                      (["in"], POINTER(GUID), "desktopId")),
        ]

    return IVirtualDesktopManager


def manager():
    """The COM manager, created once, or None when it cannot be created."""
    global _manager, _unavailable
    if _manager is not None or _unavailable:
        return _manager
    if sys.platform != "win32":
        _unavailable = True
        return None
    try:
        import comtypes
        from comtypes.client import CreateObject

        # Explicit rather than automatic: comtypes initialises COM per thread on first
        # use, and the daemon calls this from a worker thread pool. Being explicit makes
        # the apartment choice visible instead of incidental.
        comtypes.CoInitialize()
        iface = _build_interface()
        _manager = CreateObject(CLSID_VIRTUAL_DESKTOP_MANAGER, interface=iface)
        logger.info("virtual desktop manager available")
    except Exception as exc:
        # Not an error worth warning about: this is optional context, and a machine where
        # it is unavailable is a machine where every caller simply gets None.
        logger.debug("virtual desktop manager unavailable: %s", exc)
        _unavailable = True
        _manager = None
    return _manager


def _is_window(hwnd: int) -> bool:
    """Whether the handle still names a window.

    Needed because the COM method answers TRUE for handles that are not windows at all --
    measured with handle 1. It reports "not on another desktop", which for a stale handle
    is a confident answer to a question nobody asked.
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # pragma: no cover
        return False


def is_on_current_desktop(hwnd: int) -> bool | None:
    """Whether the window is on the desktop the user is looking at. None if unknown.

    The out-parameter does not appear in the Python signature: comtypes strips ``["out"]``
    arguments and returns them as the result, so passing ``byref`` here raises
    "call takes exactly 2 arguments (3 given)" rather than anything about types.
    """
    if not hwnd:
        return None
    if not _is_window(hwnd):
        # Measured: IsWindowOnCurrentVirtualDesktop answers TRUE for handle 1, which is not
        # a window at all. The method reports "not on another desktop" rather than "no such
        # window", so without this guard a stale handle yields a confident True -- the same
        # shape of defect as a mouse button falling back to a left click.
        return None
    m = manager()
    if m is None:
        return None
    try:
        value = m.IsWindowOnCurrentVirtualDesktop(ctypes.c_void_p(int(hwnd)))
        if isinstance(value, tuple):
            value = value[0]
        value = int(value)
        if value not in (0, 1):
            # The method documents a BOOL out-parameter. Anything else means the call did
            # not do what it appeared to, and a guess from a nonsense value would be worse
            # than admitting the reading is unavailable.
            return None
        return bool(value)
    except Exception as exc:
        logger.debug("IsWindowOnCurrentVirtualDesktop(%s) failed: %s", hwnd, exc)
        return None


def desktop_id(hwnd: int) -> str | None:
    """Identifier of the window's virtual desktop, or None if unknown.

    Returned as a string rather than a GUID object so it can travel through JSON and be
    compared by a caller without knowing anything about COM.
    """
    if not hwnd:
        return None
    if not _is_window(hwnd):
        return None
    m = manager()
    if m is None:
        return None
    try:
        value = m.GetWindowDesktopId(ctypes.c_void_p(int(hwnd)))
        if isinstance(value, tuple):
            value = value[0]
        return str(value) if value else None
    except Exception as exc:
        logger.debug("GetWindowDesktopId(%s) failed: %s", hwnd, exc)
        return None


def annotate(windows: list) -> list:
    """Adds desktop information to window records, in place, and returns them.

    Annotated rather than filtered: a caller needs to know the window exists *and* that it
    is elsewhere, because the second fact is what explains the symptom. Dropping it from
    the list would leave the agent believing the window is gone.

    Both fields are omitted when unknown rather than set to None or 0, so an absent field
    reads as "not determined" instead of "determined to be false".
    """
    for w in windows:
        try:
            handle = w.get("handle") if isinstance(w, dict) else getattr(
                w, "handle", None)
        except Exception:  # pragma: no cover
            handle = None
        if not handle:
            continue
        on_current = is_on_current_desktop(handle)
        if on_current is None:
            continue
        if isinstance(w, dict):
            w["on_current_desktop"] = on_current
            if not on_current:
                wid = desktop_id(handle)
                if wid:
                    w["desktop_id"] = wid
        else:
            with contextlib.suppress(Exception):
                w.on_current_desktop = on_current
    return windows
