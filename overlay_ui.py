"""Transparent HUD overlay with hardware screenshot exclusion and visual indicators."""

import ctypes
import logging
import sys
import time
from ctypes import wintypes

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import QMainWindow, QWidget

import config

logger = logging.getLogger(__name__)

# Win32 Constants
GWL_EXSTYLE: int = -20
WS_EX_LAYERED: int = 0x00080000
WS_EX_TRANSPARENT: int = 0x00000020
WS_EX_TOPMOST: int = 0x00000008
WS_EX_TOOLWINDOW: int = 0x00000080
WS_EX_NOACTIVATE: int = 0x08000000
WDA_EXCLUDEFROMCAPTURE: int = 0x00000011


class HUDOverlayWindow(QMainWindow):
    """Full-screen pass-through overlay that is invisible to OS screenshot capture."""

    # Thread-safe signals to update HUD state from async loops or worker threads
    sig_set_state = pyqtSignal(str)
    sig_set_target_rect = pyqtSignal(int, int, int, int)
    sig_show_target = pyqtSignal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._state: config.AutopilotState = config.AutopilotState.IDLE
        self._target_rect: QRect | None = None

        # Display-topology tracking; see _connect_screen_signals
        self._app_signals_connected: bool = False
        self._tracked_screens: list = []

        # Predictive ripple animation state
        self._ripple_pos: QPoint | None = None
        self._ripple_start_time: float = 0.0
        self._ripple_active: bool = False
        self._ripple_timer: QTimer = QTimer(self)
        self._ripple_timer.setInterval(config.TARGET_RIPPLE_TICK_MS)
        self._ripple_timer.timeout.connect(self._on_ripple_tick)

        # Wire up signals
        self.sig_set_state.connect(self.set_state)
        self.sig_set_target_rect.connect(self._on_set_target_rect)
        self.sig_show_target.connect(self.show_target)

        self._init_window_flags()
        self._update_geometry()
        self._connect_screen_signals()

    def _connect_screen_signals(self) -> None:
        """Re-spans the window whenever display topology or geometry changes.

        Without this the overlay keeps the geometry it had at startup, so
        plugging in a monitor or changing a resolution leaves the HUD covering
        the wrong part of the desktop.

        Idempotent: safe to call again when a screen is added, so the new
        screen's own geometryChanged signal gets tracked too.
        """
        # screenAdded / screenRemoved are static signals: in PyQt6 they are
        # connectable only through the application instance, not the class.
        app = QGuiApplication.instance()
        if app is not None and not self._app_signals_connected:
            app.screenAdded.connect(self._on_screen_topology_changed)
            app.screenRemoved.connect(self._on_screen_topology_changed)
            self._app_signals_connected = True

        for screen in QGuiApplication.screens():
            if screen in self._tracked_screens:
                continue
            screen.geometryChanged.connect(self._on_screen_topology_changed)
            self._tracked_screens.append(screen)

    @pyqtSlot()
    def _on_screen_topology_changed(self, *_args) -> None:
        """Handles screenAdded / screenRemoved (QScreen arg) and geometryChanged (no arg)."""
        self._connect_screen_signals()
        self._update_geometry()
        self.update()

    def _init_window_flags(self) -> None:
        """Set Qt window flags and attributes for a click-through, always-on-top window."""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def _update_geometry(self) -> None:
        """Spans the entire virtual desktop covering all connected monitors.

        The active target rect is deliberately preserved: it is stored in global
        desktop coordinates, so it survives a topology change, and resetting it
        would silently drop the agent's chosen monitor back to "everything".
        """
        primary_screen = QGuiApplication.primaryScreen()
        fallback = QRect(0, 0, 1920, 1080)
        virtual_geo = primary_screen.virtualGeometry() if primary_screen else fallback

        self.setGeometry(virtual_geo)
        if self._target_rect is None:
            self._target_rect = QRect(virtual_geo)

    def _global_to_local_rect(self, rect: QRect) -> QRect:
        """Converts a desktop-coordinate rect into this window's paint space.

        The window sits at ``primaryScreen().virtualGeometry()``, whose top-left
        is not ``(0, 0)`` whenever a secondary monitor is arranged to the left of
        or above the primary. ``paintEvent`` draws in widget-local coordinates,
        so painting a desktop-coordinate rect directly offsets it by exactly that
        origin. On a two-monitor setup with the secondary on the left at
        ``x = -1920``, the frame for the primary landed 1920px too far left: it
        covered the whole secondary plus only the left 640px of the primary, which
        reads on screen as a frame of the wrong size with no right edge.
        """
        origin = self.geometry().topLeft()
        return QRect(
            rect.x() - origin.x(),
            rect.y() - origin.y(),
            rect.width(),
            rect.height(),
        )

    def _global_to_local_point(self, point: QPoint) -> QPoint:
        """Converts a desktop-coordinate point into this window's paint space."""
        origin = self.geometry().topLeft()
        return QPoint(point.x() - origin.x(), point.y() - origin.y())

    @property
    def target_rect_global(self) -> QRect | None:
        """The framed region, in global desktop coordinates."""
        return self._target_rect

    def diagnostics(self) -> dict:
        """Live geometry, so frame placement can be verified rather than assumed.

        WDA_EXCLUDEFROMCAPTURE keeps this window out of every screenshot, which
        is correct for capture but means a mis-placed frame cannot be seen in a
        captured frame. These numbers are the only machine-checkable evidence
        that the border is where it should be.
        """
        geom = self.geometry()
        target = self._target_rect
        local = self._global_to_local_rect(target) if target is not None else None
        return {
            "visible": self.isVisible(),
            "window": [geom.x(), geom.y(), geom.width(), geom.height()],
            "target_global": (
                [target.x(), target.y(), target.width(), target.height()]
                if target is not None
                else None
            ),
            "target_local": (
                [local.x(), local.y(), local.width(), local.height()]
                if local is not None
                else None
            ),
            "state": self._state.value,
        }

    def showEvent(self, event) -> None:
        """Apply OS-native capture exclusion and window styles upon showing."""
        super().showEvent(event)
        self._apply_os_native_flags()

    def _apply_os_native_flags(self) -> None:
        """Configures platform-specific window properties (Win32 / macOS)."""
        if sys.platform == "win32":
            self._apply_windows_flags()
        elif sys.platform == "darwin":
            self._apply_macos_flags()

    def _apply_windows_flags(self) -> None:
        """Applies WS_EX_TRANSPARENT and WDA_EXCLUDEFROMCAPTURE via ctypes."""
        hwnd = int(self.winId())
        if not hwnd:
            logger.warning("Invalid HWND for HUD overlay.")
            return

        user32 = ctypes.windll.user32

        # 64-bit / 32-bit compatibility for Get/SetWindowLong
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_window_long = user32.GetWindowLongPtrW
            set_window_long = user32.SetWindowLongPtrW
            get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long.restype = ctypes.c_ssize_t
            set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_window_long.restype = ctypes.c_ssize_t
        else:
            get_window_long = user32.GetWindowLongW
            set_window_long = user32.SetWindowLongW
            get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long.restype = wintypes.LONG
            set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
            set_window_long.restype = wintypes.LONG

        current_ex_style = get_window_long(hwnd, GWL_EXSTYLE)
        new_ex_style = (
            current_ex_style
            | WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        set_window_long(hwnd, GWL_EXSTYLE, new_ex_style)

        # Exclude window from screenshots (mss, OBS, PrintScreen)
        set_display_affinity = getattr(user32, "SetWindowDisplayAffinity", None)
        if set_display_affinity:
            set_display_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            set_display_affinity.restype = wintypes.BOOL
            res = set_display_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if not res:
                error_code = ctypes.windll.kernel32.GetLastError()
                logger.warning(
                    f"SetWindowDisplayAffinity failed with error code {error_code}. "
                    "Window might be visible on older Windows builds."
                )
            else:
                logger.info("WDA_EXCLUDEFROMCAPTURE successfully applied to HUD overlay.")

    def _apply_macos_flags(self) -> None:
        """Applies NSWindowSharingNone and mouse pass-through on macOS."""
        try:
            import objc

            view_ptr = int(self.winId())
            ns_view = objc.objc_object(c_void_p=view_ptr)
            ns_window = ns_view.window()
            if ns_window:
                ns_window.setIgnoresMouseEvents_(True)
                # 0 = NSWindowSharingNone (excludes window from screen capture)
                ns_window.setSharingType_(0)
                logger.info("NSWindowSharingNone and mouse pass-through applied on macOS.")
        except Exception as exc:
            logger.warning(f"Failed to apply macOS native window flags: {exc}")

    @pyqtSlot(str)
    def set_state(self, state: str) -> None:
        """Changes the overlay state and triggers repaint."""
        try:
            self._state = config.AutopilotState(state)
        except ValueError:
            self._state = config.AutopilotState.IDLE
        self.update()

    @pyqtSlot(int, int, int, int)
    def _on_set_target_rect(self, x: int, y: int, width: int, height: int) -> None:
        """Sets the active rectangle boundary to frame on screen."""
        self._target_rect = QRect(x, y, width, height)
        self.update()

    def set_target_geometry(self, x: int, y: int, width: int, height: int) -> None:
        """Public thread-safe caller for setting frame geometry."""
        self.sig_set_target_rect.emit(x, y, width, height)

    @pyqtSlot(int, int)
    def show_target(self, x: int, y: int) -> None:
        """Initiates predictive ripple animation at point (x, y)."""
        self._ripple_pos = QPoint(x, y)
        self._ripple_start_time = time.monotonic()
        self._ripple_active = True
        self._ripple_timer.start()
        self.update()

    def _on_ripple_tick(self) -> None:
        """Advances ripple animation frames."""
        if not self._ripple_active:
            self._ripple_timer.stop()
            return

        elapsed_ms = (time.monotonic() - self._ripple_start_time) * 1000.0
        if elapsed_ms >= config.TARGET_RIPPLE_DURATION_MS:
            self._ripple_active = False
            self._ripple_timer.stop()

        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Renders glowing HUD border and active target ripple.

        Both inputs are stored in global desktop coordinates and converted here.
        Converting at paint time rather than at store time means a display
        topology change cannot leave stale widget-local values behind.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Draw glowing perimeter border based on state
        if self._state != config.AutopilotState.IDLE and self._target_rect:
            self._draw_neon_border(
                painter, self._global_to_local_rect(self._target_rect), self._state
            )

        # 2. Draw predictive ripple target if active
        if self._ripple_active and self._ripple_pos:
            self._draw_ripple_marker(
                painter, self._global_to_local_point(self._ripple_pos)
            )

        painter.end()

    def _draw_neon_border(
        self, painter: QPainter, rect: QRect, state: config.AutopilotState
    ) -> None:
        """Draws soft gradient glow along the inside edge of rect."""
        width = config.HUD_BORDER_WIDTH
        glow_radius = config.HUD_GLOW_RADIUS

        is_override = state == config.AutopilotState.USER_OVERRIDE

        for step in range(width + glow_radius):
            current_rect = rect.adjusted(step, step, -step, -step)
            if current_rect.width() <= 0 or current_rect.height() <= 0:
                break

            ratio = step / float(width + glow_radius)
            decay = max(0.0, 1.0 - (ratio**0.75))

            if is_override:
                # Amber / Gold pulse
                c = config.COLOR_USER_OVERRIDE
                alpha = int(c.a * decay * 0.7)
                pen_color = QColor(c.r, c.g, c.b, alpha)
                pen = QPen(pen_color, 1.5)
            else:
                # Electric Cyan to Violet diagonal gradient
                grad = QLinearGradient(
                    current_rect.topLeft().toPointF(),
                    current_rect.bottomRight().toPointF(),
                )
                c1 = config.COLOR_AI_ACTIVE_PRIMARY
                c2 = config.COLOR_AI_ACTIVE_SECONDARY
                grad.setColorAt(0.0, QColor(c1.r, c1.g, c1.b, int(c1.a * decay * 0.85)))
                grad.setColorAt(1.0, QColor(c2.r, c2.g, c2.b, int(c2.a * decay * 0.85)))
                pen = QPen(QBrush(grad), 1.5)

            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(current_rect)

    def _draw_ripple_marker(self, painter: QPainter, pos: QPoint) -> None:
        """Renders dynamic reticle with expanding circles at point pos."""
        elapsed_ms = (time.monotonic() - self._ripple_start_time) * 1000.0
        progress = min(1.0, max(0.0, elapsed_ms / config.TARGET_RIPPLE_DURATION_MS))
        alpha = int(240 * (1.0 - progress))

        radius = int(progress * config.TARGET_RIPPLE_MAX_RADIUS)
        inner_radius = int(radius * 0.45)

        # Expanding outer circle
        outer_pen = QPen(QColor(58, 134, 255, alpha), 2.0)
        painter.setPen(outer_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(pos, radius, radius)

        # Concentric inner ring
        if inner_radius > 2:
            inner_pen = QPen(QColor(123, 44, 191, int(alpha * 0.9)), 1.5)
            painter.setPen(inner_pen)
            painter.drawEllipse(pos, inner_radius, inner_radius)

        # Center target dot
        dot_pen = QPen(QColor(255, 255, 255, alpha), 1.0)
        dot_brush = QBrush(QColor(255, 50, 100, alpha))
        painter.setPen(dot_pen)
        painter.setBrush(dot_brush)
        painter.drawEllipse(pos, 4, 4)

        # Crosshair lines
        line_len = 10
        painter.setPen(QPen(QColor(255, 255, 255, int(alpha * 0.7)), 1.0))
        painter.drawLine(pos.x() - line_len, pos.y(), pos.x() - 4, pos.y())
        painter.drawLine(pos.x() + 4, pos.y(), pos.x() + line_len, pos.y())
        painter.drawLine(pos.x(), pos.y() - line_len, pos.x(), pos.y() - 4)
        painter.drawLine(pos.x(), pos.y() + 4, pos.x(), pos.y() + line_len)

