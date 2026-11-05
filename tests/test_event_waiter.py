"""Tests for event-based waiting.

The wait logic is tested against a buffer fed directly, which is what makes the
interesting cases checkable at all: stale backlogs, tick wraparound, and a stream that
never goes quiet. The thread and hook are exercised once, live, because the assumption
that a dedicated thread can register a system-wide hook is the one thing the spike
established and the implementation depends on.
"""
from __future__ import annotations

import threading
import time

import pytest

import event_waiter as ew


def make_watcher():
    """A watcher with a pump thread that is deliberately never started."""
    return ew.EventWatcher()


def push(w, tick, event, hwnd=0):
    """Feeds the watcher through its real callback rather than into the buffer.

    Going through `_on_event` is what makes the overflow accounting and the
    notification path part of what these tests cover; writing to the deque directly
    would test the wait logic against an ingestion path nothing exercises.
    """
    w._on_event(None, event, hwnd, 0, 0, 0, tick)


# ----------------------------------------------------------------- pure helpers
class TestTickArithmetic:
    def test_simple_difference(self):
        assert ew.ticks_between(1000, 1500) == 500

    def test_negative_when_event_precedes_the_mark(self):
        """This is what makes the stale filter work: a negative span means older."""
        assert ew.ticks_between(1500, 1000) == -500

    def test_wrap_is_tolerated(self):
        """GetTickCount wraps at 49.7 days. Untreated, a wait straddling the wrap would
        discard every new event as stale and time out."""
        before = 0xFFFFFFFF - 100
        after = 50
        assert ew.ticks_between(before, after) == 151

    def test_same_tick_is_zero_not_stale(self):
        assert ew.ticks_between(500, 500) == 0


class TestEventNames:
    def test_known_events_are_named(self):
        assert ew.UiEvent(0, ew.EVENT_OBJECT_VALUECHANGE, 0).name == "OBJECT_VALUECHANGE"

    def test_unknown_events_still_render(self):
        assert ew.UiEvent(0, 0x1234, 0).name == "0x1234"


class TestConditions:
    @pytest.mark.parametrize("name,events", sorted(ew.CONDITIONS.items()))
    def test_every_condition_is_non_empty_and_known(self, name, events):
        assert events, f"condition {name!r} matches nothing"
        assert all(isinstance(e, int) for e in events)

    def test_the_named_conditions_cover_what_the_plan_asked_for(self):
        """PLAN 4.3 lists: window opened, focus changed, value changed, tree rebuilt,
        notification appeared. Each must have a name a caller can ask for."""
        for needed in ("opened", "focused", "value_changed", "tree_changed",
                       "appeared"):
            assert needed in ew.CONDITIONS, needed


class TestWaitResult:
    def test_ok_requires_a_match_and_no_error(self):
        assert ew.WaitResult(True, None).ok is True
        assert ew.WaitResult(True, None, error="x").ok is False
        assert ew.WaitResult(False, None).ok is False

    def test_matched_and_timed_out_are_not_simply_opposites(self):
        """A wait can fail because the watcher never started, which is neither."""
        failed = ew.WaitResult(False, None, error="watcher not running")
        assert failed.matched is False and failed.error


