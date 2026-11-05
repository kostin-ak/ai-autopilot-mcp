"""Single-instance guard.

Without it a second daemon either fails to bind port 8765 and dies with a confusing
socket error, or -- if the first one had released the port -- both install low-level
input hooks and both draw a HUD frame. Two hook sets means every physical input event is
counted twice by two independent override detectors, and two frames means the operator
cannot tell which process is claiming the screen.

The primitive is a named mutex rather than a lock file, for one reason that matters:
the kernel closes it when the process dies, including when the process is killed or
crashes. A lock file survives the crash and then blocks every subsequent start until
someone finds and deletes it, which turns a defensive check into a support burden. The
daemon installs system-wide input hooks, so "it died and left a lock behind" is not a
hypothetical.

Mutual exclusion here is advisory in the usual Windows sense -- nothing prevents a
modified copy from ignoring the mutex -- but it does prevent the accidental double
launch, which is the actual failure mode.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger("single_instance")

ERROR_ALREADY_EXISTS = 183
MUTEX_ALL_ACCESS = 0x001F0001


class InstanceGuard:
    """Holds a named mutex for the lifetime of the process.

    Usage:

        guard = InstanceGuard("Local\\\\MyApp")
        if not guard.acquire():
            return 1        # another instance holds it

    The handle is deliberately never released while the process lives. Windows closes it
    on exit, which is what makes a crash indistinguishable from a clean shutdown as far
    as the next start is concerned.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: wintypes.HANDLE | None = None
        self.already_running = False
        self.error = ""

    def acquire(self) -> bool:
        """True if this process now holds the lock, False if another one already did."""
        kernel32 = ctypes.windll.kernel32
        # Declared rather than assumed. Without restype ctypes returns c_int and
        # truncates the handle, which still works by luck because Windows keeps handle
        # values 32-bit significant -- but CloseHandle would then be handed a value the
        # API never returned. This project has been bitten by that truncation twice
        # already, both times on a pseudo-handle.
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            # Failing open is the right call: an inability to check is not evidence that
            # another instance exists, and refusing to start would make the daemon
            # unusable because of a problem it does not have.
            err = ctypes.GetLastError()
            self.error = f"CreateMutexW failed ({err}); starting without the guard"
            logger.warning(self.error)
            return True

        self._handle = wintypes.HANDLE(handle)
        if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
            self.already_running = True
            kernel32.CloseHandle(self._handle)
            self._handle = None
            return False
        return True

    @property
    def held(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        """Releases early. Normal exit does not need this."""
        if self._handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception as exc:  # pragma: no cover
                logger.debug("CloseHandle on the instance mutex failed: %s", exc)
            self._handle = None

    def __enter__(self) -> InstanceGuard:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
