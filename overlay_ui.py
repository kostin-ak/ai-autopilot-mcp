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
    QPixmap,
)
from PyQt6.QtWidgets import QMainWindow, QWidget

try:
    # Always present under PyQt6. Imported defensively so a stand-in screen object in a
    # test -- which is not a Qt wrapper at all -- does not make the module unimportable.
    from PyQt6 import sip as _sip
except ImportError:  # pragma: no cover - not reachable under PyQt6
    _sip = None

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

# Extra margin around the ripple's maximum radius when computing the dirty rectangle.
# Covers pen width and antialiasing bleed; being generous costs a few pixels of repaint,
# while being short leaves a permanent artefact that cannot be seen in any screenshot.
_RIPPLE_DIRTY_MARGIN_PX: int = 8


class HUDOverlayWindow(QMainWindow):
    """Full-screen pass-through overlay that is invisible to OS screenshot capture."""

    # Thread-safe signals to update HUD state from async loops or worker threads
    sig_set_state = pyqtSignal(str)
    sig_set_target_rect = pyqtSignal(int, int, int, int)
    sig_show_target = pyqtSignal(int, int)
    sig_flash_action = pyqtSignal(int, int, int, int, str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._state: config.AutopilotState = config.AutopilotState.IDLE
        self._target_rect: QRect | None = None

        # Display-topology tracking; see _connect_screen_signals
        self._app_signals_connected: bool = False
        self._tracked_screens: list = []

        # Cached border image: (key, pixmap, origin). See _draw_neon_border.
        self._border_cache: tuple | None = None

        # Predictive ripple animation state
        self._ripple_pos: QPoint | None = None
        # Previous ripple position, so a new ripple starting elsewhere still clears the
        # region the last one painted. Without it the tail of a faded marker survives.
        self._last_ripple_pos: QPoint | None = None
        self._ripple_start_time: float = 0.0
        self._ripple_active: bool = False
        self._ripple_timer: QTimer = QTimer(self)
        self._ripple_timer.setInterval(config.TARGET_RIPPLE_TICK_MS)
        self._ripple_timer.timeout.connect(self._on_ripple_tick)

        # Action highlight flashes. See flash_action for why they are a list with one
        # shared timer rather than one animation object per flash.
        self._flashes: list[dict] = []
        self._flash_total: int = 0
        self._flash_timer: QTimer = QTimer(self)
        self._flash_timer.setInterval(config.ACTION_HIGHLIGHT_TICK_MS)
        self._flash_timer.timeout.connect(self._on_flash_tick)

        # Wire up signals
        self.sig_set_state.connect(self.set_state)
        self.sig_set_target_rect.connect(self._on_set_target_rect)
        self.sig_show_target.connect(self.show_target)
        self.sig_flash_action.connect(self.flash_action)

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
        self._prune_dead_screens()

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

    def _prune_dead_screens(self) -> None:
        """Drops tracked screens whose C++ object Qt has already deleted.

        This is not tidiness. When a monitor is unplugged Qt destroys the QScreen, but
        the Python wrapper stays in the list, and the next `screen in self._tracked_screens`
        comparison touches the deleted object and raises "Internal C++ object already
        deleted". The crash lands in the handler for screenRemoved -- so the feature
        fails in exactly the scenario it was written for, and it fails on the second
        topology change rather than the first, which is what makes it look intermittent.
        """
        alive = []
        for screen in self._tracked_screens:
            try:
                if _sip.isdeleted(screen):
                    continue
            except (AttributeError, NameError, RuntimeError, TypeError):
                # sip is always present under PyQt6, but a stand-in object in a test is
                # not a Qt wrapper at all. Treating an unknown object as alive is the
                # safe direction: keeping a live screen tracked matters more than
                # pruning a dead one, and a stale entry costs a redundant signal.
                pass
            alive.append(screen)
        self._tracked_screens = alive

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
        # The window moved, so every widget-local coordinate in the cache is now wrong
        # even though the key looks unchanged.
        self._invalidate_border_cache()

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
        # Through the property, so the accessor has a caller. Reading the private
        # attribute here left target_rect_global as the only caller-less member of
        # this class's public surface.
        target = self.target_rect_global
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
            # Machine-checkable evidence that action highlights fired: like the HUD
            # frame, the flashes themselves appear in no capture.
            "active_flashes": len(self._flashes),
            "flashes_total": self._flash_total,
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

    @pyqtSlot(int, int, int, int, str, str)
    def flash_action(self, x: int, y: int, width: int, height: int,
                     level: str, action: str) -> None:
        """Flashes a rectangle around an element or window an action just hit.

        The highlight is how a Class B action becomes visible. Pattern actions never
        move the cursor, so the ripple cannot mark them; this marks the rect the action
        actually touched instead. Colour comes from risk classification (ADR-0004).

        One list with a shared timer, not one QTimer per flash: a batch dispatches
        several steps in quick succession, and a timer per step would mean a growing
        number of independent paint callbacks competing for the GUI thread. The list is
        bounded so a long batch cannot accumulate unbounded paint regions.

        The rect is global desktop coordinates and is converted at paint time, exactly
        like the HUD frame and ripple, so a topology change mid-flash cannot misplace it.
        """
        if not config.ACTION_HIGHLIGHTS:
            return
        if width <= 0 or height <= 0:
            # Degenerate rects (zero-sized elements) have nothing to mark.
            return
        if len(self._flashes) >= config.ACTION_HIGHLIGHT_MAX_CONCURRENT:
            dropped = self._flashes.pop(0)
            self.update(self._flash_dirty_rect(dropped["rect"]))
        self._flashes.append({
            "rect": QRect(x, y, width, height),
            "level": str(level),
            "action": str(action),
            "start": time.monotonic(),
        })
        self._flash_total += 1
        self._flash_timer.start()
        self.update(self._flash_dirty_rect(QRect(x, y, width, height)))

    def _flash_dirty_rect(self, global_rect: QRect) -> QRect:
        """Widget-local rect a flash can paint into, including halo and label.

        Deliberately generous: it bounds the outer halo growth, the glow pens and the
        risk label chip drawn above the rect. The label is the reason the margin is
        larger than the halo alone. Like the ripple, an undersized dirty rect leaves a
        permanent ghost that no screenshot can reveal, since the overlay is excluded
        from capture.
        """
        m = config.ACTION_HIGHLIGHT_MAX_GROW_PX + config.ACTION_HIGHLIGHT_DIRTY_MARGIN_PX
        local = self._global_to_local_rect(global_rect)
        return local.adjusted(-m, -m, m, m).intersected(self.rect())

    def _on_flash_tick(self) -> None:
        """Advances every active flash and clears expired ones."""
        if not self._flashes:
            self._flash_timer.stop()
            return
        now = time.monotonic()
        duration_s = config.ACTION_HIGHLIGHT_DURATION_MS / 1000.0
        keep: list[dict] = []
        # Expired flashes must repaint their region one last time, or their last visible
        # frame stays on screen forever. Collect dirty rects before dropping them.
        for flash in self._flashes:
            self.update(self._flash_dirty_rect(flash["rect"]))
            if now - flash["start"] < duration_s:
                keep.append(flash)
        self._flashes = keep
        if not keep:
            self._flash_timer.stop()

    def _draw_action_flashes(self, painter: QPainter) -> None:
        """Renders all active action flashes."""
        if not self._flashes:
            return
        duration = config.ACTION_HIGHLIGHT_DURATION_MS
        for flash in self._flashes:
            elapsed_ms = (time.monotonic() - flash["start"]) * 1000.0
            progress = min(1.0, max(0.0, elapsed_ms / duration))
            self._draw_action_flash(
                painter,
                self._global_to_local_rect(flash["rect"]),
                flash["level"], flash["action"], progress,
            )

    def _draw_action_flash(self, painter: QPainter, rect: QRect,
                           level: str, action: str, progress: float) -> None:
        """One glowing, labelled flash, coloured by risk level.

        Visual language, in order of what the eye catches first at desktop scale:

        1. A translucent fill and a bright outline around the target rect. The outline is
           the precise boundary; the fill makes it a region rather than a hairline.
        2. A halo that expands from the rect over the animation. Motion is what makes
           a sub-second mark noticeable when the operator is not already looking at it.
        3. Corner brackets at full alpha for most of the animation. They hold the shape
           after the halo has grown away from the rect, so the target stays identifiable.
        4. A small risk chip above the top-left corner, drawn inside when there is no
           room above. It says which verb fired and how reversible it is -- the fact the
           operator most needs when something they did not click happens on screen.

        Alpha follows the ripple's rule: hold at full, then fade. A linear fade from the
        first frame would make the most visible phase the faintest.
        """
        color = config.ACTION_HIGHLIGHT_COLORS.get(
            level, config.ACTION_HIGHLIGHT_DEFAULT_COLOR
        )
        fade_from = max(0.0, 1.0 - config.ACTION_HIGHLIGHT_FADE_FRACTION)
        if progress <= fade_from:
            fade = 1.0
        else:
            fade = 1.0 - (progress - fade_from) / max(1e-6, 1.0 - fade_from)
        fade = max(0.0, min(1.0, fade))
        r, g, b = color.r, color.g, color.b

        # Expanding bloom: three nested outlines with decreasing width and alpha read
        # as one soft glow rather than a hard band. The outermost is deliberately
        # faint; the innermost is the precise edge.
        grow = int(config.ACTION_HIGHLIGHT_MAX_GROW_PX * progress)
        for pad, width, alpha in ((grow + 9, 12.0, 26), (grow + 4, 6.0, 58),
                                  (grow, 2.5, 104)):
            halo = rect.adjusted(-pad, -pad, pad, pad)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(r, g, b, int(alpha * fade)), width))
            painter.drawRoundedRect(halo, 10, 10)

        # Frosted fill: brighter at the top edge, fading downward so the target
        # remains readable through it. A flat fill looked like a selection rectangle.
        fill = QLinearGradient(rect.topLeft().toPointF(), rect.bottomLeft().toPointF())
        fill.setColorAt(0.0, QColor(r, g, b, int(48 * fade)))
        fill.setColorAt(1.0, QColor(r, g, b, int(14 * fade)))
        painter.setBrush(QBrush(fill))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 7, 7)

        # Gradient outline: a lighter leading edge and the risk colour trailing, so
        # the frame has depth instead of reading as one flat stroke.
        lead = QColor(min(255, r + 60), min(255, g + 60), min(255, b + 60),
                      int(245 * fade))
        trail = QColor(r, g, b, int(225 * fade))
        outline = QLinearGradient(rect.topLeft().toPointF(), rect.bottomRight().toPointF())
        outline.setColorAt(0.0, lead)
        outline.setColorAt(1.0, trail)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QBrush(outline), 2.8))
        painter.drawRoundedRect(rect, 7, 7)

        # A slow sheen travelling down the target. Low alpha: it is a finishing cue,
        # not a cover. Clipped to the rect so it cannot paint outside the flash.
        sheen_y = rect.top() + int(rect.height() * min(1.0, progress * 1.15))
        sheen = QRect(rect.left() + 2, sheen_y, max(0, rect.width() - 4), 2)
        if sheen.width() > 0:
            painter.fillRect(sheen, QColor(255, 255, 255, int(20 * fade)))

        # Corner brackets at nearly full alpha.
        arm = max(10, min(30, int(min(rect.width(), rect.height()) * 0.25)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(r, g, b, int(250 * fade)), 3.2))
        left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
        for x, sx in ((left, 1), (right, -1)):
            for y, sy in ((top, 1), (bottom, -1)):
                painter.drawLine(x, y, x + sx * arm, y)
                painter.drawLine(x, y, x, y + sy * arm)

        # Risk/verb chip.
        level_ru = {
            "reversible": "обратимо",
            "medium": "осторожно",
            "irreversible": "необратимо",
        }.get(level, level)
        label = f"{action} · {level_ru}" if action else level_ru
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        chip_w = metrics.horizontalAdvance(label) + 26
        chip_h = metrics.height() + 8
        chip_x = rect.left() + 2
        chip_y = rect.top() - chip_h - 4
        if chip_y < self.rect().top() + 2:
            chip_y = rect.top() + 4
        chip = QRect(chip_x, chip_y, chip_w, chip_h)
        shadow = QRect(chip).translated(2, 2)
        painter.setBrush(QBrush(QColor(0, 0, 0, int(110 * fade))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(shadow, 6, 6)
        chip_fill = QLinearGradient(chip.topLeft().toPointF(),
                                    chip.bottomLeft().toPointF())
        chip_fill.setColorAt(0.0, QColor(28, 31, 42, int(235 * fade)))
        chip_fill.setColorAt(1.0, QColor(11, 13, 20, int(225 * fade)))
        painter.setBrush(QBrush(chip_fill))
        painter.setPen(QPen(QColor(r, g, b, int(210 * fade)), 1.2))
        painter.drawRoundedRect(chip, 6, 6)
        # Coloured status dot: the risk level is legible even before the text is read.
        painter.setBrush(QBrush(QColor(r, g, b, int(255 * fade))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPoint(chip.left() + 9, chip.center().y()), 3, 3)
        painter.setPen(QColor(245, 246, 252, int(245 * fade)))
        painter.drawText(chip.adjusted(17, 0, -3, 0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

    def _on_ripple_tick(self) -> None:
        """Advances ripple animation frames."""
        if not self._ripple_active:
            self._ripple_timer.stop()
            return

        elapsed_ms = (time.monotonic() - self._ripple_start_time) * 1000.0
        if elapsed_ms >= config.TARGET_RIPPLE_DURATION_MS:
            self._ripple_active = False
            self._ripple_timer.stop()

        # Repaint only the ripple's neighbourhood rather than the whole window.
        #
        # The timer ticks every TARGET_RIPPLE_TICK_MS (~15 ms), and self.update() with no
        # argument marks the entire overlay dirty -- and the overlay spans the whole
        # virtual desktop, 4480x1440 on this machine. Each such repaint re-ran the neon
        # border loop, which builds a fresh QLinearGradient per step for width+glow
        # steps, so one ripple animation cost hundreds of gradient constructions to
        # redraw a border that had not changed.
        #
        # This is safe because the ripple does not move: _ripple_pos is fixed for the
        # whole animation and only the radius grows, from 0 to TARGET_RIPPLE_MAX_RADIUS.
        # A square of that radius around the position therefore bounds every frame the
        # animation can draw, and the union with the previous position covers a second
        # ripple starting elsewhere while an earlier one is still on screen.
        self.update(self._ripple_dirty_rect())

    def _ripple_dirty_rect(self) -> QRect:
        """Widget-local region the ripple can have painted, this tick or the last.

        Deliberately generous. An undersized rect leaves the tail of a faded marker on
        screen as a permanent artefact, while an oversized one costs a few extra pixels
        of repaint. Note this cannot be checked by looking at a screenshot: the overlay
        carries WDA_EXCLUDEFROMCAPTURE, so it appears in no capture at all.
        """
        margin = config.TARGET_RIPPLE_MAX_RADIUS + _RIPPLE_DIRTY_MARGIN_PX
        rects = []
        for pos in (self._ripple_pos, self._last_ripple_pos):
            if pos is None:
                continue
            local = self._global_to_local_point(pos)
            rects.append(QRect(local.x() - margin, local.y() - margin,
                               margin * 2, margin * 2))
        self._last_ripple_pos = self._ripple_pos
        if not rects:
            return QRect()
        region = rects[0]
        for r in rects[1:]:
            region = region.united(r)
        return region

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

        # 2. Draw action highlight flashes (Class B and any other action rect)
        self._draw_action_flashes(painter)

        # 3. Draw predictive ripple target if active
        if self._ripple_active and self._ripple_pos:
            self._draw_ripple_marker(
                painter, self._global_to_local_point(self._ripple_pos)
            )

        painter.end()

    def _draw_neon_border(
        self, painter: QPainter, rect: QRect, state: config.AutopilotState
    ) -> None:
        """Draws soft gradient glow along the inside edge of rect, from a cache.

        The border is static between state changes, but paintEvent runs on every ripple
        tick -- about 66 times a second -- and the naive version rebuilt a QLinearGradient
        for each of ``width + glow_radius`` steps on every one of those paints. Nothing in
        that work depended on anything that had changed.

        It is rendered once into a QPixmap and blitted thereafter. The pixmap covers only
        the glow band around the target rect, not the whole virtual desktop: the border is
        a hollow frame, so the interior would be transparent pixels held for nothing, and
        at 4480x1440 a full-surface cache would cost about 26 MB to store a thin rectangle.

        Cache key is the rect and the state, both in widget-local coordinates. Anything
        that can change the pixels -- a state transition, a new target rect, a topology
        change that moves the window origin, a device-pixel-ratio change -- invalidates
        through _invalidate_border_cache rather than being reasoned about here.
        """
        cached = self._border_cache
        key = (rect.x(), rect.y(), rect.width(), rect.height(), state.value,
               self.devicePixelRatioF())
        if cached is not None and cached[0] == key:
            painter.drawPixmap(cached[2], cached[1])
            return

        band = config.HUD_BORDER_WIDTH + config.HUD_GLOW_RADIUS
        # Origin of the cached image in widget coordinates, and the translation that maps
        # the caller's rect into the pixmap's own coordinate space. Keeping the drawing
        # code in the caller's coordinates is what lets the gradient endpoints stay
        # correct: a gradient built from current_rect's corners means the same thing
        # before and after caching, rather than needing to be rebuilt around a new origin.
        origin = QPoint(rect.x() - band, rect.y() - band)
        pm = QPixmap((rect.width() + 2 * band), (rect.height() + 2 * band))
        dpr = self.devicePixelRatioF()
        if dpr and dpr != 1.0:
            pm = QPixmap(int(pm.width() * dpr), int(pm.height() * dpr))
            pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)

        cache_painter = QPainter(pm)
        try:
            cache_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            cache_painter.translate(-origin.x(), -origin.y())
            self._paint_border_into(cache_painter, rect, state)
        finally:
            cache_painter.end()

        self._border_cache = (key, pm, origin)
        painter.drawPixmap(origin, pm)

    def _paint_border_into(self, painter: QPainter, rect: QRect,
                           state: config.AutopilotState) -> None:
        """The border itself, drawn into whatever painter is handed in.

        Split out from the cache so the drawing is not duplicated between the cached and
        uncached paths. Two copies of a rendering loop is how a cache starts returning
        pixels that differ from what the direct path would have drawn -- and that
        difference is invisible in every test that does not compare the two.
        """
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

    def _invalidate_border_cache(self) -> None:
        """Drops the cached border image.

        Most invalidation happens through the key itself: it carries the rect, the state
        and the device pixel ratio, so a state transition, a new target rect, or a window
        move that shifts the rect's local coordinates all miss the cache on their own.

        This explicit drop covers what the key cannot see -- a colour changed in config at
        runtime, and a geometry recomputation. Missing one of those would leave a stale
        frame drawing the previous colour, and because the overlay carries
        WDA_EXCLUDEFROMCAPTURE the stale frame appears in no screenshot, so nothing in the
        tool's own output would reveal it. get_hud_state's placement_correct flag covers
        position; nothing but this covers colour.
        """
        self._border_cache = None

    def _draw_ripple_marker(self, painter: QPainter, pos: QPoint) -> None:
        """Renders the predictive target marker at pos.

        Three changes from the original, all because the marker was correct and
        unseeable. Measured live 2026-11-06: a 36 px radius ring over 250 ms, with alpha
        ramping linearly to 12 of 240 at full size, so the largest frames were the
        faintest. The operator watching the screen saw nothing.

        - Alpha holds at full for the first part of the animation and fades only over the
          last TARGET_RIPPLE_FADE_FRACTION. Growing while bright is what makes an
          expanding ring readable as motion.
        - A translucent filled disc, not only stroked rings. A 3 px line at 84 px radius
          is about 500 pixels of ink; the disc gives it area, which is what the eye
          actually catches in a quarter of a second.
        - Thicker strokes and a larger centre dot, because at desktop scale the originals
          were sub-pixel on a non-primary monitor.
        """
        elapsed_ms = (time.monotonic() - self._ripple_start_time) * 1000.0
        progress = min(1.0, max(0.0, elapsed_ms / config.TARGET_RIPPLE_DURATION_MS))

        fade_from = max(0.0, 1.0 - config.TARGET_RIPPLE_FADE_FRACTION)
        if progress <= fade_from:
            fade = 1.0
        else:
            fade = 1.0 - (progress - fade_from) / max(1e-6, 1.0 - fade_from)
        fade = max(0.0, min(1.0, fade))

        radius = int(progress * config.TARGET_RIPPLE_MAX_RADIUS)
        inner_radius = int(radius * 0.45)

        base_alpha = int(235 * fade)

        # Filled disc: low alpha so it reads as a glow rather than hiding what is under
        # it. The overlay is click-through, but obscuring the target the agent is about
        # to click would defeat the purpose of marking it.
        if radius > 1:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(58, 134, 255, int(46 * fade))))
            painter.drawEllipse(pos, radius, radius)

        # Expanding outer ring
        painter.setPen(QPen(QColor(58, 134, 255, base_alpha), 3.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(pos, radius, radius)

        # Concentric inner ring
        if inner_radius > 2:
            painter.setPen(QPen(QColor(123, 44, 191, int(base_alpha * 0.95)), 2.5))
            painter.drawEllipse(pos, inner_radius, inner_radius)

        # Centre dot stays legible for the whole animation rather than fading with the
        # rings: it marks the point, and the point is still the point at the end.
        dot_alpha = max(base_alpha, int(235 * (1.0 - progress * 0.5)))
        painter.setPen(QPen(QColor(255, 255, 255, dot_alpha), 1.5))
        painter.setBrush(QBrush(QColor(255, 50, 100, dot_alpha)))
        painter.drawEllipse(pos, 6, 6)

        # Crosshair lines. Scaled with the rest of the marker: at the old 10 px arm on a
        # 36 px ring the arms were shorter than the dot is now, and the whole reticle
        # read as a speck at desktop scale.
        line_len = max(14, int(config.TARGET_RIPPLE_MAX_RADIUS * 0.22))
        gap = 8
        painter.setPen(QPen(QColor(255, 255, 255, int(dot_alpha * 0.85)), 2.0))
        painter.drawLine(pos.x() - line_len, pos.y(), pos.x() - gap, pos.y())
        painter.drawLine(pos.x() + gap, pos.y(), pos.x() + line_len, pos.y())
        painter.drawLine(pos.x(), pos.y() - line_len, pos.x(), pos.y() - gap)
        painter.drawLine(pos.x(), pos.y() + gap, pos.x(), pos.y() + line_len)