# ------------------------------------------------------------- wait_for_event
class TestWaitForEvent:
    def test_a_matching_event_already_buffered_returns_at_once(self):
        w = make_watcher()
        push(w, 1000, ew.EVENT_OBJECT_VALUECHANGE)
        t0 = time.monotonic()
        r = w.wait_for_event(since_tick=900, events=frozenset(
            {ew.EVENT_OBJECT_VALUECHANGE}), timeout_s=2.0)
        assert r.ok and r.event is not None
        assert (time.monotonic() - t0) < 0.2

    def test_events_older_than_the_mark_are_discarded_and_counted(self):
        """The stale-backlog case. A hook on a thread that stopped pumping delivers its
        whole backlog at once, and reading that as 'the UI just changed' is the failure
        this module exists to prevent."""
        w = make_watcher()
        push(w, 100, ew.EVENT_OBJECT_VALUECHANGE)
        push(w, 200, ew.EVENT_OBJECT_VALUECHANGE)
        r = w.wait_for_event(since_tick=1000,
                             events=frozenset({ew.EVENT_OBJECT_VALUECHANGE}),
                             timeout_s=0.3)
        assert r.matched is False
        assert r.discarded_stale == 2
        assert r.waited_events == 0

    def test_an_unmatched_event_kind_times_out(self):
        w = make_watcher()
        push(w, 1000, ew.EVENT_OBJECT_NAMECHANGE)
        r = w.wait_for_event(since_tick=900,
                             events=frozenset({ew.EVENT_OBJECT_VALUECHANGE}),
                             timeout_s=0.2)
        assert r.matched is False and r.elapsed_ms >= 150

    def test_an_event_arriving_during_the_wait_is_caught(self):
        w = make_watcher()

        def feed():
            time.sleep(0.15)
            push(w, ew.tick_now(), ew.EVENT_OBJECT_SHOW)

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        r = w.wait_for_event(since_tick=ew.tick_now() - 50,
                             events=frozenset({ew.EVENT_OBJECT_SHOW}), timeout_s=3.0)
        t.join(timeout=2.0)
        assert r.ok, r.error
        assert r.event.event == ew.EVENT_OBJECT_SHOW
        assert 100 <= r.elapsed_ms < 2000

    def test_no_filter_means_any_event_matches(self):
        w = make_watcher()
        push(w, 1000, ew.EVENT_OBJECT_REORDER)
        assert w.wait_for_event(since_tick=900, timeout_s=0.5).ok

    def test_stats_count_waits_matches_and_timeouts(self):
        w = make_watcher()
        push(w, 1000, ew.EVENT_OBJECT_SHOW)
        w.wait_for_event(since_tick=900, timeout_s=0.2)
        w.wait_for_event(since_tick=900,
                         events=frozenset({ew.EVENT_OBJECT_HIDE}), timeout_s=0.1)
        s = w.snapshot_stats()
        assert s.waits == 2 and s.matches == 1 and s.timeouts == 1


# --------------------------------------------------------- wait_for_quiescence
class TestWaitForQuiescence:
    def test_a_quiet_stream_returns_after_the_quiet_window(self):
        w = make_watcher()
        t0 = time.monotonic()
        r = w.wait_for_quiescence(since_tick=ew.tick_now(), quiet_ms=120, timeout_s=2.0)
        assert r.ok
        assert (time.monotonic() - t0) < 1.0
        assert r.quiescent_ms is not None and r.quiescent_ms >= 100

    def test_a_stream_that_never_quiets_times_out_with_the_reason(self):
        """The timeout message has to say how long ago the last change was, because
        'it did not settle' alone does not tell the caller whether to retry or to give
        up."""
        w = make_watcher()
        stop = threading.Event()

        def churn():
            while not stop.is_set():
                push(w, ew.tick_now(), ew.EVENT_OBJECT_LOCATIONCHANGE)
                time.sleep(0.02)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            r = w.wait_for_quiescence(since_tick=ew.tick_now() - 100, quiet_ms=300,
                                      timeout_s=0.5)
        finally:
            stop.set()
            t.join(timeout=2.0)
        assert r.matched is False
        assert "не успокоился" in r.error
        assert r.waited_events > 0
        assert r.quiescent_ms is not None and r.quiescent_ms < 300

    def test_it_settles_once_the_stream_stops(self):
        w = make_watcher()
        stop = threading.Event()

        def churn():
            end = time.monotonic() + 0.25
            while not stop.is_set() and time.monotonic() < end:
                push(w, ew.tick_now(), ew.EVENT_OBJECT_NAMECHANGE)
                time.sleep(0.02)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        r = w.wait_for_quiescence(since_tick=ew.tick_now() - 100, quiet_ms=120,
                                  timeout_s=3.0)
        stop.set()
        t.join(timeout=2.0)
        assert r.ok, r.error
        assert r.waited_events > 0

    def test_stale_events_do_not_hold_the_window_open(self):
        """A backlog must not look like an unsettled UI."""
        w = make_watcher()
        for i in range(50):
            push(w, 1000 + i, ew.EVENT_OBJECT_LOCATIONCHANGE)
        r = w.wait_for_quiescence(since_tick=ew.tick_now(), quiet_ms=100, timeout_s=1.0)
        assert r.ok
        assert r.discarded_stale == 50


