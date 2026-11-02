"""Screen capture, perceptual diff stabilization, cursor rendering, and UI element inspection."""

import base64
import ctypes
import io
import logging
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

import config

logger = logging.getLogger(__name__)


class ScreenCaptureError(Exception):
    """Raised when screen capture fails (e.g. Windows secure desktop / UAC)."""


@dataclass
class MonitorInfo:
    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool


@dataclass
class WindowInfo:
    handle: int
    title: str
    rect: tuple[int, int, int, int]
    monitor_index: int
    is_active: bool


@dataclass
class UIElementInfo:
    control_type: str
    title: str
    automation_id: str
    scaled_center_x: int
    scaled_center_y: int
    abs_center_x: int
    abs_center_y: int
    rect: tuple[int, int, int, int]


class VisionEngine:
    """Handles multi-monitor frame acquisition, stabilization, and accessibility inspection."""

    def __init__(self) -> None:
        pass

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerates connected monitors through mss and platform metrics."""
        config.ensure_interactive_desktop()
        monitors: list[MonitorInfo] = []
        import mss

        with mss.mss() as sct:
            for idx, m in enumerate(sct.monitors):
                name = "All Displays (Virtual Desktop)" if idx == 0 else f"Display {idx}"
                is_primary = idx == 1
                monitors.append(
                    MonitorInfo(
                        index=idx,
                        name=name,
                        left=m["left"],
                        top=m["top"],
                        width=m["width"],
                        height=m["height"],
                        is_primary=is_primary,
                    )
                )
        return monitors

    def get_monitor_bounds(self, monitor_index: int | None = None) -> tuple[dict[str, int], MonitorInfo]:
        """Resolves target monitor dictionary for mss capture.

        An out-of-range index raises instead of falling back. The fallback this
        replaces silently switched to the whole virtual desktop, which on a
        two-monitor setup changes the frame from 2560x1440 to 4480x1440 and the
        downscale from 2.0x to 3.5x -- every coordinate the model has accumulated
        becomes wrong, with no error to tell it so.
        """
        monitors = self.list_monitors()
        if monitor_index is None:
            # Default to primary monitor if available, otherwise first available
            target = next((m for m in monitors if m.is_primary), monitors[0])
        else:
            if not 0 <= monitor_index < len(monitors):
                available = ", ".join(str(m.index) for m in monitors)
                raise ScreenCaptureError(
                    f"monitor_index={monitor_index} does not exist. "
                    f"Available indexes: {available}. "
                    "Index 0 spans every display, 1 is the primary monitor."
                )
            target = monitors[monitor_index]

        bbox = {
            "left": target.left,
            "top": target.top,
            "width": target.width,
            "height": target.height,
        }
        return bbox, target

    def capture_screen(
        self,
        monitor_index: int | None = None,
        cursor_pos: tuple[int, int] | None = None,
        wait_stabilization: bool = False,
        show_grid: bool = True,
    ) -> tuple[str, int, int, float, float, MonitorInfo]:
        """
        Captures target display, waits for animations to finish, overlays cursor, and compresses to WebP.

        Returns:
            base64_webp (str): Base64 encoded WebP image
            scaled_width (int): Target image width (default 1280)
            scaled_height (int): Proportional image height
            scale_x (float): Conversion factor from scaled to desktop X
            scale_y (float): Conversion factor from scaled to desktop Y
            target_monitor (MonitorInfo): Active monitor metadata
        """
        config.ensure_interactive_desktop()
        bbox, target_monitor = self.get_monitor_bounds(monitor_index)
        import mss

        try:
            with mss.mss() as sct:
                if wait_stabilization:
                    pil_image = self._capture_stabilized_frame(sct, bbox)
                else:
                    raw_frame = sct.grab(bbox)
                    pil_image = Image.frombytes("RGB", raw_frame.size, raw_frame.bgra, "raw", "BGRX")
        except Exception as exc:
            msg = f"Screen capture failed. Windows UAC or secure desktop may be active: {exc}"
            logger.error(msg)
            raise ScreenCaptureError(msg) from exc

        orig_w, orig_h = pil_image.size
        scaled_w = config.TARGET_IMAGE_WIDTH
        scaled_h = int(round(orig_h * (scaled_w / float(orig_w))))

        scale_x = orig_w / float(scaled_w)
        scale_y = orig_h / float(scaled_h)

        # High quality downsampling
        scaled_image = pil_image.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

        # Overlay coordinate grid if requested
        if show_grid:
            scaled_image = self._draw_coordinate_grid(scaled_image)

        # Draw mouse pointer if cursor is inside this monitor's bounds
        if cursor_pos is not None:
            cur_x, cur_y = cursor_pos
            if (
                bbox["left"] <= cur_x < bbox["left"] + bbox["width"]
                and bbox["top"] <= cur_y < bbox["top"] + bbox["height"]
            ):
                img_cur_x = int(round((cur_x - bbox["left"]) / scale_x))
                img_cur_y = int(round((cur_y - bbox["top"]) / scale_y))
                self._draw_cursor_pointer(scaled_image, img_cur_x, img_cur_y)

        # Encode to WebP
        buffer = io.BytesIO()
        scaled_image.save(buffer, format="WEBP", quality=config.WEBP_QUALITY, method=4)
        encoded_webp = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return encoded_webp, scaled_w, scaled_h, scale_x, scale_y, target_monitor

    def _draw_coordinate_grid(self, image: Image.Image) -> Image.Image:
        """Overlays semi-transparent grid lines and coordinate markers to assist model localization."""
        w, h = image.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Top and left ruler background bars for high contrast
        draw.rectangle([(0, 0), (w, 14)], fill=(0, 0, 0, 150))
        draw.rectangle([(0, 0), (28, h)], fill=(0, 0, 0, 150))

        # Vertical grid lines & labels every 100px
        for x in range(100, w, 100):
            draw.line([(x, 14), (x, h)], fill=(255, 255, 255, 40), width=1)
            draw.text((x - 10, 1), str(x), fill=(0, 255, 255, 230))

        # Horizontal grid lines & labels every 100px
        for y in range(100, h, 100):
            draw.line([(28, y), (w, y)], fill=(255, 255, 255, 40), width=1)
            draw.text((2, y - 5), str(y), fill=(0, 255, 255, 230))

        # 50px tick marks along rulers
        for x in range(50, w, 50):
            if x % 100 != 0:
                draw.line([(x, 0), (x, 7)], fill=(255, 255, 255, 180), width=1)

        for y in range(50, h, 50):
            if y % 100 != 0:
                draw.line([(0, y), (7, y)], fill=(255, 255, 255, 180), width=1)

        return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    def _capture_stabilized_frame(self, sct: Any, bbox: dict[str, int]) -> Image.Image:
        """Samples frames until consecutive pixel changes fall below the perceptual threshold."""
        time.sleep(config.STABILIZATION_INTERVAL_SEC)

        prev_thumb: np.ndarray | None = None
        final_image: Image.Image | None = None

        for _iteration in range(config.STABILIZATION_MAX_ITERATIONS):
            raw = sct.grab(bbox)
            current_image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

            # 64x64 grayscale downscale for perceptual diff
            gray_thumb = (
                current_image.convert("L")
                .resize(config.STABILIZATION_RESIZE_DIMS, Image.Resampling.BILINEAR)
            )
            curr_thumb = np.asarray(gray_thumb, dtype=np.float32)

            if prev_thumb is not None:
                diff = float(np.mean(np.abs(curr_thumb - prev_thumb)))
                if diff < config.STABILIZATION_DIFF_THRESHOLD:
                    return current_image

            prev_thumb = curr_thumb
            final_image = current_image
            time.sleep(config.STABILIZATION_INTERVAL_SEC)

        return final_image or Image.new("RGB", (bbox["width"], bbox["height"]))

    def _draw_cursor_pointer(self, image: Image.Image, x: int, y: int) -> None:
        """Draws an unmistakable high-contrast pointer arrow directly on the scaled image."""
        draw = ImageDraw.Draw(image)

        # Arrow polygon vertices relative to tip (0, 0)
        arrow_vertices = [
            (x, y),
            (x, y + 17),
            (x + 4, y + 13),
            (x + 8, y + 21),
            (x + 11, y + 20),
            (x + 7, y + 12),
            (x + 13, y + 12),
        ]

        # Draw black outline border for contrast over light backgrounds
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx != 0 or dy != 0:
                    outline_poly = [(vx + dx, vy + dy) for vx, vy in arrow_vertices]
                    draw.polygon(outline_poly, fill=config.CURSOR_COLOR_OUTLINE)

        # Fill cursor body with saturated red
        draw.polygon(arrow_vertices, fill=config.CURSOR_COLOR_FILL)

    def list_windows(self) -> list[WindowInfo]:
        """Lists active top-level windows and their bounding rects across monitors."""
        windows: list[WindowInfo] = []
        if sys.platform != "win32":
            return windows

        # Called on a pool thread via asyncio.to_thread, and DPI awareness is
        # per-thread: without this, GetWindowRect returns virtualised coordinates
        # on mixed-DPI setups and every window rect is subtly wrong.
        config.ensure_interactive_desktop()

        user32 = ctypes.windll.user32
        active_hwnd = user32.GetForegroundWindow()

        # Cache monitors for hit testing
        monitors = self.list_monitors()

        def _enum_proc(hwnd: int, lparam: int) -> int:
            if not user32.IsWindowVisible(hwnd):
                return 1

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return 1

            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

            if not title:
                return 1

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            # Ignore zero-size and offscreen utility windows
            if w <= 10 or h <= 10:
                return 1

            # Match monitor
            mid_x = (rect.left + rect.right) // 2
            mid_y = (rect.top + rect.bottom) // 2
            matched_monitor = 0
            for m in monitors[1:]:
                if m.left <= mid_x < m.left + m.width and m.top <= mid_y < m.top + m.height:
                    matched_monitor = m.index
                    break

            windows.append(
                WindowInfo(
                    handle=hwnd,
                    title=title,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    monitor_index=matched_monitor,
                    is_active=(hwnd == active_hwnd),
                )
            )
            return 1

        enum_proc_proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(enum_proc_proto(_enum_proc), 0)

        # Sort so active window is first, followed by others
        windows.sort(key=lambda w: not w.is_active)
        return windows

    def inspect_ui_elements(
        self, window_title: str | None = None, monitor_index: int | None = None
    ) -> tuple[list[UIElementInfo], int]:
        """Inspects accessible controls (buttons, edits) with coordinates pre-scaled for LLM clicks.

        Returns the elements whose center lies on the target monitor, plus the
        number skipped for being off it. Off-monitor elements have no valid frame
        coordinate; publishing a negative one is worse than omitting the element,
        because the agent then aims at a point that does not exist in the image it
        is looking at.
        """
        if sys.platform != "win32":
            return [], 0

        # Pool thread; DPI awareness is per-thread. See list_windows.
        config.ensure_interactive_desktop()

        bbox, _ = self.get_monitor_bounds(monitor_index)
        scale_x = bbox["width"] / float(config.TARGET_IMAGE_WIDTH)
        scaled_h = int(round(bbox["height"] * (config.TARGET_IMAGE_WIDTH / float(bbox["width"]))))
        scale_y = bbox["height"] / float(scaled_h)

        elements: list[UIElementInfo] = []
        skipped_off_monitor = 0

        try:
            from pywinauto import Desktop

            desktop = Desktop(backend="uia")
            if window_title:
                # Escape the caller's text. A title containing regex
                # metacharacters -- "[admin]" brackets, parentheses, a trailing
                # backslash -- previously produced re.error or silently matched
                # the wrong window.
                win = desktop.window(
                    title_re=f".*{re.escape(window_title)}.*", visible_only=True
                )
            else:
                user32 = ctypes.windll.user32
                active_hwnd = user32.GetForegroundWindow()
                win = desktop.window(handle=active_hwnd)

            for ctrl in win.descendants():
                try:
                    rect = ctrl.rectangle()
                    # Skip invisible or empty elements
                    if rect.width() <= 4 or rect.height() <= 4:
                        continue

                    # Filter to interactive control types
                    c_type = ctrl.friendly_class_name()
                    if c_type not in (
                        "Button",
                        "Edit",
                        "CheckBox",
                        "RadioButton",
                        "ComboBox",
                        "MenuItem",
                        "TabItem",
                        "ListItem",
                        "Hyperlink",
                    ):
                        continue

                    abs_cx = (rect.left + rect.right) // 2
                    abs_cy = (rect.top + rect.bottom) // 2

                    if not (
                        bbox["left"] <= abs_cx < bbox["left"] + bbox["width"]
                        and bbox["top"] <= abs_cy < bbox["top"] + bbox["height"]
                    ):
                        skipped_off_monitor += 1
                        continue

                    # Convert to scaled screenshot coordinate
                    sc_x = int(round((abs_cx - bbox["left"]) / scale_x))
                    sc_y = int(round((abs_cy - bbox["top"]) / scale_y))

                    text = ctrl.window_text().strip()
                    auto_id = ctrl.automation_id()

                    elements.append(
                        UIElementInfo(
                            control_type=c_type,
                            title=text,
                            automation_id=auto_id,
                            scaled_center_x=sc_x,
                            scaled_center_y=sc_y,
                            abs_center_x=abs_cx,
                            abs_center_y=abs_cy,
                            rect=(rect.left, rect.top, rect.right, rect.bottom),
                        )
                    )
                except Exception:
                    continue

        except Exception as exc:
            logger.warning(f"UI automation inspection encountered an issue: {exc}")

        return elements, skipped_off_monitor


