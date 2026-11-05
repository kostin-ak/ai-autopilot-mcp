"""Tests for display-topology tracking in the HUD overlay.

The overlay spans the whole virtual desktop, so its geometry is computed once from the
primary screen's virtual geometry. Without re-computing it on a topology change, plugging
in a monitor or changing a resolution leaves the frame covering the wrong part of the
desktop -- and because the overlay carries WDA_EXCLUDEFROMCAPTURE, a mis-placed frame never
shows up in a screenshot. get_hud_state's placement_correct flag is the only evidence.

These tests avoid constructing the widget: it needs a QApplication and a real display, and
neither is what is under test. The tracking bookkeeping is exercised on a bare instance.
"""
from __future__ import annotations

import pytest

import overlay_ui


class FakeSignal:
    def __init__(self):
        self.connections = 0

    def connect(self, _slot):
        self.connections += 1


class FakeScreen:
    """Stands in for QScreen. Not a Qt wrapper, which is itself part of what is tested:
    the pruning logic must tolerate objects sip knows nothing about."""

    def __init__(self, name="screen"):
        self.name = name
        self.geometryChanged = FakeSignal()

    def __repr__(self):
        return f"<FakeScreen {self.name}>"


class DeadScreen:
    """A wrapper whose C++ object Qt has deleted."""

    def __init__(self, name="dead"):
        self.name = name
        self.geometryChanged = FakeSignal()


@pytest.fixture
def overlay():
    win = overlay_ui.HUDOverlayWindow.__new__(overlay_ui.HUDOverlayWindow)
    win._app_signals_connected = False
    win._tracked_screens = []
    win.geometry_calls = 0
    win.update_calls = 0
    win._update_geometry = lambda: setattr(win, "geometry_calls", win.geometry_calls + 1)
    win.update = lambda: setattr(win, "update_calls", win.update_calls + 1)
    return win


@pytest.fixture
def fake_screens(monkeypatch):
    """Replaces QGuiApplication's screen list and app instance."""
    screens = [FakeScreen("primary")]

    class FakeApp:
        screenAdded = FakeSignal()
        screenRemoved = FakeSignal()

    app = FakeApp()
    monkeypatch.setattr(overlay_ui.QGuiApplication, "instance",
                        staticmethod(lambda: app), raising=False)
    monkeypatch.setattr(overlay_ui.QGuiApplication, "screens",
                        staticmethod(lambda: screens), raising=False)
    return {"screens": screens, "app": app}


class TestPruning:
    def test_dead_screens_are_dropped(self, overlay, monkeypatch):
        dead = DeadScreen()
        alive = FakeScreen("alive")
        overlay._tracked_screens = [dead, alive]
        monkeypatch.setattr(overlay_ui, "_sip",
                            type("S", (), {"isdeleted": staticmethod(
                                lambda o: isinstance(o, DeadScreen))}))
        overlay._prune_dead_screens()
        assert overlay._tracked_screens == [alive]

    def test_live_screens_survive(self, overlay, monkeypatch):
        a, b = FakeScreen("a"), FakeScreen("b")
        overlay._tracked_screens = [a, b]
        monkeypatch.setattr(overlay_ui, "_sip",
                            type("S", (), {"isdeleted": staticmethod(lambda o: False)}))
        overlay._prune_dead_screens()
        assert overlay._tracked_screens == [a, b]

    def test_non_qt_objects_are_kept_rather_than_dropped(self, overlay, monkeypatch):
        """A test stand-in is not a Qt wrapper, so sip raises or is absent. Treating an
        unknown object as alive is the safe direction: keeping a live screen tracked
        matters more than pruning a dead one, and a stale entry costs only a redundant
        signal."""
        obj = FakeScreen("x")
        overlay._tracked_screens = [obj]
        monkeypatch.setattr(overlay_ui, "_sip", None)
        overlay._prune_dead_screens()
        assert overlay._tracked_screens == [obj]

    def test_pruning_survives_an_unexpected_sip_error(self, overlay, monkeypatch):
        def boom(_o):
            raise RuntimeError("Internal C++ object already deleted")

        obj = FakeScreen("x")
        overlay._tracked_screens = [obj]
        monkeypatch.setattr(overlay_ui, "_sip",
                            type("S", (), {"isdeleted": staticmethod(boom)}))
        overlay._prune_dead_screens()
        assert overlay._tracked_screens == [obj]

    def test_a_dead_entry_does_not_break_the_next_membership_test(self, overlay,
                                                                  monkeypatch,
                                                                  fake_screens):
        """The crash this prevents: after an unplug, the next topology change compared a
        live screen against a list holding a deleted wrapper, and the comparison itself
        raised. It failed on the second change rather than the first, which is what made
        it look intermittent."""
        dead = DeadScreen()
        overlay._tracked_screens = [dead]
        monkeypatch.setattr(overlay_ui, "_sip",
                            type("S", (), {"isdeleted": staticmethod(
                                lambda o: isinstance(o, DeadScreen))}))
        overlay._connect_screen_signals()
        assert dead not in overlay._tracked_screens
        assert len(overlay._tracked_screens) == 1
        assert isinstance(overlay._tracked_screens[0], FakeScreen)


