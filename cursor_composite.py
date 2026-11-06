"""Composites the real system cursor into a captured frame.

The daemon previously drew a fixed red arrow polygon at the cursor position. That was
wrong in two ways at once: it reported a shape the cursor did not have -- an I-beam over a
text field, an hourglass while an application was busy, and a "no" cursor over something
that would refuse the action all rendered as the same red arrow -- and it covered the
pixels underneath with an opaque fill, so the very thing the agent was about to click was
hidden by the marker meant to show where the click would go.

Drawing the actual cursor fixes both. The shape carries information the frame otherwise
loses, and a real cursor is mostly transparent outside its own outline, so it occludes far
less.

Everything here fails soft. A caller that cannot get a real cursor gets None and falls
back to whatever it was doing before, because a capture that returns no frame at all is
worse than a frame with an approximate marker in it.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PIL import Image

logger = logging.getLogger("cursor_composite")

DI_NORMAL = 0x0003
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_CXCURSOR = 13
SM_CYCURSOR = 14


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p), ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


CURSORINFO_SHOWING = 0x00000001


class CursorCapture:
    """The real cursor, as an RGBA image plus the hotspot that marks its active pixel.

    The hotspot is not decoration. It is the point that lands on the target, and for most
    cursors it is not the top-left of the image: an arrow's is its tip, an I-beam's is the
    centre of the bar. Compositing at the reported cursor position without subtracting the
    hotspot draws the cursor offset from the point it actually refers to, which is a worse
    lie than the fixed arrow was, because it looks authoritative.
    """

    __slots__ = ("image", "hotspot", "position", "shape_handle")

    def __init__(self, image: Image.Image, hotspot: tuple[int, int],
                 position: tuple[int, int], shape_handle: int) -> None:
        self.image = image
        self.hotspot = hotspot
        self.position = position
        self.shape_handle = shape_handle


def capture_cursor() -> CursorCapture | None:
    """Grabs the current cursor as an RGBA image, or None if it cannot be done.

    Returns None rather than raising for every failure mode, because the caller is in the
    middle of producing a screenshot and a cursor it cannot draw is not a reason to lose
    the frame.
    """
    if sys.platform != "win32":
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                       wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                  wintypes.HICON, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DrawIconEx.restype = wintypes.BOOL

    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        logger.debug("GetCursorInfo failed: %s", ctypes.GetLastError())
        return None
    if not (info.flags & CURSORINFO_SHOWING):
        # Not an error: the pointer is genuinely hidden. Drawing a cursor that is not on
        # screen would be the same lie the fixed arrow told, in a new form.
        return None
    handle = int(info.hCursor or 0)
    if not handle:
        return None

    icon_info = ICONINFO()
    if not user32.GetIconInfo(wintypes.HICON(handle), ctypes.byref(icon_info)):
        logger.debug("GetIconInfo failed: %s", ctypes.GetLastError())
        return None

    # GetIconInfo hands back two bitmaps the caller owns. Not deleting them leaks GDI
    # objects, and the process-wide handle table is finite -- a daemon that captures a
    # frame per action would exhaust it and then fail at something unrelated, far from
    # the cause.
    mem_dc = None
    dib = None
    old = None
    try:
        bm = BITMAP()
        source = icon_info.hbmColor or icon_info.hbmMask
        if not source:
            return None
        if gdi32.GetObjectW(wintypes.HGDIOBJ(source), ctypes.sizeof(BITMAP),
                            ctypes.byref(bm)) == 0:
            return None
        width, height = int(bm.bmWidth), abs(int(bm.bmHeight))
        if width <= 0 or height <= 0:
            width = user32.GetSystemMetrics(SM_CXCURSOR) or 32
            height = user32.GetSystemMetrics(SM_CYCURSOR) or 32

        mem_dc = gdi32.CreateCompatibleDC(None)
        if not mem_dc:
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        # Negative height asks for a top-down DIB, so row 0 of the buffer is the top of
        # the image. A positive height gives bottom-up rows, and reading those as
        # top-down flips the cursor vertically -- an arrow would point up and look like a
        # bug in the caller rather than in the sign of one field.
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        if not dib or not bits:
            return None
        old = gdi32.SelectObject(mem_dc, wintypes.HGDIOBJ(dib))

        # Zeroed so the cursor composites over transparency rather than over whatever the
        # section happened to contain.
        ctypes.memset(bits, 0, width * height * 4)

        if not user32.DrawIconEx(mem_dc, 0, 0, wintypes.HICON(handle), width, height,
                                 0, None, DI_NORMAL):
            logger.debug("DrawIconEx failed: %s", ctypes.GetLastError())
            return None

        raw = ctypes.string_at(bits, width * height * 4)
    except Exception as exc:  # pragma: no cover - defensive, logged not raised
        logger.debug("cursor capture failed: %s", exc)
        return None
    finally:
        try:
            if mem_dc and old:
                gdi32.SelectObject(mem_dc, old)
            if dib:
                gdi32.DeleteObject(wintypes.HGDIOBJ(dib))
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if icon_info.hbmMask:
                gdi32.DeleteObject(icon_info.hbmMask)
            if icon_info.hbmColor:
                gdi32.DeleteObject(icon_info.hbmColor)
        except Exception as exc:  # pragma: no cover
            logger.debug("cursor GDI cleanup failed: %s", exc)

    image = _bgra_premultiplied_to_rgba(raw, width, height)
    if image is None:
        return None
    return CursorCapture(
        image=image,
        hotspot=(int(icon_info.xHotspot), int(icon_info.yHotspot)),
        position=(int(info.ptScreenPos.x), int(info.ptScreenPos.y)),
        shape_handle=handle,
    )


def _bgra_premultiplied_to_rgba(raw: bytes, width: int, height: int) -> Image.Image | None:
    """Converts a 32-bit BGRA DIB, whose colour channels DrawIconEx leaves premultiplied,
    into a straight-alpha RGBA image.

    Skipping the un-premultiply is the usual mistake and it is subtle: fully opaque pixels
    look correct either way, and a cursor is mostly opaque, so the error shows only on the
    anti-aliased edge as a dark fringe where the composited cursor meets the desktop.
    """
    try:
        # rawmode "RGBA" means "take the bytes as they are", not "they are red-green-blue".
        # Asking for "BGRA" here looks like the clearer spelling of the same intent and is
        # not: Pillow's raw decoder does not reorder for that name, so the buffer arrives
        # in DIB order (B, G, R, A) either way, and a loop below that unpacks
        # ``b, g, r, a`` and writes back ``r, g, b, a`` would swap twice and cancel out.
        # The net effect is a blue cursor rendered red -- invisible in any test that only
        # checks that something was drawn, which is why the channel-order test exists.
        # Reading with no reinterpretation makes the single swap below the only one.
        # frombuffer rather than frombytes would be the wrong call here for a second
        # reason: with a rawmode that matches the image mode it shares the buffer and marks
        # the result read-only, and the un-premultiply loop below writes into it. frombytes
        # copies, so the image is writable.
        img = Image.frombytes("RGBA", (width, height), raw, "raw", "RGBA")
    except Exception as exc:  # pragma: no cover
        logger.debug("cursor buffer conversion failed: %s", exc)
        return None

    px = img.load()
    if px is None:  # pragma: no cover
        return None
    for y in range(height):
        for x in range(width):
            b, g, r, a = px[x, y]
            if a == 0:
                px[x, y] = (0, 0, 0, 0)
            elif a < 255:
                # Divide back out of premultiplication. Round rather than truncate: a
                # truncating divide darkens every semi-transparent pixel by up to one
                # level, which accumulates into a visible edge.
                px[x, y] = (min(255, (r * 255 + a // 2) // a),
                            min(255, (g * 255 + a // 2) // a),
                            min(255, (b * 255 + a // 2) // a), a)
            else:
                px[x, y] = (r, g, b, 255)
    return img


def composite(target: Image.Image, cursor: CursorCapture, x: int, y: int) -> None:
    """Alpha-composites the cursor onto target with its hotspot at (x, y).

    Drawn in place rather than pasted, so the semi-transparent edge of a real cursor
    blends with the desktop under it instead of replacing it -- the occlusion that made the
    fixed arrow actively harmful.
    """
    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    hx, hy = cursor.hotspot
    layer.paste(cursor.image, (int(x) - int(hx), int(y) - int(hy)), cursor.image)
    merged = Image.alpha_composite(target.convert("RGBA"), layer)
    target.paste(merged.convert(target.mode), (0, 0))
