"""Session and workstation state, so the agent is never lied to about the desktop.

When the workstation is locked, screen capture still succeeds -- it returns the
lock screen. Without an explicit signal the agent sees a login prompt and cannot
tell whether the machine locked, the target application closed, or the capture
itself broke. It may then try to interact with a session that is not accepting
input.

Uses the documented WTS session-info query rather than window-class heuristics:
the lock screen's window classes are an implementation detail that has changed
between Windows versions.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

# WTS_INFO_CLASS.WTSSessionInfoEx
_WTS_INFO_EX = 25
# WTS_CONNECTSTATE_CLASS
_WTS_ACTIVE = 0
_WTS_DISCONNECTED = 4

# WTSINFOEX_LEVEL1_W. The WCHAR array lengths are the documented constants plus
# the terminating NUL: WINSTATIONNAME_LENGTH=32, USERNAME_LENGTH=20,
# DOMAIN_LENGTH=17.
_WINSTATIONNAME_LEN = 32
_USERNAME_LEN = 20
_DOMAIN_LEN = 17


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * (_WINSTATIONNAME_LEN + 1)),
        ("UserName", wintypes.WCHAR * (_USERNAME_LEN + 1)),
        ("DomainName", wintypes.WCHAR * (_DOMAIN_LEN + 1)),
    ]


class _WTSINFOEX_LEVEL_DATA(ctypes.Union):
    _fields_ = [
        ("WTSInfoExLevel1", _WTSINFOEX_LEVEL1_W),
        # The real union also carries a level-2 variant whose 64-bit members
        # (FILETIME, ULONGLONG) force 8-byte alignment. That is what places Data
        # at offset 8 rather than 4. Declaring a dummy 64-bit member reproduces
        # the alignment without reproducing fields this module never reads.
        #
        # Getting this wrong is silent: with Data at offset 4 every field lands
        # 4 bytes early, SessionFlags reads a padding zero, and "locked" comes
        # out True for the wrong reason. Confirmed against a hexdump of the real
        # buffer: Level at 0, pad at 4, SessionId at 8, SessionState at 12,
        # SessionFlags at 16, WinStationName at 20.
        ("_align64", ctypes.c_uint64),
    ]


class _WTSINFOEXW(ctypes.Structure):
    _fields_ = [
        ("Level", wintypes.DWORD),
        ("Data", _WTSINFOEX_LEVEL_DATA),
    ]


def session_state() -> dict:
    """Returns what this process can determine about the user session.

    Keys:
      ``locked``     True if the workstation is locked, False if unlocked,
                     None if it cannot be determined on this platform.
      ``state``      the raw WTS connect state name, for diagnostics.
      ``session_id`` the session the query answered for.
      ``reason``     why the value is None, when it is.

    ``SessionFlags`` is a single documented bit: 0 means locked, 1 means
    unlocked. Microsoft notes the value was inverted on Windows 7; this targets
    the Windows 10/11 semantics.
    """
    result = {"locked": None, "state": "unknown", "session_id": None, "reason": ""}

    if sys.platform != "win32":
        result["reason"] = "not Windows"
        return result

    try:
        wtsapi32 = ctypes.WinDLL("wtsapi32")
        kernel32 = ctypes.windll.kernel32

        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,          # hServer, NULL means the local server
            wintypes.DWORD,           # SessionId
            ctypes.c_int,             # WTS_INFO_CLASS
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]

        kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

        session_id = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(
            kernel32.GetCurrentProcessId(), ctypes.byref(session_id)
        ):
            result["reason"] = "ProcessIdToSessionId failed"
            return result

        buf = ctypes.c_void_p()
        nbytes = wintypes.DWORD(0)
        ok = wtsapi32.WTSQuerySessionInformationW(
            None, session_id.value, _WTS_INFO_EX,
            ctypes.byref(buf), ctypes.byref(nbytes),
        )
        if not ok or not buf:
            result["reason"] = f"WTSQuerySessionInformation failed (error {kernel32.GetLastError()})"
            return result

        try:
            if nbytes.value < ctypes.sizeof(_WTSINFOEXW):
                result["reason"] = (
                    f"unexpected buffer size {nbytes.value} "
                    f"< {ctypes.sizeof(_WTSINFOEXW)}"
                )
                return result

            info = ctypes.cast(buf, ctypes.POINTER(_WTSINFOEXW)).contents
            level1 = info.Data.WTSInfoExLevel1

            # Sanity-check before trusting anything. A misaligned structure
            # yields plausible-looking numbers rather than an error, so a wrong
            # layout must be detected here instead of being reported as a
            # confident answer. Both checks are cheap and both fail loudly.
            reported_session = int(level1.SessionId)
            if reported_session != session_id.value:
                result["reason"] = (
                    f"structure mismatch: buffer reports session {reported_session}, "
                    f"queried {session_id.value}"
                )
                return result
            if not level1.WinStationName:
                result["reason"] = "structure mismatch: empty WinStationName"
                return result

            result["session_id"] = reported_session

            states = {
                0: "active", 1: "connected", 2: "connect-query",
                3: "shadow", 4: "disconnected", 5: "idle", 6: "listen",
                7: "reset", 8: "down", 9: "init",
            }
            result["state"] = states.get(level1.SessionState, str(level1.SessionState))

            # Level 1 carries SessionFlags; levels 0 and 2 do not.
            if info.Level == 1:
                result["locked"] = (level1.SessionFlags & 0x1) == 0
            else:
                result["reason"] = f"WTSINFOEX level {info.Level} has no SessionFlags"
            return result
        finally:
            wtsapi32.WTSFreeMemory(buf)

    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        logger.debug("session_state failed: %s", exc)
        return result


def describe(info: dict) -> str:
    """Human-facing one-line summary of an already-fetched state, empty when fine.

    Split out of describe_state because the daemon was paying for the WTS query
    twice per response: once for the structured ``session`` block and once again
    inside describe_state, which re-fetched what the caller already held. The
    query is cheap but it runs on every successful response, including the
    high-frequency ones like get_snapshot.

    A TTL cache was considered and rejected. Staleness would trade directly against
    the guarantee this module exists to provide -- that the agent is never left
    looking at a lock screen without being told -- and there is no lock event to
    invalidate on, so any cache would be a guess about a state whose whole purpose
    is to be authoritative.
    """
    if info["locked"] is True:
        return (
            "СЕССИЯ ЗАБЛОКИРОВАНА. Захват экрана возвращает экран блокировки, "
            "а ввод не принимается рабочим столом приложений. Снимок не описывает "
            "целевую программу. Разблокируйте сессию перед продолжением."
        )
    if info["state"] == "disconnected":
        return "СЕССИЯ ОТКЛЮЧЕНА. Окна не рендерятся, захват и ввод недостоверны."
    return ""


def describe_state() -> str:
    """Fetches the session state and summarises it. See describe for the split."""
    return describe(session_state())
