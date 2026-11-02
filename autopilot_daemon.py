"""Autopilot Daemon: Combines PyQt6 HUD overlay, low-level hooks, and asyncio WebSocket server."""

import contextlib
import sys

if sys.platform == "win32":
    import ctypes
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

import asyncio
import contextlib
import json
import logging
import secrets
import sys
from typing import Any

import qasync
import websockets
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from websockets.asyncio.server import ServerConnection

import config
from config import AutopilotState
from input_controller import InputController, InterruptedByUserError
from overlay_ui import HUDOverlayWindow
from vision_engine import ScreenCaptureError, VisionEngine

# Must run before logging.basicConfig: StreamHandler captures sys.stderr at
# construction time, so the encoding has to be correct first.
config.configure_stdio_encoding()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
)
logger = logging.getLogger("AutopilotDaemon")

# Actions that never touch the session input queue and may therefore run
# concurrently with an in-flight input action.
READ_ONLY_ACTIONS: frozenset[str] = frozenset({
    "get_screen_state",
    "list_windows_and_monitors",
    "get_ui_elements",
    "reset_override",
})


class AutopilotDaemon:
    """Core daemon managing GUI overlay, input executor, and local WebSocket bridge."""

    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.auth_token = self._init_auth_token()

        self.vision = VisionEngine()
        self.overlay = HUDOverlayWindow()
        self.input_ctrl = InputController(on_state_change=self._on_input_state_changed)

        # Serializes every action that can touch the input queue. The host is free
        # to issue tool calls in parallel; without this, two worker threads end up
        # driving the same cursor at the same time.
        self._input_lock = asyncio.Lock()

        self._active_monitor_index: int | None = None
        self._init_tray_icon()
        self.overlay.show()

    def _init_auth_token(self) -> str:
        """Generates session token and writes it to disk for the local MCP client."""
        token = secrets.token_hex(24)
        try:
            config.TOKEN_FILE_PATH.write_text(token, encoding="utf-8")
            logger.info(f"Session token generated at {config.TOKEN_FILE_PATH}")
        except Exception as exc:
            logger.warning(f"Could not write auth token file: {exc}")
        return token

    def _create_tray_pixmap(self, color_name: str) -> QPixmap:
        """Generates a color dot pixmap for the system tray icon."""
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(color_name))
        painter.setPen(QColor(0, 0, 0, 160))
        painter.drawEllipse(1, 1, 13, 13)
        painter.end()
        return pixmap

    def _init_tray_icon(self) -> None:
        """Initializes system tray icon with quick actions and override reset."""
        self.tray = QSystemTrayIcon(self.overlay)
        self.tray.setIcon(QIcon(self._create_tray_pixmap("#3A86FF")))

        menu = QMenu()
        self.action_status = QAction("Статус: IDLE", menu)
        self.action_status.setEnabled(False)
        menu.addAction(self.action_status)

        action_reset = QAction("Сбросить паузу (Ctrl+Alt+Space)", menu)
        action_reset.triggered.connect(self._reset_override)
        menu.addAction(action_reset)

        menu.addSeparator()
        action_quit = QAction("Завершить работу", menu)
        action_quit.triggered.connect(self.shutdown)
        menu.addAction(action_quit)

        self.tray.setContextMenu(menu)
        self.tray.show()

    def _reset_override(self) -> None:
        """Resets user override state."""
        self.input_ctrl.reset_override()

    def _on_input_state_changed(self, new_state: AutopilotState) -> None:
        """Synchronizes GUI overlay and tray icon with input controller state."""
        self.overlay.sig_set_state.emit(new_state.value)

        status_text = f"Статус: {new_state.value}"
        self.action_status.setText(status_text)

        if new_state == AutopilotState.USER_OVERRIDE:
            self.tray.setIcon(QIcon(self._create_tray_pixmap("#FFB703")))
            self.tray.showMessage(
                "AI Autopilot приостановлен",
                "Обнаружен физический ввод пользователя. Нажмите Ctrl+Alt+Space для возобновления.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
        elif new_state == AutopilotState.AI_ACTIVE:
            self.tray.setIcon(QIcon(self._create_tray_pixmap("#7B2CBF")))
        else:
            self.tray.setIcon(QIcon(self._create_tray_pixmap("#3A86FF")))

    def _update_overlay_target(self, monitor_index: int | None) -> tuple[int, int, float, float]:
        """Aligns HUD frame geometry with target monitor bounds and computes coordinate scale."""
        bbox, target_mon = self.vision.get_monitor_bounds(monitor_index)
        self.overlay.sig_set_target_rect.emit(
            bbox["left"], bbox["top"], bbox["width"], bbox["height"]
        )

        scaled_w = config.TARGET_IMAGE_WIDTH
        scaled_h = int(round(bbox["height"] * (scaled_w / float(bbox["width"]))))
        scale_x = bbox["width"] / float(scaled_w)
        scale_y = bbox["height"] / float(scaled_h)

        return bbox["left"], bbox["top"], scale_x, scale_y

    async def handle_client(self, websocket: ServerConnection) -> None:
        """Handles incoming commands from the MCP gateway over WebSocket."""
        logger.info(f"Client connected from {websocket.remote_address}")

        try:
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                except Exception:
                    await websocket.send(
                        json.dumps({"status": "error", "message": "Malformed JSON payload"})
                    )
                    continue

                req_id = payload.get("id", "none")
                token = payload.get("token")
                action = payload.get("action")
                params = payload.get("params", {})

                # Validate session token
                if token != self.auth_token:
                    await websocket.send(
                        json.dumps({
                            "id": req_id,
                            "status": "unauthorized",
                            "message": "Invalid authentication token.",
                        })
                    )
                    continue

                response = await self.dispatch_action(action, params)
                response["id"] = req_id
                await websocket.send(json.dumps(response))

        except websockets.exceptions.ConnectionClosed:
            logger.info("Client disconnected.")
        except Exception as exc:
            logger.error(f"Unexpected connection handler error: {exc}")

    async def dispatch_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serializes input-queue actions, then dispatches.

        Read-only actions bypass the lock: a host may legitimately want a fresh
        screenshot or a tree inspection while a click is still in flight, and
        blocking that would make the observer blind exactly when it matters.
        """
        if action in READ_ONLY_ACTIONS:
            return await self._dispatch(action, params)
        async with self._input_lock:
            return await self._dispatch(action, params)

    async def _dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatches and executes requested tool action."""
        current_state = self.input_ctrl.state

        # Immediate return if user override is active. Read-only actions stay
        # available so the agent can still observe and re-plan while paused.
        if current_state == AutopilotState.USER_OVERRIDE and action not in READ_ONLY_ACTIONS:
            return {
                "status": "interrupted",
                "state": current_state.value,
                "message": (
                    "PAUSE: Пользователь перехватил управление мышью/клавиатурой. "
                    "Спроси у пользователя в чате разрешения продолжить или уточни дальнейшие действия."
                ),
            }

        try:
            if action == "take_control":
                return await self._action_take_control(params)
            elif action == "release_control":
                return await self._action_release_control()
            elif action == "get_screen_state":
                return await self._action_get_screen_state(params)
            elif action == "mouse_action":
                return await self._action_mouse_action(params)
            elif action == "keyboard_action":
                return await self._action_keyboard_action(params)
            elif action == "scroll_screen":
                return await self._action_scroll_screen(params)
            elif action == "list_windows_and_monitors":
                return await self._action_list_windows_and_monitors()
            elif action == "get_ui_elements":
                return await self._action_get_ui_elements(params)
            elif action == "reset_override":
                self.input_ctrl.reset_override()
                return {"status": "ok", "state": self.input_ctrl.state.value}
            else:
                return {"status": "error", "message": f"Unknown action: {action}"}

        except InterruptedByUserError as exc:
            logger.warning(f"Operation interrupted by user activity: {exc}")
            return {
                "status": "interrupted",
                "state": AutopilotState.USER_OVERRIDE.value,
                "message": (
                    "PAUSE: Пользователь перехватил управление мышью/клавиатурой. "
                    "Спроси у пользователя в чате разрешения продолжить или уточни дальнейшие действия."
                ),
            }
        except ScreenCaptureError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.error(f"Error while dispatching action {action}: {exc}", exc_info=True)
            return {"status": "error", "message": str(exc)}

    async def _action_take_control(self, params: dict[str, Any]) -> dict[str, Any]:
        """Locks session control to AI, activates persistent HUD neon frame, and returns initial screenshot."""
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", True)
        self._update_overlay_target(mon_idx)
        self.input_ctrl.set_state(AutopilotState.AI_ACTIVE)

        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=False,
            show_grid=show_grid,
        )

        img_cur_x = int(round((cursor_abs[0] - mon.left) / sx))
        img_cur_y = int(round((cursor_abs[1] - mon.top) / sy))

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "cursor": [img_cur_x, img_cur_y],
                "message": f"Управление захвачено. Рамка HUD активна на мониторе {mon.index} ('{mon.name}').",
            },
        }

    async def _action_release_control(self) -> dict[str, Any]:
        """Releases session control back to idle and hides the HUD neon border."""
        self.input_ctrl.set_state(AutopilotState.IDLE)
        self.overlay.sig_set_state.emit(AutopilotState.IDLE.value)
        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "message": "Управление освобождено. HUD рамка отключена.",
            },
        }

    async def _action_get_screen_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Captures screen and reports cursor coordinates."""
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", True)
        self._update_overlay_target(mon_idx)

        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=False,
            show_grid=show_grid,
        )

        img_cur_x = int(round((cursor_abs[0] - mon.left) / sx))
        img_cur_y = int(round((cursor_abs[1] - mon.top) / sy))

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "cursor": [img_cur_x, img_cur_y],
                "monitor": {
                    "index": mon.index,
                    "name": mon.name,
                    "bounds": [mon.left, mon.top, mon.width, mon.height],
                },
            },
        }

    async def _action_mouse_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Executes mouse move, click, or drag with HUD ripple notification."""
        sub_action = params.get("action", "click")
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", True)

        left, top, sx, sy = self._update_overlay_target(mon_idx)
        abs_x = left + int(round(x * sx))
        abs_y = top + int(round(y * sy))

        self.input_ctrl.set_state(AutopilotState.AI_ACTIVE)

        # Trigger 250ms predictive ripple at target location
        self.overlay.sig_show_target.emit(abs_x, abs_y)
        await asyncio.sleep(config.TARGET_RIPPLE_DURATION_MS / 1000.0)

        # Execute smooth move and click in worker thread
        if sub_action == "move":
            await asyncio.to_thread(self.input_ctrl.smooth_move, abs_x, abs_y)
        elif sub_action == "click":
            await asyncio.to_thread(self.input_ctrl.smooth_move, abs_x, abs_y)
            await asyncio.to_thread(self.input_ctrl.click, "left")
        elif sub_action == "double_click":
            await asyncio.to_thread(self.input_ctrl.smooth_move, abs_x, abs_y)
            await asyncio.to_thread(self.input_ctrl.double_click)
        elif sub_action == "right_click":
            await asyncio.to_thread(self.input_ctrl.smooth_move, abs_x, abs_y)
            await asyncio.to_thread(self.input_ctrl.click, "right")
        elif sub_action == "drag":
            end_x = int(params.get("end_x", x))
            end_y = int(params.get("end_y", y))
            abs_end_x = left + int(round(end_x * sx))
            abs_end_y = top + int(round(end_y * sy))
            await asyncio.to_thread(self.input_ctrl.drag, abs_x, abs_y, abs_end_x, abs_end_y)

        # Capture stabilized post-action screen
        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=True,
            show_grid=show_grid,
        )

        # Report where the cursor actually ended up, not where the caller asked it to
        # go. The two differ whenever a move was clipped, interrupted, or the
        # application moved the pointer itself -- and the difference is exactly
        # the signal the agent needs in order to notice a failed click.
        actual_x = int(round((cursor_abs[0] - mon.left) / sx))
        actual_y = int(round((cursor_abs[1] - mon.top) / sy))

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "requested": [x, y],
                "cursor": [actual_x, actual_y],
                "monitor": {
                    "index": mon.index,
                    "name": mon.name,
                    "bounds": [mon.left, mon.top, mon.width, mon.height],
                },
                "message": (
                    f"Mouse {sub_action} at ({x}, {y}); cursor now at "
                    f"({actual_x}, {actual_y})."
                ),
            },
        }

    async def _action_keyboard_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Executes text typing or key combination."""
        text = params.get("text")
        hotkey = params.get("hotkey")
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", True)

        self.input_ctrl.set_state(AutopilotState.AI_ACTIVE)

        if text:
            await asyncio.to_thread(self.input_ctrl.type_text, text)
        if hotkey:
            await asyncio.to_thread(self.input_ctrl.press_hotkey, hotkey)

        # Capture stabilized screen
        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=True,
            show_grid=show_grid,
        )

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "message": f"Keyboard action completed (text={bool(text)}, hotkey={hotkey}).",
            },
        }

    async def _action_scroll_screen(self, params: dict[str, Any]) -> dict[str, Any]:
        """Moves cursor to optional coordinates and scrolls mouse wheel."""
        direction = params.get("direction", "down")
        amount = int(params.get("amount", 3))
        x = params.get("x")
        y = params.get("y")
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", True)

        self.input_ctrl.set_state(AutopilotState.AI_ACTIVE)

        if x is not None and y is not None:
            left, top, sx, sy = self._update_overlay_target(mon_idx)
            abs_x = left + int(round(int(x) * sx))
            abs_y = top + int(round(int(y) * sy))
            await asyncio.to_thread(self.input_ctrl.smooth_move, abs_x, abs_y)

        await asyncio.to_thread(self.input_ctrl.scroll, direction, amount)

        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=True,
            show_grid=show_grid,
        )

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "message": f"Scrolled {direction} by {amount} clicks.",
            },
        }

    async def _action_list_windows_and_monitors(self) -> dict[str, Any]:
        """Returns metadata for all displays and open program windows."""
        monitors = self.vision.list_monitors()
        windows = await asyncio.to_thread(self.vision.list_windows)

        return {
            "status": "ok",
            "data": {
                "monitors": [
                    {
                        "index": m.index,
                        "name": m.name,
                        "bounds": [m.left, m.top, m.width, m.height],
                        "is_primary": m.is_primary,
                    }
                    for m in monitors
                ],
                "windows": [
                    {
                        "handle": w.handle,
                        "title": w.title,
                        "bounds": list(w.rect),
                        "monitor_index": w.monitor_index,
                        "is_active": w.is_active,
                    }
                    for w in windows[:25]
                ],
            },
        }

    async def _action_get_ui_elements(self, params: dict[str, Any]) -> dict[str, Any]:
        """Returns interactive UI control elements with pre-computed scaled click targets."""
        win_title = params.get("window_title")
        mon_idx = params.get("monitor_index")

        elements = await asyncio.to_thread(self.vision.inspect_ui_elements, win_title, mon_idx)

        return {
            "status": "ok",
            "data": {
                "count": len(elements),
                "elements": [
                    {
                        "control_type": e.control_type,
                        "title": e.title,
                        "automation_id": e.automation_id,
                        "click_target": [e.scaled_center_x, e.scaled_center_y],
                        "bounds": list(e.rect),
                    }
                    for e in elements[:60]
                ],
            },
        }

    def shutdown(self) -> None:
        """Cleans up hooks, tokens, and terminates application."""
        logger.info("Shutting down Autopilot Daemon...")
        self.input_ctrl.stop()
        if config.TOKEN_FILE_PATH.exists():
            with contextlib.suppress(Exception):
                config.TOKEN_FILE_PATH.unlink()
        self.app.quit()


def main() -> None:
    import concurrent.futures
    config.ensure_interactive_desktop()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    loop.set_default_executor(executor)

    daemon = AutopilotDaemon(app)

    async def run_server() -> None:
        server = await websockets.serve(
            daemon.handle_client,
            config.WS_HOST,
            config.WS_PORT,
            ping_interval=config.WS_HEARTBEAT_INTERVAL,
        )
        logger.info(f"WebSocket server listening on ws://{config.WS_HOST}:{config.WS_PORT}")
        await server.wait_closed()

    loop.create_task(run_server())

    try:
        loop.run_forever()
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    main()