class TestSignalWiring:
    def test_every_screen_is_tracked_and_connected_once(self, overlay, fake_screens):
        fake_screens["screens"].append(FakeScreen("secondary"))
        overlay._connect_screen_signals()
        assert len(overlay._tracked_screens) == 2
        assert all(s.geometryChanged.connections == 1 for s in overlay._tracked_screens)

    def test_repeated_calls_do_not_duplicate_connections(self, overlay, fake_screens):
        """Idempotence matters because the handler calls this on every topology change,
        and a screen connected twice fires the re-span twice per event."""
        overlay._connect_screen_signals()
        overlay._connect_screen_signals()
        overlay._connect_screen_signals()
        assert len(overlay._tracked_screens) == 1
        assert overlay._tracked_screens[0].geometryChanged.connections == 1

    def test_app_signals_are_connected_once(self, overlay, fake_screens):
        overlay._connect_screen_signals()
        overlay._connect_screen_signals()
        assert overlay._app_signals_connected is True
        assert fake_screens["app"].screenAdded.connections == 1
        assert fake_screens["app"].screenRemoved.connections == 1

    def test_a_newly_added_screen_gets_tracked_on_the_next_pass(self, overlay,
                                                                fake_screens):
        overlay._connect_screen_signals()
        assert len(overlay._tracked_screens) == 1
        added = FakeScreen("third")
        fake_screens["screens"].append(added)
        overlay._connect_screen_signals()
        assert added in overlay._tracked_screens
        assert added.geometryChanged.connections == 1

    def test_it_tolerates_no_application_instance(self, overlay, monkeypatch,
                                                  fake_screens):
        """The overlay is constructed before the event loop is guaranteed to exist."""
        monkeypatch.setattr(overlay_ui.QGuiApplication, "instance",
                            staticmethod(lambda: None), raising=False)
        overlay._connect_screen_signals()
        assert overlay._app_signals_connected is False
        assert len(overlay._tracked_screens) == 1, "screens are still tracked"


class TestHandler:
    def test_a_topology_change_re_spans_and_repaints(self, overlay, fake_screens):
        overlay._on_screen_topology_changed()
        assert overlay.geometry_calls == 1
        assert overlay.update_calls == 1

    def test_the_handler_accepts_the_qscreen_argument(self, overlay, fake_screens):
        """screenAdded and screenRemoved pass a QScreen; geometryChanged passes nothing.
        One slot serves all three, so it must swallow either shape."""
        overlay._on_screen_topology_changed(FakeScreen("arg"))
        overlay._on_screen_topology_changed()
        assert overlay.geometry_calls == 2

    def test_the_handler_tracks_screens_added_since_construction(self, overlay,
                                                                 fake_screens):
        new = FakeScreen("hotplug")
        fake_screens["screens"].append(new)
        overlay._on_screen_topology_changed(new)
        assert new in overlay._tracked_screens
