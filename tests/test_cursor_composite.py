"""Tests for native cursor compositing.

The daemon drew a fixed red arrow at the cursor position. That reported the same shape
every time -- an I-beam over a text field, a wait cursor while the application was busy,
and a "no" cursor over something that would refuse the action all looked identical -- and
it painted over the pixels underneath, including the control the agent was about to click.

These tests are live: they capture the real cursor. A faked capture would test the plumbing
and not the thing that was wrong.

The distinguishing assertion is that native mode does NOT produce the red arrow. Without
it, a silent fallthrough to the fallback passes every test that only checks "something was
drawn", which is precisely the failure the change exists to remove.
"""
from __future__ import annotations

import ctypes
import sys

import pytest
from PIL import Image

import config
import cursor_composite as cc
from vision_engine import VisionEngine

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")

BG = (30, 30, 30)


def blank(w=400, h=300):
    return Image.new("RGB", (w, h), BG)


def changed_pixels(a, b, step=2):
    return sum(1 for y in range(0, a.height, step) for x in range(0, a.width, step)
               if a.getpixel((x, y)) != b.getpixel((x, y)))


def saturated_red_count(img, cx, cy, r=18):
    """Counts the drawn marker's signature colour. Zero means the native path ran."""
    n = 0
    for y in range(max(0, cy - r), min(img.height, cy + r)):
        for x in range(max(0, cx - r), min(img.width, cx + r)):
            pr, pg, _pb = img.getpixel((x, y))[:3]
            if pr > 200 and pg < 80:
                n += 1
    return n


class TestCapture:
    def test_it_returns_a_real_cursor_image(self):
        shot = cc.capture_cursor()
        if shot is None:
            pytest.skip("the pointer is hidden right now; nothing to capture")
        assert shot.image.mode == "RGBA"
        assert shot.image.width > 0 and shot.image.height > 0
        px = shot.image.load()
        opaque = sum(1 for y in range(shot.image.height)
                     for x in range(shot.image.width) if px[x, y][3] > 0)
        assert opaque > 20, "the captured cursor is essentially empty"

    def test_the_hotspot_is_inside_the_image(self):
        """A hotspot outside the image would composite the cursor offset from the point it
        refers to -- a worse lie than the fixed arrow, because it looks authoritative."""
        shot = cc.capture_cursor()
        if shot is None:
            pytest.skip("the pointer is hidden right now")
        hx, hy = shot.hotspot
        assert 0 <= hx < shot.image.width
        assert 0 <= hy < shot.image.height

    def test_the_cursor_is_mostly_transparent(self):
        """The point of compositing the real cursor instead of an opaque polygon: it
        occludes only what the cursor itself covers."""
        shot = cc.capture_cursor()
        if shot is None:
            pytest.skip("the pointer is hidden right now")
        px = shot.image.load()
        total = shot.image.width * shot.image.height
        opaque = sum(1 for y in range(shot.image.height)
                     for x in range(shot.image.width) if px[x, y][3] == 255)
        assert opaque < total * 0.9, (
            f"{opaque}/{total} fully opaque; a real cursor is not a solid block")

    def test_it_does_not_leak_gdi_handles(self):
        """The daemon captures a frame per action. GetIconInfo hands back two bitmaps the
        caller owns, so an undeleted pair per capture exhausts a process-wide table in a
        few hundred actions and then fails somewhere unrelated.

        The counter is validated first, because GetGuiResources returns 0 on failure as
        well as for a process with no objects -- a check reading 0 before and 0 after
        would prove nothing.
        """
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32
        user32.GetGuiResources.restype = ctypes.c_uint
        user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]

        def gdi():
            return user32.GetGuiResources(
                ctypes.c_void_p(kernel32.GetCurrentProcess()), 0)

        held = [h for h in (gdi32.CreateCompatibleDC(None) for _ in range(3)) if h]
        with_held = gdi()
        for h in held:
            gdi32.DeleteDC(ctypes.c_void_p(h))
        baseline = gdi()
        assert with_held - baseline == len(held), (
            f"GetGuiResources moved by {with_held - baseline} for {len(held)} objects; "
            "the counter is not usable, so this test would prove nothing")

        captures = sum(1 for _ in range(200) if cc.capture_cursor() is not None)
        assert captures > 0, "no cursor could be captured at all"
        growth = gdi() - baseline
        assert growth <= 2, f"GDI grew by {growth} over {captures} captures"