# ------------------------------------------------------------------- buffering
class TestBuffering:
    def test_the_buffer_is_bounded(self):
        w = ew.EventWatcher(buffer_size=10)
        for i in range(40):
            push(w, 1000 + i, ew.EVENT_OBJECT_NAMECHANGE)
        assert len(w._buffer) == 10
        assert w.stats.dropped_overflow == 30

    def test_the_newest_events_are_the_ones_kept(self):
        w = ew.EventWatcher(buffer_size=5)
        for i in range(20):
            push(w, i, ew.EVENT_OBJECT_NAMECHANGE)
        assert [e.tick for e in w._buffer] == [15, 16, 17, 18, 19]

    def test_events_since_filters_by_tick(self):
        w = make_watcher()
        for i in range(5):
            push(w, 100 * (i + 1), ew.EVENT_OBJECT_NAMECHANGE)
        got = w.events_since(since_tick=300)
        assert [e.tick for e in got] == [300, 400, 500]

    def test_events_since_filters_by_kind(self):
        w = make_watcher()
        push(w, 100, ew.EVENT_OBJECT_SHOW)
        push(w, 200, ew.EVENT_OBJECT_HIDE)
        got = w.events_since(0, events=frozenset({ew.EVENT_OBJECT_HIDE}))
        assert [e.event for e in got] == [ew.EVENT_OBJECT_HIDE]


# ---------------------------------------------------------------- live thread
class TestQuiescentEventSet:
    def test_location_change_is_excluded(self):
        """Pointer-tracking noise, not a change to the interface.

        Counting it made the event path stricter than the frame diff it replaces:
        measured at 2359 ms for quiet_ms=200 against 406 ms for frame differencing on
        the same screen, because a live desktop never stops moving the pointer.
        """
        assert ew.EVENT_OBJECT_LOCATIONCHANGE not in ew.QUIESCENT_EVENTS
        assert ew.EVENT_SYSTEM_MOVESIZEEND not in ew.QUIESCENT_EVENTS

    def test_every_quiescent_event_is_actually_ingested(self):
        """A filter naming an event the hook never buffers would match nothing, and
        quiescence would then return instantly on every call -- a silent no-op that
        looks exactly like a very settled interface."""
        missing = ew.QUIESCENT_EVENTS - ew.INGEST_EVENTS
        assert not missing, f"not ingested: {sorted(missing)}"

    def test_the_meaningful_change_events_are_present(self):
        for needed in (ew.EVENT_OBJECT_NAMECHANGE, ew.EVENT_OBJECT_VALUECHANGE,
                       ew.EVENT_OBJECT_SHOW, ew.EVENT_OBJECT_CREATE):
            assert needed in ew.QUIESCENT_EVENTS, needed

    def test_the_filter_makes_a_noisy_stream_look_quiet(self):
        """LOCATIONCHANGE churn alone must not hold the quiet window open.

        The anchor is "now", not a tick in the past: anchoring behind by more than
        quiet_ms satisfies the window at entry and the assertion passes without waiting
        for anything, which is what the first version of this test did.
        """
        w = make_watcher()
        stop = threading.Event()

        def churn():
            while not stop.is_set():
                push(w, ew.tick_now(), ew.EVENT_OBJECT_LOCATIONCHANGE)
                time.sleep(0.02)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            r = w.wait_for_quiescence(since_tick=ew.tick_now(), quiet_ms=100,
                                      timeout_s=2.0, events=ew.QUIESCENT_EVENTS)
        finally:
            stop.set()
            t.join(timeout=2.0)
        assert r.ok, r.error
        assert r.waited_events == 0
        assert r.elapsed_ms >= 90, f"returned before the quiet window: {r.elapsed_ms}"

    def test_without_the_filter_the_same_stream_is_never_quiet(self):
        w = make_watcher()
        stop = threading.Event()

        def churn():
            while not stop.is_set():
                push(w, ew.tick_now(), ew.EVENT_OBJECT_LOCATIONCHANGE)
                time.sleep(0.02)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            r = w.wait_for_quiescence(since_tick=ew.tick_now(), quiet_ms=100,
                                      timeout_s=0.4)
        finally:
            stop.set()
            t.join(timeout=2.0)
        assert r.matched is False
        assert r.waited_events > 0


