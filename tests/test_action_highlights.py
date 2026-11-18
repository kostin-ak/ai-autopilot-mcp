"""Tests for Class B action highlight flashes over the live UI.

The overlay is excluded from every capture (WDA_EXCLUDEFROMCAPTURE), so a highlight
can only be verified by rendering it offscreen and by observing it live. These tests
pin the drawing, the bounded flash list, the expiry repaint, and the daemon's rect
plumbing. Live confirmation is recorded in docs/HANDOFF.md separately.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

import config
import overlay_ui
from autopilot_daemon import AutopilotDaemon
from uia_client import UIAClient


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    a = QApplication([])
    yield a


class FakeTimer:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def overlay(app):
    win = overlay_ui.HUDOverlayWindow.__new__(overlay_ui.HUDOverlayWindow)
    win._flashes = []
    win._flash_total = 0
    win._flash_timer = FakeTimer()
    win.updates = []
    win.update = win.updates.append
    win.rect = lambda: QRect(0, 0, 1200, 800)
    win._global_to_local_rect = lambda r: QRect(r)
    win._global_to_local_point = lambda p: p
    return win


class TestFlashLifecycle:
    def test_a_flash_is_registered_and_repaints(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        overlay.flash_action(100, 100, 300, 120, "reversible", "invoke")
        assert len(overlay._flashes) == 1
        assert overlay._flashes[0]["rect"] == QRect(100, 100, 300, 120)
        assert overlay._flashes[0]["level"] == "reversible"
        assert overlay._flash_timer.started == 1
        assert overlay.updates, "the new flash must schedule a repaint"

    def test_disabled_highlights_do_nothing(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", False)
        overlay.flash_action(100, 100, 300, 120, "reversible", "invoke")
        assert overlay._flashes == []
        assert overlay._flash_timer.started == 0
        assert overlay.updates == []

    def test_degenerate_rects_are_not_flashed(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        overlay.flash_action(100, 100, 0, 120, "reversible", "invoke")
        assert overlay._flashes == []

    def test_the_flash_list_is_bounded(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        for i in range(config.ACTION_HIGHLIGHT_MAX_CONCURRENT + 3):
            overlay.flash_action(i * 10, 100, 50, 50, "reversible", "invoke")
        assert len(overlay._flashes) == config.ACTION_HIGHLIGHT_MAX_CONCURRENT

    def test_expiry_clears_the_list_and_stops_the_timer(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        overlay.flash_action(100, 100, 300, 120, "reversible", "invoke")
        overlay._flashes[0]["start"] = 0.0
        overlay._on_flash_tick()
        assert overlay._flashes == []
        assert overlay._flash_timer.stopped == 1
        # The expired rect still repainted once, or its last frame would stay forever.
        assert overlay.updates

    def test_a_still_active_flash_keeps_the_timer(self, overlay, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        overlay.flash_action(100, 100, 300, 120, "reversible", "invoke")
        overlay._on_flash_tick()
        assert len(overlay._flashes) == 1
        assert overlay._flash_timer.stopped == 0

    def test_dirty_rect_covers_halo_and_label(self, overlay):
        rect = QRect(200, 200, 300, 100)
        dirty = overlay._flash_dirty_rect(rect)
        margin = (config.ACTION_HIGHLIGHT_MAX_GROW_PX
                  + config.ACTION_HIGHLIGHT_DIRTY_MARGIN_PX)
        assert dirty.left() <= rect.left() - margin
        assert dirty.top() <= rect.top() - margin
        assert dirty.right() >= rect.right() + margin
        assert dirty.bottom() >= rect.bottom() + margin


class TestFlashRendering:
    def _render(self, overlay, level, progress):
        img = QImage(700, 500, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        overlay._draw_action_flash(painter, QRect(150, 150, 300, 120),
                                   level, "invoke", progress)
        painter.end()
        return img

    @staticmethod
    def _ink(img):
        count = 0
        for y in range(img.height()):
            for x in range(img.width()):
                if img.pixelColor(x, y).alpha() > 0:
                    count += 1
        return count

    @pytest.mark.parametrize("level", ["reversible", "medium", "irreversible", "other"])
    @pytest.mark.parametrize("progress", [0.0, 0.3, 0.7, 1.0])
    def test_every_level_and_phase_renders(self, overlay, level, progress):
        """Rendering is the only way to catch a NameError in a paint branch."""
        img = self._render(overlay, level, progress)
        assert img is not None

    def test_a_flash_draws_real_ink(self, overlay):
        img = self._render(overlay, "reversible", 0.2)
        assert self._ink(img) > 1000, "the flash must be visible, not a hairline"

    def test_label_falls_inside_when_there_is_no_room_above(self, overlay):
        img = QImage(500, 400, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        painter = QPainter(img)
        overlay._draw_action_flash(painter, QRect(10, 0, 200, 60),
                                   "medium", "resize", 0.2)
        painter.end()
        assert self._ink(img) > 0


class TestDaemonPlumbing:
    class _Signal:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)

    def _daemon(self):
        d = AutopilotDaemon.__new__(AutopilotDaemon)
        d.overlay = SimpleNamespace(sig_flash_action=self._Signal())
        return d

    def test_element_flash_reads_bounds_and_emits(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.uia = SimpleNamespace(element_bounds=lambda h, p: (10, 20, 300, 100))
        asyncio.run(d._flash_element(1, "2.0", "reversible", "invoke"))
        assert d.overlay.sig_flash_action.calls == [
            (10, 20, 300, 100, "reversible", "invoke")
        ]

    def test_element_without_bounds_is_not_flashed(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.uia = SimpleNamespace(element_bounds=lambda h, p: None)
        asyncio.run(d._flash_element(1, "2.0", "reversible", "invoke"))
        assert d.overlay.sig_flash_action.calls == []

    def test_window_flash_converts_ltrb_to_xywh(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.uia = SimpleNamespace(window_bounds=lambda h: (-1920, 351, 0, 1431))
        asyncio.run(d._flash_window(1, "medium", "resize"))
        assert d.overlay.sig_flash_action.calls == [
            (-1920, 351, 1920, 1080, "medium", "resize")
        ]

    def test_minimized_sentinel_is_never_flashed(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.uia = SimpleNamespace(window_bounds=lambda h: (-32000, -32000, -31840, -31972))
        asyncio.run(d._flash_window(1, "medium", "restore"))
        assert d.overlay.sig_flash_action.calls == []

    def test_disabled_config_short_circuits_the_uia_read(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", False)
        d = self._daemon()
        d.uia = SimpleNamespace(element_bounds=lambda h, p: pytest.fail("must not read"))
        asyncio.run(d._flash_element(1, "2.0", "reversible", "invoke"))
        assert d.overlay.sig_flash_action.calls == []

    def test_monitor_flash_uses_the_monitor_bounds(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.vision = SimpleNamespace(
            get_monitor_bounds=lambda index: (
                {"left": -1920, "top": 351, "width": 1920, "height": 1080}, None)
        )
        asyncio.run(d._flash_monitor(2, "reversible", "scroll down"))
        assert d.overlay.sig_flash_action.calls == [
            (-1920, 351, 1920, 1080, "reversible", "scroll down")
        ]

    def test_foreground_flash_targets_the_focused_window(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()
        d.uia = SimpleNamespace(window_bounds=lambda h: (10, 10, 410, 210))
        monkeypatch.setattr("ctypes.windll.user32.GetForegroundWindow", lambda: 42)
        asyncio.run(d._flash_foreground("medium", "hotkey"))
        assert d.overlay.sig_flash_action.calls == [(10, 10, 400, 200, "medium", "hotkey")]

    def test_a_bounds_read_failure_never_breaks_the_action(self, monkeypatch):
        monkeypatch.setattr(config, "ACTION_HIGHLIGHTS", True)
        d = self._daemon()

        def boom(_hwnd, _path):
            raise RuntimeError("provider died")

        d.uia = SimpleNamespace(element_bounds=boom)
        asyncio.run(d._flash_element(1, "2.0", "reversible", "invoke"))
        assert d.overlay.sig_flash_action.calls == []


class TestElementBounds:
    def _client(self, element):
        client = UIAClient()
        client._element_at_path = lambda hwnd, path: element
        return client

    def test_a_visible_element_returns_xywh(self):
        element = SimpleNamespace(
            CurrentIsOffscreen=False,
            CurrentBoundingRectangle=SimpleNamespace(left=100, top=50, right=400, bottom=200),
        )
        assert self._client(element).element_bounds(1, "2.0") == (100, 50, 300, 150)

    def test_offscreen_element_has_no_rect(self):
        element = SimpleNamespace(CurrentIsOffscreen=True)
        assert self._client(element).element_bounds(1, "2.0") is None

    def test_minimized_sentinel_has_no_rect(self):
        element = SimpleNamespace(
            CurrentIsOffscreen=False,
            CurrentBoundingRectangle=SimpleNamespace(
                left=-32000, top=-32000, right=-31840, bottom=-31972),
        )
        assert self._client(element).element_bounds(1, "2.0") is None

    def test_zero_size_has_no_rect(self):
        element = SimpleNamespace(
            CurrentIsOffscreen=False,
            CurrentBoundingRectangle=SimpleNamespace(left=10, top=10, right=10, bottom=10),
        )
        assert self._client(element).element_bounds(1, "2.0") is None

    def test_stale_path_has_no_rect(self):
        assert self._client(None).element_bounds(1, "9.9") is None