class TestUnpremultiply:
    def test_opaque_pixels_pass_through_unchanged(self):
        # BGRA, fully opaque blue-ish pixel.
        raw = bytes([200, 100, 50, 255])
        img = cc._bgra_premultiplied_to_rgba(raw, 1, 1)
        assert img.getpixel((0, 0)) == (50, 100, 200, 255)

    def test_fully_transparent_pixels_become_zero(self):
        raw = bytes([17, 34, 51, 0])
        img = cc._bgra_premultiplied_to_rgba(raw, 1, 1)
        assert img.getpixel((0, 0)) == (0, 0, 0, 0)

    def test_a_half_transparent_pixel_is_un_premultiplied(self):
        """DrawIconEx leaves the colour channels premultiplied. Reading them as straight
        alpha darkens every anti-aliased edge pixel, which shows as a fringe where the
        cursor meets the desktop -- subtle, because the mostly-opaque body looks fine."""
        # alpha 128, premultiplied red 64 => straight red ~128
        raw = bytes([0, 0, 64, 128])
        img = cc._bgra_premultiplied_to_rgba(raw, 1, 1)
        r, _g, _b, a = img.getpixel((0, 0))
        assert a == 128
        assert 125 <= r <= 131, f"expected ~128 straight red, got {r}"

    def test_un_premultiplying_never_exceeds_255(self):
        raw = bytes([255, 255, 255, 1])
        img = cc._bgra_premultiplied_to_rgba(raw, 1, 1)
        r, g, b, _a = img.getpixel((0, 0))
        assert max(r, g, b) <= 255

    def test_channel_order_is_bgra_not_rgba(self):
        """Reading BGRA as RGBA swaps red and blue. A blue cursor would render red and
        nothing else would look wrong."""
        raw = bytes([255, 0, 0, 255])   # B=255, G=0, R=0
        img = cc._bgra_premultiplied_to_rgba(raw, 1, 1)
        assert img.getpixel((0, 0)) == (0, 0, 255, 255)


