"""Screen capture, perceptual diff stabilization, cursor rendering, and UI element inspection."""

import base64
import ctypes
import io
import logging
import math
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
    # Nearest monitor by center point. None only when no physical monitor is
    # enumerated at all.
    monitor_index: int | None
    is_active: bool
    # False when the center point fell in a gap between monitors and
    # monitor_index is therefore an approximation, not a containment.
    on_monitor: bool = True
    class_name: str = ""
    pid: int = 0


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


@dataclass
class WindowTarget:
    """The window an inspection actually resolved to."""

    handle: int
    title: str
    class_name: str
    pid: int
    rect: tuple[int, int, int, int]
    resolution: str = ""


@dataclass
class InspectionResult:
    """Elements found, plus how the target window was chosen.

    The caller needs the target back because an empty element list and a
    mis-resolved window look identical from the outside.
    """

    elements: list[UIElementInfo]
    skipped_off_monitor: int
    target: WindowTarget | None
    error: str | None = None


def _target_from(wrapper: Any, resolution: str = "") -> WindowTarget:
    """Builds a WindowTarget from a pywinauto window wrapper, tolerating failures.

    Every accessor is wrapped in a lambda so that the attribute lookup itself
    happens inside the guard. Passing the bound method directly would evaluate
    ``wrapper.window_text`` before ``safe`` is entered and let an AttributeError
    escape for any wrapper that lacks the attribute.
    """

    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    rect = safe(wrapper.rectangle, None)
    return WindowTarget(
        handle=safe(lambda: int(wrapper.handle), 0),
        title=safe(lambda: str(wrapper.window_text() or ""), ""),
        class_name=safe(lambda: str(wrapper.class_name() or ""), ""),
        pid=safe(lambda: int(wrapper.process_id()), 0),
        rect=(
            (rect.left, rect.top, rect.right, rect.bottom) if rect else (0, 0, 0, 0)
        ),
        resolution=resolution,
    )


def _find_hwnd_by_win32_title(
    title_fragment: str, class_name: str | None = None
) -> int | None:
    """Finds a top-level window by its Win32 title, ignoring case.

    Needed because the Win32 title and the UIA Name are different strings for the
    same window. Qt derives the UIA Name from the application name while
    GetWindowText returns the document title: AyuGram's main window is
    "AyuGramDesktop" to UIA and "Избранное @ Alex Kostin" to Win32. Since
    list_windows_and_monitors reports Win32 titles, an agent that copies a title
    from there would otherwise match nothing.

    Returns the largest visible match, for the same reason the UIA path does:
    applications keep several same-titled top-level windows, and the notification
    popups are the small ones.
    """
    user32 = ctypes.windll.user32
    needle = title_fragment.strip().lower()
    if not needle:
        return None

    best: tuple[int, int] | None = None  # (area, hwnd)
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd: int, _lparam: int) -> int:
        nonlocal best
        if not user32.IsWindowVisible(hwnd):
            return 1

        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return 1
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle not in buf.value.strip().lower():
            return 1

        if class_name:
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value.lower() != class_name.lower():
                return 1

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(rect.right - rect.left, 0) * max(rect.bottom - rect.top, 0)
        if area <= 100:
            return 1
        if best is None or area > best[0]:
            best = (area, hwnd)
        return 1

    user32.EnumWindows(proto(cb), 0)
    return best[1] if best else None


def _distance_to_rect(x: int, y: int, mon: MonitorInfo) -> float:
    """Distance from a point to a monitor rectangle; 0.0 when inside.

    Used to attribute a window to the nearest monitor when its center falls in a
    gap between monitors, which happens whenever the displays do not tile the
    virtual desktop.
    """
    dx = max(mon.left - x, 0, x - (mon.left + mon.width - 1))
    dy = max(mon.top - y, 0, y - (mon.top + mon.height - 1))
    return math.hypot(dx, dy)


