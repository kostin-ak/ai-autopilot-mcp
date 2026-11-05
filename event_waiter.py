"""Event-based waiting on a dedicated Win32 message pump.

Replaces frame-difference polling for the question "did the UI change?". The daemon
currently stabilises after every action by capturing frames until the perceptual
difference settles: up to 10 captures at ~100 ms, i.e. 100-1000 ms and a screenshot
burned on every iteration.

Measured (spike S7/S8 series, 2026-11-04):

  hook registered, no message pump        0 of 20 callbacks, silently
  hook plus a tight Win32 pump            20 of 20, queue drains in 0.1-0.3 ms
  events fired before pumping starts      0 immediately, 18 arrive when pumping resumes
  dedicated pump, own-window events       median 1.02 ms, max 1.67 ms
  system-wide full range, live desktop    ~3 events/sec

Three design constraints follow directly from that table.

The watcher must own its thread. `WINEVENT_OUTOFCONTEXT` delivers through the message
queue of the thread that registered the hook, so a hook registered on an action's
worker thread delivers nothing at all -- not slowly, never, and without an error. It
cannot be registered on the Qt loop either: that was measured at 320-630 ms, though the
setup was confounded by a blocking cross-thread SendMessage, so the number is unusable
in both directions. A dedicated thread with a tight pump is the one configuration that
was measured clean.

Silent queueing is the dangerous failure, not a missing one. A hook on a thread that
stops pumping does not lose events; it accumulates them and hands them over all at once
when pumping resumes. For a waiting primitive that is worse than having no events,
because a batch of stale changes reads as "the UI just changed". Every wait therefore
takes a `since` tick and discards anything older, and the tick comes from the same
clock the callback reports (`GetTickCount`), so the comparison needs no conversion.

Volume is low enough that filtering can stay cheap. At ~3 events/sec across the whole
desktop, matching on window and time costs less than the machinery to avoid it.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field

logger = logging.getLogger("event_waiter")

# --- event constants (WinUser.h) ----------------------------------------
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_CAPTURESTART = 0x0008
EVENT_SYSTEM_CAPTUREEND = 0x0009
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_FOCUS = 0x8005
EVENT_OBJECT_SELECTION = 0x8006
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_VALUECHANGE = 0x800E
EVENT_OBJECT_TEXTSELECTIONCHANGED = 0x8014

EVENT_MIN = 0x00000001
# WinUser.h defines EVENT_MAX as 0x7FFFFFFF, not 0x7FFF. The difference is the whole
# 0x8000 range, which is where every object-level event lives -- NAMECHANGE,
# VALUECHANGE, CREATE, SHOW. With the truncated value a hook registers successfully,
# reports no error, and silently never delivers the events the waiter was built for.
EVENT_MAX = 0x7FFFFFFF

# Contiguous range covering everything the module actually keeps, computed after
# CONDITIONS below. Far narrower than EVENT_MIN..EVENT_MAX, which keeps the kernel from
# posting events nothing will ever read.
DEFAULT_EVENT_MIN = 0
DEFAULT_EVENT_MAX = 0
WINEVENT_OUTOFCONTEXT = 0x0000
GA_ROOT = 2

# A desktop left running for a day at the measured rate is ~260k events. The bound
# exists so a waiter that never comes back cannot grow the buffer without limit; at
# 20k entries the oldest retained event is still minutes old on a busy desktop, which
# is far longer than any wait.
MAX_BUFFERED_EVENTS = 20000

# Named conditions from PLAN 4.3. Spelled as sets rather than free integers so a
# caller asks for "the window opened" and not for a number it has to look up.
CONDITIONS: dict[str, frozenset[int]] = {
    "opened": frozenset({EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW,
                         EVENT_SYSTEM_FOREGROUND}),
    "closed": frozenset({EVENT_OBJECT_DESTROY, EVENT_OBJECT_HIDE}),
    "focused": frozenset({EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS}),
    "value_changed": frozenset({EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_NAMECHANGE,
                                EVENT_OBJECT_TEXTSELECTIONCHANGED}),
    "tree_changed": frozenset({EVENT_OBJECT_REORDER, EVENT_OBJECT_CREATE,
                               EVENT_OBJECT_DESTROY, EVENT_OBJECT_NAMECHANGE}),
    "appeared": frozenset({EVENT_OBJECT_SHOW, EVENT_OBJECT_CREATE,
                           EVENT_OBJECT_NAMECHANGE}),
}

# Events actually kept. SetWinEventHook takes a contiguous range, so without an
# ingestion filter the buffer fills with LOCATIONCHANGE, which fires on essentially
# every pointer move over a new element and is the noisiest event in the set. Dropping
# unwanted kinds in the callback rather than in the waiter also means a scan never
# wades through them.
INGEST_EVENTS: frozenset[int] = frozenset().union(*CONDITIONS.values()) | {
    EVENT_OBJECT_LOCATIONCHANGE,   # move/resize settling
    EVENT_OBJECT_STATECHANGE,      # a control changing enabled/checked/expanded
    EVENT_OBJECT_SELECTION,        # list and tree selection
    EVENT_SYSTEM_MOVESIZEEND,
}

# Narrower than the full 0x00000001-0x7FFFFFFF, which measured clean but exposes the
# pump to every object event the desktop produces. The narrower range delivers the same
# events this module needs and nothing else.
DEFAULT_EVENT_MIN = min(INGEST_EVENTS)
DEFAULT_EVENT_MAX = max(INGEST_EVENTS)

# Events that mean "the interface changed", for quiescence only.
#
# LOCATIONCHANGE is deliberately excluded, and the exclusion is measured rather than
# aesthetic: it fires on essentially every pointer move over a new element, so a
# quiescence criterion that counts it never sees silence on a live desktop. Sweep of
# quiet windows against a working desktop, 2026-11-05:
#
#   quiet_ms   stabilisation time, all ingested events counted
#         40   0 ms      (a 40 ms gap happened to exist)
#         80   94 ms
#        120   282 ms
#        200   2359 ms   -- effectively the timeout
#
# For comparison the frame-difference path returned in 406 ms on the same screen,
# because a pixel diff only cares about what is *visible*: a caret blinking or a
# pointer crossing widgets does not move it. Counting pointer-tracking events made the
# event path stricter than the thing it was meant to replace, which inverts the entire
# argument for it.
QUIESCENT_EVENTS: frozenset[int] = frozenset({
    EVENT_OBJECT_NAMECHANGE,
    EVENT_OBJECT_VALUECHANGE,
    EVENT_OBJECT_TEXTSELECTIONCHANGED,
    EVENT_OBJECT_CREATE,
    EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_SHOW,
    EVENT_OBJECT_HIDE,
    EVENT_OBJECT_REORDER,
    EVENT_OBJECT_STATECHANGE,
    EVENT_OBJECT_SELECTION,
    EVENT_SYSTEM_FOREGROUND,
})

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
    wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

# Every registered callback, for the lifetime of the process. Discarding on unhook
# would be tidier but is exactly the window in which a late callback faults, so these
# are deliberately never released: a process has one watcher and a handful of tests.
_LIVE_CALLBACKS: list = []   # a list, not a set: CFUNCTYPE instances are unhashable


@dataclass(frozen=True)
class UiEvent:
    """One delivered event.

    ``tick`` is the kernel's own timestamp for when the event happened, not when the
    callback ran. That distinction is the whole basis for discarding stale events: a
    backlog delivered in one burst carries the times it originally occurred at.
    """

    tick: int
    event: int
    hwnd: int
    id_object: int = 0
    id_child: int = 0

    @property
    def name(self) -> str:
        return _EVENT_NAMES.get(self.event, f"0x{self.event:04X}")


_EVENT_NAMES = {
    EVENT_SYSTEM_FOREGROUND: "SYSTEM_FOREGROUND",
    EVENT_OBJECT_CREATE: "OBJECT_CREATE",
    EVENT_OBJECT_DESTROY: "OBJECT_DESTROY",
    EVENT_OBJECT_SHOW: "OBJECT_SHOW",
    EVENT_OBJECT_HIDE: "OBJECT_HIDE",
    EVENT_OBJECT_REORDER: "OBJECT_REORDER",
    EVENT_OBJECT_FOCUS: "OBJECT_FOCUS",
    EVENT_OBJECT_SELECTION: "OBJECT_SELECTION",
    EVENT_OBJECT_STATECHANGE: "OBJECT_STATECHANGE",
    EVENT_OBJECT_LOCATIONCHANGE: "OBJECT_LOCATIONCHANGE",
    EVENT_OBJECT_NAMECHANGE: "OBJECT_NAMECHANGE",
    EVENT_OBJECT_VALUECHANGE: "OBJECT_VALUECHANGE",
    EVENT_OBJECT_TEXTSELECTIONCHANGED: "OBJECT_TEXTSELECTIONCHANGED",
}


@dataclass
class WaitResult:
    """Outcome of one wait.

    ``matched`` and ``timed_out`` are not simply each other's negation: a wait can also
    fail because the watcher never started, which is neither.
    """

    matched: bool
    event: UiEvent | None = None
    elapsed_ms: float = 0.0
    waited_events: int = 0
    discarded_stale: int = 0
    error: str = ""
    quiescent_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.matched and not self.error


@dataclass
class WatcherStats:
    delivered: int = 0
    dropped_overflow: int = 0
    waits: int = 0
    matches: int = 0
    timeouts: int = 0
    started_at: float | None = None
    hook: int = 0
    thread_alive: bool = False
    error: str = ""
    buffer_len: int = 0
    _recent: deque = field(default_factory=lambda: deque(maxlen=8))


def tick_now() -> int:
    """Current GetTickCount value, on the same clock as ``UiEvent.tick``."""
    return int(ctypes.windll.kernel32.GetTickCount())


def ticks_between(a: int, b: int) -> int:
    """b - a in milliseconds, signed, tolerant of the 49.7-day tick wrap.

    The sign matters more than the wrap: a negative result is what identifies an event
    older than the caller's mark, which is the entire stale-backlog filter. An unsigned
    difference turns "500 ms before" into "4294966796 ms after" and silently disables
    it -- every stale event then looks fresh, which is the failure this module exists
    to prevent.
    """
    delta = (b - a) & 0xFFFFFFFF
    return delta - 0x100000000 if delta >= 0x80000000 else delta


_root_cache: dict[int, int] = {}
_root_cache_lock = threading.Lock()


def root_window(hwnd: int) -> int:
    """Top-level ancestor of a window, or the handle itself.

    Events on a control carry the control's handle, not the top-level window's, so a
    wait keyed on a window handle would miss everything inside it without this.

    Cached, and never called while holding the watcher's condition variable. The
    callback needs that lock from the pump thread, so a Win32 call made under it can
    block the pump while the pump is the only thing that can deliver the event being
    waited for -- which presents as an access violation rather than as a deadlock.
    """
    if not hwnd:
        return 0
    with _root_cache_lock:
        cached = _root_cache.get(hwnd)
        if cached is not None:
            return cached
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        root = int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd)
    except (OSError, ValueError):
        root = hwnd
    with _root_cache_lock:
        # Bounded: handles are recycled, so an unbounded cache would grow forever on a
        # machine left running, and a stale entry would misroute a filter.
        if len(_root_cache) > 4096:
            _root_cache.clear()
        _root_cache[hwnd] = root
    return root


class EventWatcher:
    """One dedicated thread that owns a system-wide hook and a message pump.

    A single watcher for the whole process, not one per wait: each hook costs a kernel
    object and each pump a thread, and the measured event volume is low enough that one
    buffer serves every waiter.
    """

    def __init__(self, event_min: int = DEFAULT_EVENT_MIN,
                 event_max: int = DEFAULT_EVENT_MAX,
                 buffer_size: int = MAX_BUFFERED_EVENTS,
                 ingest: frozenset[int] | None = None) -> None:
        self._event_min = event_min
        self._event_max = event_max
        self._ingest = INGEST_EVENTS if ingest is None else frozenset(ingest)
        self._buffer: deque[UiEvent] = deque(maxlen=buffer_size)
        # Never reset, and not affected by eviction. A waiter uses it to tell which
        # buffered entries it has already scanned, so a count of events seen means
        # distinct events rather than rescans of the same ones.
        self._appended = 0
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._hook = 0
        self._stop = threading.Event()
        # Kept as an instance attribute: a collected CFUNCTYPE callback is a crash, not
        # a missed event.
        self._proc: WinEventProc | None = None
        self.stats = WatcherStats()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> str:
        """Starts the pump thread. Idempotent; returns "" on success."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                self.stats.thread_alive = True
                return ""
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ui-event-pump",
                                            daemon=True)
            self._thread.start()
        # The hook is registered inside the thread, so starting is not the same as
        # being ready. Waiting for the hook means the first wait cannot race a
        # registration that has not happened yet and silently see nothing.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._cond:
                if self._hook or self.stats.error:
                    break
            time.sleep(0.005)
        with self._cond:
            self.stats.thread_alive = bool(self._thread and self._thread.is_alive())
            return self.stats.error

    def stop(self) -> None:
        """Unhooks and joins. Safe to call twice."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._hook)

    # --------------------------------------------------------------- pump
    def _run(self) -> None:
        """Registers the hook and pumps messages until stopped.

        Everything Win32-facing happens on this thread, because the hook delivers to
        the thread that registered it.
        """
        try:
            user32 = ctypes.windll.user32
            self._proc = WinEventProc(self._on_event)
            # Held at module level as well as on the instance. A collected CFUNCTYPE
            # leaves Windows holding a pointer to freed memory, and the resulting fault
            # arrives on the pump thread with no Python traceback -- it presents as a
            # random access violation rather than as the reference bug it is.
            _LIVE_CALLBACKS.append(self._proc)
            user32.SetWinEventHook.restype = wintypes.HANDLE
            hook = user32.SetWinEventHook(
                self._event_min, self._event_max, None, self._proc, 0, 0,
                WINEVENT_OUTOFCONTEXT)
            if not hook:
                err = f"SetWinEventHook returned null (GetLastError={ctypes.GetLastError()})"
                with self._cond:
                    self.stats.error = err
                    self._cond.notify_all()
                logger.error(err)
                return
            self._hook = int(hook)
            with self._cond:
                self.stats.hook = self._hook
                self.stats.started_at = time.time()
                self._cond.notify_all()
            logger.info("UI event watcher armed (hook=%s, range=0x%04X-0x%04X)",
                        self._hook, self._event_min, self._event_max)

            msg = wintypes.MSG()
            # PM_REMOVE in a single call. The peek-then-get shape is a documented race:
            # PeekMessage without PM_REMOVE leaves the message in the queue, and a
            # GetMessage that then finds nothing returns -1 or 0 and leaves MSG
            # uninitialised, which DispatchMessage dereferences. It ran clean in the
            # spike and crashed here, which is what a race looks like.
            pm_remove = 0x0001
            wm_quit = 0x0012
            user32.PeekMessageW.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                            wintypes.HWND, wintypes.UINT,
                                            wintypes.UINT, wintypes.UINT]
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            while not self._stop.is_set():
                drained = False
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_remove):
                    if msg.message == wm_quit:
                        self._stop.set()
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                    drained = True
                if not drained:
                    # 2 ms rather than a blocking GetMessage: the stop flag has to be
                    # noticed promptly, and a blocking wait would only return on a
                    # message that may never come.
                    time.sleep(0.002)
        except Exception as exc:  # pragma: no cover - a dead pump must be visible
            err = f"event pump died: {exc!r}"
            with self._cond:
                self.stats.error = err
                self._cond.notify_all()
            logger.exception("UI event watcher failed")
        finally:
            if self._hook:
                # Suppressed rather than raised: this runs in a finally block on the way
                # out, and an unhook failure must not mask whatever put us here.
                with contextlib.suppress(Exception):
                    ctypes.windll.user32.UnhookWinEvent(wintypes.HANDLE(self._hook))
                self._hook = 0
                with self._cond:
                    self.stats.hook = 0

    def _on_event(self, _hook, event, hwnd, id_object, id_child, _thread, tick) -> None:
        """Callback, invoked on the pump thread. Must never raise."""
        try:
            event = int(event)
            if event not in self._ingest:
                return
            rec = UiEvent(tick=int(tick), event=event, hwnd=int(hwnd or 0),
                          id_object=int(id_object or 0), id_child=int(id_child or 0))
            with self._cond:
                if len(self._buffer) == self._buffer.maxlen:
                    self.stats.dropped_overflow += 1
                self._buffer.append(rec)
                self._appended += 1
                self.stats.delivered += 1
                self.stats.buffer_len = len(self._buffer)
                self.stats._recent.append(rec)
                self._cond.notify_all()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------- queries
    def events_since(self, since_tick: int, hwnd: int = 0,
                     events: frozenset[int] | None = None) -> list[UiEvent]:
        """Buffered events at or after a tick, optionally filtered.

        Filters by top-level window when a handle is given, because events on controls
        carry the control's handle.
        """
        root = root_window(hwnd) if hwnd else 0
        with self._cond:
            snapshot = list(self._buffer)
        out = []
        for rec in snapshot:
            if ticks_between(since_tick, rec.tick) < 0:
                continue
            if events is not None and rec.event not in events:
                continue
            if root and rec.hwnd and root_window(rec.hwnd) != root:
                continue
            out.append(rec)
        return out

    def snapshot_stats(self) -> WatcherStats:
        with self._cond:
            self.stats.buffer_len = len(self._buffer)
            self.stats.thread_alive = bool(self._thread and self._thread.is_alive())
            return self.stats

    # --------------------------------------------------------------- waits
    def wait_for_event(self, since_tick: int, hwnd: int = 0,
                       events: frozenset[int] | None = None,
                       timeout_s: float = 30.0) -> WaitResult:
        """Waits until one matching event arrives.

        ``since_tick`` is mandatory rather than defaulted, because the alternative --
        matching anything already in the buffer -- is exactly the stale-backlog failure
        this module exists to avoid. A caller that wants "anything recent" must say so
        by passing a tick it chose.
        """
        started = time.monotonic()
        root = root_window(hwnd) if hwnd else 0
        deadline = started + max(0.0, timeout_s)
        seen = 0
        with self._cond:
            self.stats.waits += 1
            # Counted once, from the backlog that exists when the wait begins. Scanning
            # it on every poll would multiply the count by the number of polls, which
            # is the one number a caller uses to tell "stale backlog" from "nothing
            # happened".
            stale = sum(1 for r in self._buffer
                        if ticks_between(since_tick, r.tick) < 0)
        while True:
            # Snapshotted under the lock, filtered outside it. Filtering calls
            # root_window, which is a Win32 call, and the pump thread's callback needs
            # this same lock to deliver the event being waited for.
            with self._cond:
                snapshot = list(self._buffer)
            for rec in snapshot:
                if ticks_between(since_tick, rec.tick) < 0:
                    continue
                if events is not None and rec.event not in events:
                    continue
                if root and rec.hwnd and root_window(rec.hwnd) != root:
                    continue
                seen += 1
                with self._cond:
                    self.stats.matches += 1
                return WaitResult(True, rec, (time.monotonic() - started) * 1000.0,
                                  seen, stale)
            with self._cond:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stats.timeouts += 1
                    return WaitResult(False, None,
                                      (time.monotonic() - started) * 1000.0, seen,
                                      stale)
                # Bounded wait so a missed notification cannot hang the caller for the
                # whole timeout.
                self._cond.wait(min(remaining, 0.05))

    def wait_for_quiescence(self, since_tick: int, hwnd: int = 0,
                            quiet_ms: float = 250.0,
                            timeout_s: float = 30.0,
                            events: frozenset[int] | None = None) -> WaitResult:
        """Waits until no matching event has arrived for ``quiet_ms``.

        ``events`` defaults to every ingested kind, which on a live desktop includes
        LOCATIONCHANGE -- pointer-tracking noise that never lets the stream go quiet.
        Callers asking "has the interface finished changing" should pass
        QUIESCENT_EVENTS; callers asking "has anything at all stopped happening" can
        leave it out.

        Returns as soon as a quiet window has elapsed. ``quiescent_ms`` reports how long
        the stream was actually quiet, so a caller can tell a UI that settled instantly
        from one that was still churning when the timeout expired.
        """
        started = time.monotonic()
        root = root_window(hwnd) if hwnd else 0
        deadline = started + max(0.0, timeout_s)
        quiet_ms = max(0.0, quiet_ms)
        seen = 0
        with self._cond:
            self.stats.waits += 1
            stale = sum(1 for r in self._buffer
                        if ticks_between(since_tick, r.tick) < 0)
        # Quietness is measured on the kernel's own clock, from the newest matching
        # event's tick to now. Doing it in monotonic time instead would need a
        # conversion per event and would drift; doing it by wall-clock arrival time
        # would let a late-delivered backlog reset the window even though the change
        # itself happened long ago.
        newest_tick = since_tick
        cursor = self._appended  # entries at or before this have been scanned
        while True:
            # Sampled BEFORE the scan, not after. Sampling afterwards would let an event
            # that arrives during the scan go uncounted this pass while "now" has already
            # moved past it, overstating how quiet the stream is. Sampling first makes the
            # error conservative: such an event carries a tick at or after the sample, so
            # it is picked up on the next pass and pushes the quiet window back out. A
            # wait that occasionally takes one extra 20 ms pass is fine; one that reports
            # a settled interface while it is still changing is not.
            now_tick = tick_now()
            # Only entries appended since the last pass. Rescanning the whole buffer
            # would multiply `seen` by the number of polls, and that count is what a
            # caller reads to tell "the UI kept changing" from "it changed once".
            with self._cond:
                skip = max(0, len(self._buffer) - (self._appended - cursor))
                cursor = self._appended
                fresh = list(self._buffer)[skip:]
            # Filtered outside the lock: root_window is a Win32 call and the pump
            # thread needs this lock to deliver anything at all.
            for rec in fresh:
                if ticks_between(since_tick, rec.tick) < 0:
                    continue
                if events is not None and rec.event not in events:
                    continue
                if root and rec.hwnd and root_window(rec.hwnd) != root:
                    continue
                seen += 1
                if ticks_between(newest_tick, rec.tick) > 0:
                    newest_tick = rec.tick
            quiet_for = ticks_between(newest_tick, now_tick)
            if quiet_for >= quiet_ms:
                self.stats.matches += 1
                return WaitResult(True, None, (time.monotonic() - started) * 1000.0,
                                  seen, stale, quiescent_ms=float(quiet_for))
            if time.monotonic() >= deadline:
                self.stats.timeouts += 1
                return WaitResult(
                    False, None, (time.monotonic() - started) * 1000.0, seen, stale,
                    error=(f"интерфейс не успокоился за {timeout_s:.1f} с: "
                           f"последнее изменение {quiet_for} мс назад, нужно "
                           f"{quiet_ms:.0f} мс тишины"),
                    quiescent_ms=float(quiet_for))
            with self._cond:
                self._cond.wait(0.02)


# One watcher for the process. Created lazily so importing this module costs nothing
# and a headless test run never starts a thread.
_watcher: EventWatcher | None = None
_watcher_lock = threading.Lock()


def watcher() -> EventWatcher:
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = EventWatcher()
        return _watcher