class TestWatcherThread:
    def test_a_dedicated_thread_can_register_a_system_wide_hook(self):
        """The one assumption the whole design rests on, checked against the real API.

        The spike established it for a hand-rolled pump; this checks the shipped class:
        that starting it registers a hook, that `running` becomes true, and that
        stopping unhooks without leaving a thread behind.
        """
        w = ew.EventWatcher()
        err = w.start()
        try:
            assert err == "", err
            assert w.running, w.snapshot_stats()
            assert w.stats.hook != 0
            assert w.stats.started_at is not None
        finally:
            w.stop()
        assert not w.running
        assert w.stats.hook == 0

    def test_start_is_idempotent(self):
        w = ew.EventWatcher()
        try:
            assert w.start() == ""
            first = w.stats.hook
            assert w.start() == ""
            assert w.stats.hook == first, "a second start must not register a second hook"
        finally:
            w.stop()

    def test_stop_is_safe_to_call_twice(self):
        w = ew.EventWatcher()
        w.start()
        w.stop()
        w.stop()
        assert not w.running

    def test_events_from_the_live_desktop_are_delivered(self):
        """Confirms the shipped class against the real API, end to end.

        Deliberately does not create or destroy a window. An earlier version did, and
        reliably produced an access violation in the test process *after* the test
        passed: creating and destroying a window on the main thread while a system-wide
        hook is live on another faults during teardown. A suite whose interpreter dies
        on the way out has an unreliable exit status, which is worse than a narrower
        assertion. The desktop produces enough events on its own -- measured at 10-23 in
        1.5 s -- to prove registration, pumping, the callback, buffering and the wait.

        Skipped rather than failed when the workstation is locked: a locked session
        produces no desktop UI events, and that is a condition of the machine rather
        than a defect here.
        """
        import session_state

        if session_state.session_state()["locked"]:
            pytest.skip("workstation locked; the desktop delivers no UI events")

        w = ew.EventWatcher()
        err = w.start()
        assert err == "", err
        try:
            since = ew.tick_now() - 100
            r = w.wait_for_event(since_tick=since, timeout_s=5.0)
            assert r.ok, (r.error, w.snapshot_stats())
            assert r.event is not None
            assert w.stats.delivered > 0
            # Every delivered event must be one the ingestion filter keeps, otherwise
            # the buffer fills with kinds no waiter ever asks for.
            assert r.event.event in ew.INGEST_EVENTS
        finally:
            w.stop()

    def test_the_hook_range_covers_every_named_condition(self):
        """The range is contiguous because that is all SetWinEventHook takes, so a
        condition outside it would silently never fire -- which is exactly how the
        original EVENT_MAX = 0x7FFF bug hid."""
        w = ew.EventWatcher()
        for name, events in ew.CONDITIONS.items():
            outside = [e for e in events
                       if not (w._event_min <= e <= w._event_max)]
            assert not outside, f"{name} has events outside the hook range: {outside}"