class VisionEngine:
    """Handles multi-monitor frame acquisition, stabilization, and accessibility inspection."""

    def __init__(self) -> None:
        pass

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerates connected monitors through mss and platform metrics."""
        config.ensure_interactive_desktop()
        monitors: list[MonitorInfo] = []
        import mss

        with mss.MSS() as sct:
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
        show_grid: bool = False,
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
            with mss.MSS() as sct:
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
        """Overlays grid lines and coordinate markers without occluding the frame.

        The previous version painted two semi-opaque black bars -- the full width
        of the top 14px and the full height of the left 28px -- to give the
        labels contrast. That band is exactly where a window's title-bar controls
        live. A live inspection of the active window returned its only four
        elements at y=6: "Система" at [6, 6], and "Свернуть" / "Восстановить" /
        "Закрыть" at [1221, 6] / [1244, 6] / [1268, 6]. With the grid on, the
        agent could not see the close button of the window it was inspecting.

        Labels now carry a dark stroke instead of a backing bar, so they stay
        legible over any background while occluding only the glyph pixels.
        """
        w, h = image.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        label_fill = (0, 255, 255, 235)
        label_stroke = (0, 0, 0, 230)
        line_fill = (255, 255, 255, 40)
        tick_fill = (255, 255, 255, 170)

        # Vertical grid lines & labels every 100px, spanning the full height.
        # Labels are centered on their line rather than starting at line+2: a
        # left-aligned "1200" reaches x=1226 and lands squarely on the window
        # buttons that a real title bar puts at x=1221/1244/1268 on a 1280-wide
        # frame. Centering leaves the inter-line space, where controls live, clear.
        for x in range(100, w, 100):
            draw.line([(x, 0), (x, h)], fill=line_fill, width=1)
            label = str(x)
            draw.text(
                (x - draw.textlength(label) / 2, 1),
                label,
                fill=label_fill,
                stroke_width=2,
                stroke_fill=label_stroke,
            )

        # Horizontal grid lines & labels every 100px, spanning the full width
        for y in range(100, h, 100):
            draw.line([(0, y), (w, y)], fill=line_fill, width=1)
            draw.text(
                (2, y + 2), str(y), fill=label_fill,
                stroke_width=2, stroke_fill=label_stroke,
            )

        # 50px ticks kept to a 4px edge so they never reach real content
        for x in range(50, w, 50):
            if x % 100 != 0:
                draw.line([(x, 0), (x, 4)], fill=tick_fill, width=1)

        for y in range(50, h, 50):
            if y % 100 != 0:
                draw.line([(0, y), (4, y)], fill=tick_fill, width=1)

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

        # Cache monitors for hit testing. Index 0 is the mss pseudo-entry spanning
        # every display and is not a real monitor, so it is excluded from matching.
        monitors = self.list_monitors()
        physical = monitors[1:]

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
            matched_monitor: int | None = None
            for m in physical:
                if m.left <= mid_x < m.left + m.width and m.top <= mid_y < m.top + m.height:
                    matched_monitor = m.index
                    break

            # Monitors need not tile the virtual desktop. On this machine the
            # secondary occupies y in [351, 1431] at x < 0, so strips above and
            # below it belong to no monitor, and a window centered there matched
            # nothing. The old default of 0 was actively misleading: 0 is also
            # the legitimate index for "all displays", so the agent read an
            # unmatched window as one spanning every screen. Nearest monitor plus
            # an explicit flag is the honest answer.
            on_monitor = matched_monitor is not None
            if matched_monitor is None and physical:
                matched_monitor = min(
                    physical, key=lambda m: _distance_to_rect(mid_x, mid_y, m)
                ).index

            # Class and process identify a window unambiguously; a bare title
            # does not. AyuGram, for instance, has four top-level windows called
            # "AyuGramDesktop": the real one and three notification popups.
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            windows.append(
                WindowInfo(
                    handle=hwnd,
                    title=title,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    monitor_index=matched_monitor,
                    on_monitor=on_monitor,
                    is_active=(hwnd == active_hwnd),
                    class_name=cls_buf.value,
                    pid=int(pid.value),
                )
            )
            return 1

        enum_proc_proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(enum_proc_proto(_enum_proc), 0)

        # Sort so active window is first, followed by others
        windows.sort(key=lambda w: not w.is_active)
        return windows

    def inspect_ui_elements(
        self,
        window_title: str | None = None,
        monitor_index: int | None = None,
        hwnd: int | None = None,
        class_name: str | None = None,
    ) -> InspectionResult:
        """Inspects accessible controls with coordinates pre-scaled for the frame.

        Target resolution is explicit because a title alone does not identify a
        window. AyuGram has four top-level windows titled "AyuGramDesktop": the
        real one with ~1400 descendants and three notification popups with 0-1.
        Matching by title regex and taking the first hit lands on a popup, and
        the agent then reports that the application exposes nothing.

        Preference order: explicit ``hwnd``, then (title, class_name), then the
        foreground window. When a title matches several windows the largest by
        area wins -- popups are small -- and the ambiguity is reported so the
        caller can re-target by handle.

        Off-monitor elements are skipped and counted rather than published with
        a negative click_target: an element outside the captured frame has no
        valid coordinate in the image the agent is looking at.
        """
        if sys.platform != "win32":
            return InspectionResult([], 0, None, "UI automation requires Windows")

        # Pool thread; DPI awareness is per-thread. See list_windows.
        config.ensure_interactive_desktop()

        bbox, _ = self.get_monitor_bounds(monitor_index)
        scale_x = bbox["width"] / float(config.TARGET_IMAGE_WIDTH)
        scaled_h = int(round(bbox["height"] * (config.TARGET_IMAGE_WIDTH / float(bbox["width"]))))
        scale_y = bbox["height"] / float(scaled_h)

        elements: list[UIElementInfo] = []
        skipped_off_monitor = 0
        target: WindowTarget | None = None
        error: str | None = None

        try:
            from pywinauto import Desktop

            desktop = Desktop(backend="uia")
            target, _candidates, how = self._resolve_window(
                desktop, hwnd, window_title, class_name
            )
            if target is None:
                # Distinguish the causes. "The handle you gave me is dead" and
                # "nothing matched that title" need different follow-up actions,
                # and a single generic message sends the agent down both.
                if how == "hwnd-invalid":
                    message = (
                        f"Дескриптор окна {hwnd} недействителен (IsWindow вернул false) — "
                        "окно могло быть закрыто. Запросите list_windows_and_monitors "
                        "для получения актуальных дескрипторов."
                    )
                elif how == "none":
                    message = (
                        "Ни одно видимое окно не подошло под заданные критерии. "
                        "Запросите list_windows_and_monitors и передайте точный handle."
                    )
                else:
                    message = f"Не удалось определить целевое окно (причина: {how})."
                return InspectionResult([], 0, None, message)

            win = desktop.window(handle=target.handle)

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

                    elements.append(
                        UIElementInfo(
                            control_type=c_type,
                            title=ctrl.window_text().strip(),
                            automation_id=ctrl.automation_id(),
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
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"UI automation inspection encountered an issue: {exc}")

        return InspectionResult(elements, skipped_off_monitor, target, error)

    @staticmethod
    def _resolve_window(
        desktop: Any,
        hwnd: int | None,
        window_title: str | None,
        class_name: str | None,
    ) -> tuple[WindowTarget | None, int, str]:
        """Picks the window to inspect and says how it was picked.

        Returns the target, the number of candidates that matched, and a
        resolution label. Ambiguity is surfaced rather than silently resolved,
        because guessing wrong here produces an empty element list that looks
        identical to an application that exposes no accessibility tree.
        """
        user32 = ctypes.windll.user32

        if hwnd:
            if not user32.IsWindow(hwnd):
                return None, 0, "hwnd-invalid"
            return _target_from(desktop.window(handle=hwnd), "hwnd"), 1, "hwnd"

        if not window_title and not class_name:
            active = user32.GetForegroundWindow()
            if not active:
                return None, 0, "none"
            return _target_from(desktop.window(handle=active), "foreground"), 1, "foreground"

        criteria: dict[str, Any] = {"visible_only": True}
        if window_title:
            # Escape the caller's text: brackets, parentheses and trailing
            # backslashes in a title otherwise produce re.error or a wrong match.
            criteria["title_re"] = f".*{re.escape(window_title)}.*"
        if class_name:
            criteria["class_name"] = class_name

        matches = desktop.windows(**criteria)
        if not matches:
            # The Win32 title and the UIA Name are different strings for the same
            # window. list_windows_and_monitors reports Win32 titles while this
            # match is against UIA Name, so a title copied out of the window list
            # can legitimately match nothing here. Fall back to Win32 matching
            # before giving up.
            if window_title:
                handle = _find_hwnd_by_win32_title(window_title, class_name)
                if handle:
                    return (
                        _target_from(desktop.window(handle=handle), "win32-title"),
                        1,
                        "win32-title",
                    )
            return None, 0, "none"
        if len(matches) == 1:
            return _target_from(matches[0], "unique"), 1, "unique"

        def area(w) -> int:
            try:
                r = w.rectangle()
                return max(r.width(), 0) * max(r.height(), 0)
            except Exception:
                return 0

        best = max(matches, key=area)
        return _target_from(best, f"largest-of-{len(matches)}"), len(matches), f"largest-of-{len(matches)}"


