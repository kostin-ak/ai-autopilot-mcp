"""Tests for the cached neon border.

The cache must not change what is drawn -- only how often it is computed. The test that
matters is therefore a pixel comparison between the cached path and the direct path, not a
count of cache hits. Rendering runs on the offscreen Qt platform so no display is needed.

The border cannot be checked by looking at a screenshot: the overlay carries
WDA_EXCLUDEFROMCAPTURE and appears in no capture. So this comparison is the only evidence
available, and it has to be a comparison rather than an assertion that a pixmap exists.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

import config
import overlay_ui


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    a = QApplication([])
    yield a


@pytest.fixture
def overlay(app):
    win = overlay_ui.HUDOverlayWindow.__new__(overlay_ui.HUDOverlayWindow)
    win._border_cache = None
    win._state = config.AutopilotState.AI_ACTIVE
    win._target_rect = None
    win._app_signals_connected = False
    win._tracked_screens = []
    # The instance is built with __new__, so it has no C++ counterpart. Calling a real
    # QWidget method on it does not raise -- it takes the interpreter down, which is what
    # the first run of this file did. Everything the code under test reaches into on the
    # widget has to be stubbed.
    win.devicePixelRatioF = lambda: 1.0
    return win


RECT = QRect(40, 30, 400, 240)


def render(overlay, rect, state, painter_fn):
    """Renders into a QImage through the given function and returns the image."""
    img = QImage(rect.width() + 200, rect.height() + 200,
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter_fn(p, rect, state)
    p.end()
    return img


def non_transparent_pixels(img):
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if QColor(img.pixel(x, y)).alpha() > 0:
                n += 1
    return n


def images_identical(a, b):
    if a.size() != b.size():
        return False
    for y in range(a.height()):
        for x in range(a.width()):
            if a.pixel(x, y) != b.pixel(x, y):
                return False
    return True


class TestCacheDoesNotChangeOutput:
    def test_the_cached_border_matches_the_direct_one(self, overlay):
        """The whole risk of caching, stated as a test. A cache that returns slightly
        different pixels would look correct in every test that only counts hits."""
        cached = render(overlay, RECT, config.AutopilotState.AI_ACTIVE,
                        overlay._draw_neon_border)
        assert overlay._border_cache is not None, "nothing was cached"
        direct = render(overlay, RECT, config.AutopilotState.AI_ACTIVE,
                        overlay._paint_border_into)
        assert images_identical(cached, direct), (
            "the cached render differs from the direct one; the translation into the "
            "pixmap's coordinate space is wrong")

    def test_the_border_actually_draws_something(self, overlay):
        """Guards the comparison above from passing because both paths drew nothing."""
        img = render(overlay, RECT, config.AutopilotState.AI_ACTIVE,
                     overlay._draw_neon_border)
        assert non_transparent_pixels(img) > 500

    def test_the_override_state_matches_too(self, overlay):
        """The override branch uses a flat colour rather than a gradient, so it is a
        separate code path through the same cache."""
        cached = render(overlay, RECT, config.AutopilotState.USER_OVERRIDE,
                        overlay._draw_neon_border)
        direct = render(overlay, RECT, config.AutopilotState.USER_OVERRIDE,
                        overlay._paint_border_into)
        assert images_identical(cached, direct)

    def test_the_two_states_differ(self, overlay):
        """If they did not, a stale colour would be invisible and the invalidation would
        be untestable."""
        active = render(overlay, RECT, config.AutopilotState.AI_ACTIVE,
                        overlay._draw_neon_border)
        override = render(overlay, RECT, config.AutopilotState.USER_OVERRIDE,
                          overlay._draw_neon_border)
        assert not images_identical(active, override)


class TestCacheBehaviour:
    def test_a_second_paint_reuses_the_cached_pixmap(self, overlay):
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        first = overlay._border_cache
        assert first is not None
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        assert overlay._border_cache is first, "the cache was rebuilt for identical inputs"

    def test_a_state_change_rebuilds(self, overlay):
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        first = overlay._border_cache
        overlay._draw_neon_border(_NullPainter(), RECT,
                                  config.AutopilotState.USER_OVERRIDE)
        assert overlay._border_cache is not first

    def test_a_rect_change_rebuilds(self, overlay):
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        first = overlay._border_cache
        overlay._draw_neon_border(_NullPainter(), QRect(41, 30, 400, 240),
                                  config.AutopilotState.AI_ACTIVE)
        assert overlay._border_cache is not first

    def test_the_cached_image_covers_only_the_band_not_the_desktop(self, overlay):
        """The point of caching the band rather than the window: a full-surface cache at
        4480x1440 would hold about 26 MB to store a thin rectangle."""
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        pm = overlay._border_cache[1]
        band = config.HUD_BORDER_WIDTH + config.HUD_GLOW_RADIUS
        dpr = pm.devicePixelRatio() or 1.0
        assert int(pm.width() / dpr) == RECT.width() + 2 * band
        assert int(pm.height() / dpr) == RECT.height() + 2 * band

    def test_explicit_invalidation_clears(self, overlay):
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        assert overlay._border_cache is not None
        overlay._invalidate_border_cache()
        assert overlay._border_cache is None

    def test_invalidation_is_idempotent(self, overlay):
        overlay._invalidate_border_cache()
        overlay._invalidate_border_cache()
        assert overlay._border_cache is None

    def test_the_origin_places_the_band_around_the_rect(self, overlay):
        overlay._draw_neon_border(_NullPainter(), RECT, config.AutopilotState.AI_ACTIVE)
        origin = overlay._border_cache[2]
        band = config.HUD_BORDER_WIDTH + config.HUD_GLOW_RADIUS
        assert origin == QPoint(RECT.x() - band, RECT.y() - band)

    def test_a_degenerate_rect_does_not_crash_or_cache_garbage(self, overlay):
        """A rect collapsed to nothing makes every adjusted rect non-positive, and the
        loop breaks on the first step. It must still produce a valid pixmap rather than a
        zero-sized one, which Qt would reject."""
        tiny = QRect(10, 10, 1, 1)
        overlay._draw_neon_border(_NullPainter(), tiny, config.AutopilotState.AI_ACTIVE)
        assert overlay._border_cache is not None
        assert overlay._border_cache[1].width() > 0


class _NullPainter:
    """Accepts the draw calls the cache makes without needing a paint device.

    The cache-miss path renders into a real QPixmap with a real QPainter before this is
    used, so what is skipped is only the final blit, which the pixel tests above cover.
    """

    def drawPixmap(self, *_a, **_k):
        pass

    def setRenderHint(self, *_a, **_k):
        pass
