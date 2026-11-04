"""Protection of the host application's own process tree from agent input.

ADR-0004 requires that input into the host's own windows be refused without an
explicit override, and until now the daemon could not honour it: it knew which
process owned every window on the desktop and had no idea which process owned
*it*. The failure mode is not exotic. An agent that mis-resolves a target by one
element can land on the chat input of the very application driving it, and typing
there does not fail visibly -- it becomes a message in the conversation, which the
agent then reads back as user input.

Measured host chain on this machine (2026-11-03):

    kilo-desktop.exe (29916) -> kilo.exe (38824) -> powershell.exe (15640)

The PIDs are session-specific and were already stale in PLAN.md when they were
read back, which is the reason the tree is resolved at handshake time rather than
carried as a list. The walk also has to tolerate a dead parent: kilo-desktop.exe's
own parent had already exited, so the chain ended there rather than continuing.

What is protected, and why not simply "the whole tree"
------------------------------------------------------
The obvious rule -- protect the reporting process and everything under it -- is
wrong in both directions here.

Too narrow: the process that reports is the MCP gateway, but the windows that
render the conversation belong to its *ancestors* (kilo-desktop.exe). Protecting
only the gateway and its children would leave the actual chat window writable.

Too broad: protecting every descendant of every ancestor would also cover a
terminal the agent deliberately launched for automation, which is a legitimate
target.

The rule adopted is: the reporting process, its ancestors up to the first piece of
OS infrastructure, and every descendant of each of those. That covers the host
application, the shells the host spawns for its own tool channel -- typing into
those corrupts the channel the agent is talking through -- and nothing else. A
terminal launched from the desktop is a child of the shell, not of the host, and
stays automatable.

Infrastructure processes terminate the ancestor walk and are themselves excluded.
Stopping there is what keeps the guard from claiming explorer.exe, which owns the
desktop and the taskbar: blocking input to it would refuse ordinary clicks on empty
desktop space, and the host is not the shell.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

logger = logging.getLogger("host_guard")

# Processes that terminate the ancestor walk. The host is a descendant of the
# shell, not the other way round, so walking past the shell would claim the
# desktop itself.
INFRASTRUCTURE: frozenset[str] = frozenset({
    "explorer.exe", "winlogon.exe", "services.exe", "csrss.exe", "smss.exe",
    "svchost.exe", "dwm.exe", "wininit.exe", "lsass.exe", "system",
    "system idle process", "registry", "memory compression",
})

# A runaway guard is worse than no guard, so the walk is bounded. Real host chains
# are three or four deep; 32 leaves room for a launcher stack without permitting
# an unbounded climb if a snapshot reports a cycle.
MAX_ANCESTOR_DEPTH = 32
MAX_DESCENDANT_PIDS = 512

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


@dataclass
class HostTree:
    """The protected set, plus enough context to explain a refusal.

    ``error`` is set rather than raising when the tree cannot be built. A guard
    that cannot answer must fail closed -- see ``blocks`` -- but the reason has to
    travel with the refusal, because "refused, unknown reason" is indistinguishable
    from a bug in the guard.
    """

    root_pid: int = 0
    pids: frozenset[int] = field(default_factory=frozenset)
    names: dict[int, str] = field(default_factory=dict)
    ancestors: list[int] = field(default_factory=list)
    truncated: bool = False
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error and bool(self.pids)

    def blocks(self, pid: int | None) -> bool:
        """Whether input to ``pid`` must be refused. Fails closed."""
        if pid is None:
            return True
        if self.error:
            return True
        if not self.pids:
            return True
        return pid in self.pids

    def describe(self, pid: int | None) -> str:
        if self.error:
            return f"дерево процессов хоста не построено ({self.error})"
        if pid is None:
            return "процесс-владелец окна не определён"
        return self.names.get(pid, f"pid {pid}")


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, image name), from one Toolhelp snapshot.

    One snapshot rather than a per-process query: the table is read once per
    handshake, and enumerating parents individually would be dozens of calls to
    learn the same thing.
    """
    table: dict[int, tuple[int, str]] = {}
    if sys.platform != "win32":
        return table
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or snap is None:
        raise OSError(f"CreateToolhelp32Snapshot failed: {ctypes.GetLastError()}")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        first = kernel32.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        first.restype = wintypes.BOOL
        nxt = kernel32.Process32NextW
        nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        nxt.restype = wintypes.BOOL
        if not first(snap, ctypes.byref(entry)):
            return table
        while True:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID), entry.szExeFile or "")
            if not nxt(snap, ctypes.byref(entry)):
                break
        return table
    finally:
        kernel32.CloseHandle(snap)