class TestPaintCursor:
    def test_native_mode_does_not_draw_the_red_arrow(self):
        """The assertion that distinguishes the two paths. Without it, a silent fallthrough
        to the fallback passes every test that merely checks something was drawn."""
        shot = cc.capture_cursor()
        if shot is None:
            pytest.skip("the pointer is hidden right now")
        ve = VisionEngine()
        img = blank()
        ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        assert changed_pixels(img, blank()) > 0, "nothing was painted"
        assert saturated_red_count(img, 200, 150) == 0, (
            "native mode produced the red arrow, so it silently fell back")

    def test_drawn_mode_still_produces_the_red_arrow(self):
        ve = VisionEngine()
        img = blank()
        original = config.CURSOR_RENDER_MODE
        try:
            config.CURSOR_RENDER_MODE = "drawn"
            ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        finally:
            config.CURSOR_RENDER_MODE = original
        assert saturated_red_count(img, 200, 150) > 50

    def test_off_mode_paints_nothing(self):
        ve = VisionEngine()
        img = blank()
        original = config.CURSOR_RENDER_MODE
        try:
            config.CURSOR_RENDER_MODE = "off"
            ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        finally:
            config.CURSOR_RENDER_MODE = original
        assert changed_pixels(img, blank()) == 0

    def test_it_falls_back_when_the_cursor_cannot_be_captured(self, monkeypatch):
        """A cursor that cannot be read must not cost the frame it is drawn onto."""
        monkeypatch.setattr(cc, "capture_cursor", lambda: None)
        ve = VisionEngine()
        img = blank()
        ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        assert saturated_red_count(img, 200, 150) > 50, (
            "no fallback marker was drawn when the real cursor was unavailable")

    def test_it_falls_back_when_compositing_raises(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("composite failed")

        monkeypatch.setattr(cc, "composite", boom)
        ve = VisionEngine()
        img = blank()
        ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        assert saturated_red_count(img, 200, 150) > 50

    def test_it_falls_back_when_capture_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("capture failed")

        monkeypatch.setattr(cc, "capture_cursor", boom)
        ve = VisionEngine()
        img = blank()
        ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        assert saturated_red_count(img, 200, 150) > 50

    def test_a_degenerate_cursor_image_falls_back(self, monkeypatch):
        """A zero-sized image would make resize raise or paste nothing; either way the
        frame should still get a marker."""
        empty = cc.CursorCapture(Image.new("RGBA", (0, 0)), (0, 0), (0, 0), 1)
        monkeypatch.setattr(cc, "capture_cursor", lambda: empty)
        ve = VisionEngine()
        img = blank()
        ve._paint_cursor(img, 200, 150, (1.0, 1.0))
        assert saturated_red_count(img, 200, 150) > 50

    def test_the_scaled_path_runs_and_scales_the_hotspot(self, monkeypatch):
        """The frame is downscaled to a fixed width; a cursor left at native size would
        appear twice as large as it does on the operator's screen."""
        seen = {}

        def fake_composite(target, shot, x, y):
            seen["size"] = shot.image.size
            seen["hotspot"] = shot.hotspot

        monkeypatch.setattr(cc, "composite", fake_composite)
        shot = cc.CursorCapture(Image.new("RGBA", (32, 32), (0, 0, 0, 0)),
                                (8, 4), (0, 0), 1)
        monkeypatch.setattr(cc, "capture_cursor", lambda: shot)
        ve = VisionEngine()
        ve._paint_cursor(blank(200, 150), 100, 75, (2.0, 2.0))
        assert seen["size"] == (16, 16)
        assert seen["hotspot"] == (4, 2)

    def test_scale_of_one_leaves_the_cursor_untouched(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            cc, "composite",
            lambda t, shot, x, y: seen.update(size=shot.image.size))
        shot = cc.CursorCapture(Image.new("RGBA", (32, 32), (0, 0, 0, 0)),
                                (0, 0), (0, 0), 1)
        monkeypatch.setattr(cc, "capture_cursor", lambda: shot)
        VisionEngine()._paint_cursor(blank(200, 150), 100, 75, (1.0, 1.0))
        assert seen["size"] == (32, 32)


class TestComposite:
    def test_it_changes_the_target_around_the_point(self):
        shot = cc.capture_cursor()
        if shot is None:
            pytest.skip("the pointer is hidden right now")
        img = blank()
        before = img.copy()
        cc.composite(img, shot, 200, 150)
        assert changed_pixels(img, before) > 0

    def test_the_hotspot_lands_on_the_given_point(self):
        """Compositing at the reported position without subtracting the hotspot draws the
        cursor offset from the point it refers to."""
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        mark = Image.new("RGBA", (5, 5), (0, 0, 0, 0))
        mark.putpixel((2, 2), (255, 0, 0, 255))
        shot = cc.CursorCapture(mark, (2, 2), (0, 0), 1)
        cc.composite(img, shot, 30, 40)
        assert img.getpixel((30, 40))[:3] == (255, 0, 0), (
            "the hotspot pixel did not land on the requested point")

    def test_it_preserves_the_target_mode(self):
        shot = cc.CursorCapture(Image.new("RGBA", (4, 4), (255, 0, 0, 255)), (0, 0),
                                (0, 0), 1)
        img = blank()
        cc.composite(img, shot, 200, 150)
        assert img.mode == "RGB"

    def test_it_tolerates_a_point_outside_the_image(self):
        shot = cc.CursorCapture(Image.new("RGBA", (4, 4), (255, 0, 0, 255)), (0, 0),
                                (0, 0), 1)
        img = blank()
        cc.composite(img, shot, -50, -50)
        cc.composite(img, shot, 10_000, 10_000)
