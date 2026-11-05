"""Tests for the ripple dirty-rectangle optimisation.

The ripple timer ticks every ~15 ms. Marking the whole overlay dirty on each tick
repainted the entire virtual desktop -- 4480x1440 on this machine -- and re-ran the neon
border loop, which constructs a fresh QLinearGradient per step, to redraw a border that
had not changed. Repainting only the ripple's neighbourhood removes that.

The correctness argument the tests pin down: the ripple does not move. _ripple_pos is
fixed for the whole animation and only the radius grows from 0 to
TARGET_RIPPLE_MAX_RADIUS, so a square of that radius bounds every frame it can draw.
"""
from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QPoint, QRect

import config
import overlay_ui


@pytest.fixture
def overlay():
    win = overlay_ui.HUDOverlayWindow.__new__(overlay_ui.HUDOverlayWindow)
    win._ripple_pos = None
    win._last_ripple_pos = None
    win._ripple_active = False
    win._ripple_start_time = 0.0
    win._state = config.AutopilotState.IDLE
    win._target_rect = None
    win.updated = []
    win.update = lambda rect=None: win.updated.append(rect)
    # The real one reads self.geometry(), which needs a constructed C++ widget.
    win._global_to_local_point = lambda p: QPoint(p.x(), p.y())
    win._global_to_local_rect = lambda r: QRect(r)
    return win


class FakeTimer:
    def __init__(self):
        self.stopped = 0
        self.started = 0

    def stop(self):
        self.stopped += 1

    def start(self, *_a):
        self.started += 1


def bound():
    return config.TARGET_RIPPLE_MAX_RADIUS + overlay_ui._RIPPLE_DIRTY_MARGIN_PX


class TestDirtyRect:
    def test_no_position_gives_an_empty_rect(self, overlay):
        assert overlay._ripple_dirty_rect() == QRect()

    def test_the_rect_bounds_the_maximum_radius(self, overlay):
        overlay._ripple_pos = QPoint(500, 300)
        r = overlay._ripple_dirty_rect()
        m = bound()
        assert r == QRect(500 - m, 300 - m, m * 2, m * 2)

    def test_the_rect_is_far_smaller_than_the_virtual_desktop(self, overlay):
        """The whole point. A dirty rect the size of the window would be no optimisation
        at all, and would still re-run the border loop."""
        overlay._ripple_pos = QPoint(2000, 1000)
        r = overlay._ripple_dirty_rect()
        desktop = QRect(0, 0, 4480, 1440)
        assert r.width() * r.height() < desktop.width() * desktop.height() / 10

    def test_the_previous_position_is_included(self, overlay):
        """A second ripple starting elsewhere must still clear the region the first one
        painted, or the tail of a faded marker survives on screen."""
        overlay._last_ripple_pos = QPoint(100, 100)
        overlay._ripple_pos = QPoint(900, 700)
        r = overlay._ripple_dirty_rect()
        m = bound()
        assert r.contains(QRect(100 - m, 100 - m, m * 2, m * 2))
        assert r.contains(QRect(900 - m, 700 - m, m * 2, m * 2))

    def test_the_previous_position_is_advanced(self, overlay):
        overlay._ripple_pos = QPoint(300, 300)
        overlay._ripple_dirty_rect()
        assert overlay._last_ripple_pos == QPoint(300, 300)

    def test_local_coordinates_are_used_not_desktop_ones(self, overlay):
        """The overlay's origin is not (0,0) when a secondary monitor sits to the left of
        or above the primary, so a desktop coordinate would offset the dirty region by
        exactly that origin and repaint the wrong part of the window."""
        origin = QPoint(-1920, 0)
        overlay._global_to_local_point = lambda p: QPoint(p.x() - origin.x(),
                                                          p.y() - origin.y())
        overlay._ripple_pos = QPoint(100, 100)
        r = overlay._ripple_dirty_rect()
        m = bound()
        # topLeft, not center: QRect's right/bottom edges are inclusive, so center() of
        # an even-sided rect lands one pixel short of the arithmetic centre and asserting
        # on it tests Qt's convention rather than the conversion.
        assert r.topLeft() == QPoint(100 - origin.x() - m, 100 - origin.y() - m)
        assert r.width() == m * 2


class TestTick:
    def test_an_inactive_ripple_stops_the_timer_and_paints_nothing(self, overlay):
        overlay._ripple_timer = FakeTimer()
        overlay._ripple_active = False
        overlay._on_ripple_tick()
        assert overlay._ripple_timer.stopped == 1
        assert overlay.updated == []

    def test_a_tick_repaints_only_the_dirty_region(self, overlay):
        overlay._ripple_timer = FakeTimer()
        overlay._ripple_active = True
        overlay._ripple_pos = QPoint(400, 400)
        overlay._ripple_start_time = time.monotonic()
        overlay._on_ripple_tick()
        assert len(overlay.updated) == 1
        rect = overlay.updated[0]
        assert isinstance(rect, QRect) and not rect.isEmpty()
        assert rect.width() < 4480, "a full-window repaint would defeat the purpose"

    def test_the_final_tick_still_clears_the_marker(self, overlay):
        """When the animation ends, paintEvent no longer draws the ripple, so the dirty
        region must still be repainted once -- otherwise the last frame stays on screen
        permanently."""
        overlay._ripple_timer = FakeTimer()
        overlay._ripple_active = True
        overlay._ripple_pos = QPoint(400, 400)
        overlay._ripple_start_time = 0.0  # far in the past
        overlay._on_ripple_tick()
        assert overlay._ripple_active is False
        assert overlay._ripple_timer.stopped == 1
        assert len(overlay.updated) == 1, "the clearing repaint must still happen"
        assert isinstance(overlay.updated[0], QRect)
        assert not overlay.updated[0].isEmpty()


class TestFullRepaintsArePreserved:
    """The optimisation applies only to the hot path. Anything that changes the border
    must still repaint the whole window, or a colour change would be visible only inside
    the last ripple's neighbourhood."""

    def test_a_state_change_repaints_everything(self, overlay):
        overlay.set_state("USER_OVERRIDE")
        assert overlay.updated == [None], "None means the whole widget"

    def test_a_target_change_repaints_everything(self, overlay):
        overlay._on_set_target_rect(0, 0, 800, 600)
        assert overlay.updated == [None]

    def test_starting_a_ripple_repaints_everything(self, overlay):
        overlay._ripple_timer = FakeTimer()
        overlay.show_target(120, 240)
        assert overlay.updated == [None]
        assert overlay._ripple_timer.started == 1
        assert overlay._ripple_active is True