def _children_of(table: dict[int, tuple[int, str]], pid: int) -> list[int]:
    return [p for p, (pp, _n) in table.items() if pp == pid]


def build_host_tree(root_pid: int | None = None,
                    table: dict[int, tuple[int, str]] | None = None) -> HostTree:
    """Resolves the protected set for ``root_pid`` (default: this process).

    Never raises. A caller that cannot build the tree gets one with ``error`` set,
    and ``HostTree.blocks`` then refuses everything -- an unusable guard must not
    silently become a permissive one.
    """
    if root_pid is None:
        root_pid = os.getpid()
    tree = HostTree(root_pid=root_pid)
    try:
        table = process_table() if table is None else table
    except OSError as exc:
        tree.error = str(exc)
        logger.warning("host tree unavailable, failing closed: %s", exc)
        return tree
    if not table:
        tree.error = "пустой снимок процессов"
        return tree
    if root_pid not in table:
        # The reporting process is already gone, or the PID is invented. Either way
        # there is no chain to protect and no basis for allowing anything.
        tree.error = f"pid {root_pid} отсутствует в снимке процессов"
        return tree

    names = {p: n for p, (_pp, n) in table.items()}
    tree.names = names

    # Ancestors, stopping before the first piece of infrastructure. The stopping
    # process itself is excluded: it is the shell, not the host.
    chain: list[int] = [root_pid]
    seen = {root_pid}
    cur = root_pid
    for _ in range(MAX_ANCESTOR_DEPTH):
        parent = table.get(cur, (0, ""))[0]
        if not parent or parent in seen:
            break
        if parent not in table:
            # The parent has already exited, which is exactly what was measured on
            # this machine: kilo-desktop.exe's launcher was gone by the time the
            # chain was read. A process that does not exist owns no window, so
            # protecting its PID would only put a stale number in the set -- and a
            # recycled PID would then refuse input to an unrelated live process.
            break
        pname = (names.get(parent) or "").lower()
        if pname in INFRASTRUCTURE:
            break
        chain.append(parent)
        seen.add(parent)
        cur = parent
    else:
        tree.truncated = True
    tree.ancestors = chain

    protected = set(chain)
    # Every descendant of every protected process. Bounded, because a host that
    # spawns hundreds of helpers should not turn this into a long walk on the
    # handshake path.
    frontier = list(chain)
    while frontier and len(protected) < MAX_DESCENDANT_PIDS:
        nxt = []
        for pid in frontier:
            for child in _children_of(table, pid):
                if child not in protected:
                    protected.add(child)
                    nxt.append(child)
        frontier = nxt
        if len(protected) >= MAX_DESCENDANT_PIDS:
            tree.truncated = True
    tree.pids = frozenset(protected)
    return tree


def pid_of_window(hwnd: int) -> int | None:
    """Owning process of a window, or None when it cannot be determined."""
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value) or None
    except (OSError, ValueError):
        return None


def pid_at_point(x: int, y: int) -> int | None:
    """Owning process of whatever is at a physical screen coordinate.

    Class A actions carry no window handle -- they carry a point -- so the guard
    has to resolve the point to a window first. Without this, coordinate input
    would bypass the protection entirely, which is the path an off-by-one
    resolution actually takes.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None
        return pid_of_window(int(hwnd))
    except (OSError, ValueError):
        return None
