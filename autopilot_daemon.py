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
import time
from typing import Any

import qasync
import websockets
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from websockets.asyncio.server import ServerConnection

import audit
import config
import host_guard
import risk
import session_state
from config import AutopilotState
from input_controller import (
    MOUSE_SUB_ACTIONS,
    SCROLL_DIRECTIONS,
    InputController,
    InterruptedByUserError,
)
from ocr_engine import OcrReader
from overlay_ui import HUDOverlayWindow
from uia_client import (
    ACTIONABLE_PATTERNS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    DEFAULT_TIME_BUDGET_S,
    TERMINAL_BUFFER_ROWS,
    UIAClient,
)
from vision_engine import ScreenCaptureError, VisionEngine

# Must run before logging.basicConfig: StreamHandler captures sys.stderr at
# construction time, so the encoding has to be correct first.
config.configure_stdio_encoding()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
)
logger = logging.getLogger("AutopilotDaemon")

# Actions that never touch the session input queue. They bypass the input lock
# and they also stay available while USER_OVERRIDE is active, so the agent can
# keep observing and -- critically -- can always give control back.
#
# release_control belongs here even though it mutates state. Blocking it during
# USER_OVERRIDE was a dead end: the override gate stopped the only action that
# clears the HUD frame, so a pause left the neon border lit with no way out
# except reset_override or killing the daemon.
NON_INPUT_ACTIONS: frozenset[str] = frozenset({
    "get_screen_state",
    "list_windows_and_monitors",
    "get_ui_elements",
    "probe_target",
    "get_snapshot",
    "resolve_clickable",
    # Reading a console buffer touches nothing. Writing to one does mutate the
    # target, so write_terminal is deliberately absent: it takes the input lock and
    # stays blocked while a human has taken over.
    "read_terminal",
    "wait_terminal",
    # OCR reads pixels; it changes nothing and touches no input queue.
    "read_screen_text",
    "find_text_on_screen",
    "wait_for_text",
    "reset_override",
    "release_control",
    "hud_state",
    # Re-acquiring control has to be possible from a pause, or the pause is a dead
    # end: the agent could observe but never act again without a human telling it to
    # reset. take_control touches no input queue -- it sets state and captures once --
    # and the human keeps the interrupt, since the next physical movement pauses it
    # again immediately. The response says control was re-taken, and the HUD changes
    # colour, so this is visible rather than silent.
    "take_control",
})

# Returned verbatim to the model whenever an action is refused because a human
# took the machine. This is agent-facing instruction, not a log line, so it has
# to describe how the pause actually ends.
#
# It used to say "ask the user in chat for permission to continue", which stopped
# being true once the daemon began releasing the pause on its own after
# config.IDLE_RESUME_SECONDS of stillness. Leaving the stale wording in place
# meant the model was told to stall on a conversation turn for a pause that had
# already cleared. One constant, referenced by both paths that can raise it,
# because a message duplicated in two places drifts -- and this one did.
# Class B element verbs: name -> (handler, required params).
#
# A table rather than an if/elif chain so that the set of accepted names and the
# set of handlers are the same object. The mouse_action chain kept them apart and
# grew a hole: an unrecognized action matched no branch, did nothing, and returned
# status "ok".
#
# Each handler takes (uia, handle, path, params) and returns an ActionResult, whose
# tri-state `verified` is what tells the caller whether the effect was read back or
# merely accepted.
ELEMENT_ACTIONS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "invoke": (lambda uia, h, p, _pr: uia.invoke(h, p), ()),
    "set_value": (lambda uia, h, p, pr: uia.set_value(h, p, str(pr["text"])), ("text",)),
    "toggle": (lambda uia, h, p, _pr: uia.toggle(h, p), ()),
    "select": (lambda uia, h, p, _pr: uia.select_item(h, p), ()),
    "expand": (lambda uia, h, p, _pr: uia.expand_collapse(h, p, True), ()),
    "collapse": (lambda uia, h, p, _pr: uia.expand_collapse(h, p, False), ()),
    # Added by the Class B verb work. ScrollItem and RangeValue were already
    # published per node as actionable with no verb behind them, so an off-screen
    # element could only be reached by escalating to Class A and scrolling pixels.
    "scroll_into_view": (lambda uia, h, p, _pr: uia.scroll_into_view(h, p), ()),
    "focus": (lambda uia, h, p, _pr: uia.focus_element(h, p), ()),
    "set_range_value": (
        lambda uia, h, p, pr: uia.set_range_value(h, p, float(pr["value"])),
        ("value",),
    ),
}


# Window-level Class B verbs: name -> (handler, required params).
#
# Separate from ELEMENT_ACTIONS because the target is the window itself, addressed
# by handle, not a node inside it addressed by snapshot path. WindowPattern and
# TransformPattern both live on the window's root element.
WINDOW_ACTIONS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "close": (lambda uia, h, _pr: uia.close_window(h), ()),
    "minimize": (lambda uia, h, _pr: uia.set_window_state(h, "minimize"), ()),
    "maximize": (lambda uia, h, _pr: uia.set_window_state(h, "maximize"), ()),
    "restore": (lambda uia, h, _pr: uia.set_window_state(h, "normal"), ()),
    "move": (lambda uia, h, pr: uia.move_window(h, int(pr["x"]), int(pr["y"])),
             ("x", "y")),
    "resize": (lambda uia, h, pr: uia.resize_window(h, int(pr["width"]),
                                                    int(pr["height"])),
               ("width", "height")),
}


def pause_message() -> str:
    """Text handed to the model when an action is refused by an active pause.

    Reads the threshold at call time rather than at import, so the message and the
    policy cannot disagree: a monkeypatched or reconfigured IDLE_RESUME_SECONDS is
    reflected in what the agent is told, and a disabled auto-release says so
    instead of promising a resume that will never come.
    """
    head = "PAUSE: Пользователь перехватил управление мышью/клавиатурой. Действие не исполнено. "
    tail = (
        " Наблюдательные инструменты доступны и во время паузы. Для немедленного "
        "возврата управления вызовите take_control или reset_user_override."
    )
    if config.IDLE_RESUME_SECONDS > 0:
        return (
            head
            + f"Пауза снимется сама, как только пользователь не будет трогать ввод "
              f"{config.IDLE_RESUME_SECONDS:g} с подряд: повторите вызов, если задача "
                "всё ещё актуальна."
            + " Любое новое физическое движение мыши или нажатие клавиши снова "
              "приостановит агента."
            + tail
        )
    return (
        head
        + "Авто-снятие отключено (IDLE_RESUME_SECONDS=0): управление возвращается "
          "только через take_control, reset_user_override или Ctrl+Alt+Space "
          "со стороны пользователя."
        + tail
    )


class AutopilotDaemon:
    """Core daemon managing GUI overlay, input executor, and local WebSocket bridge."""

    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.auth_token = self._init_auth_token()

        self.vision = VisionEngine()
        # The UIA-first path from ADR-0007. Kept separate from VisionEngine so the
        # snapshot layer stays free of pywinauto, which ADR-0007 rules out of the
        # hot path.
        self.uia = UIAClient()
        # OCR is the fallback for targets with no accessibility tree. Created once:
        # engine construction does a language lookup not worth repeating per frame.
        self.ocr = OcrReader()
        self.overlay = HUDOverlayWindow()
        self.input_ctrl = InputController(on_state_change=self._on_input_state_changed)

        # Serializes every action that can touch the input queue. The host is free
        # to issue tool calls in parallel; without this, two worker threads end up
        # driving the same cursor at the same time.
        self._input_lock = asyncio.Lock()

        self._active_monitor_index: int | None = None

        # --- safety layer (ADR-0004) -------------------------------------
        # Append-only trace of every action, its target and its verification.
        # Created here rather than per action: rotation state and the record
        # sequence have to survive across calls or the log is not a timeline.
        self.audit = audit.AuditTrail(
            config.TRACE_PATH,
            max_bytes=config.TRACE_MAX_BYTES,
            keep=config.TRACE_KEEP,
            enabled=config.TRACE_ENABLED,
        )
        # Resolved from the gateway's PID at handshake, never carried as a list:
        # the PIDs recorded in PLAN.md were already stale when read back. None
        # means "not yet told", and the guard fails closed on that.
        self.host_tree: host_guard.HostTree | None = None

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
        # The MonitorInfo half of the return is discarded: bbox already carries
        # left/top/width/height, and binding a name nobody reads hides that fact
        # from F841, which does not report unused tuple unpacking.
        bbox, _ = self.vision.get_monitor_bounds(monitor_index)
        # Through the overlay's public entry point rather than emitting its signal
        # directly. Both reach the same slot, but having two ways to set the frame
        # means the one nobody calls rots -- and it was already the only caller-less
        # member of the overlay's public surface.
        self.overlay.set_target_geometry(
            bbox["left"], bbox["top"], bbox["width"], bbox["height"]
        )

        scaled_w = config.TARGET_IMAGE_WIDTH
        scaled_h = int(round(bbox["height"] * (scaled_w / float(bbox["width"]))))
        scale_x = bbox["width"] / float(scaled_w)
        scale_y = bbox["height"] / float(scaled_h)

        return bbox["left"], bbox["top"], scale_x, scale_y

    @staticmethod
    def _cursor_in_frame(
        cursor_abs: tuple[int, int], mon: Any, sx: float, sy: float
    ) -> list[int] | None:
        """Maps the desktop cursor into frame coordinates, or None if off-frame.

        Returning an out-of-frame position as though it were valid is worse than
        returning nothing. With a secondary monitor to the left of the primary,
        a cursor sitting on the secondary maps to a negative x on the primary's
        frame, and the agent then aims at coordinates that do not exist in the
        image it is looking at.
        """
        cx, cy = cursor_abs
        if not (mon.left <= cx < mon.left + mon.width):
            return None
        if not (mon.top <= cy < mon.top + mon.height):
            return None
        return [int(round((cx - mon.left) / sx)), int(round((cy - mon.top) / sy))]

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

                self._learn_host_pid(payload.get("host_pid"))

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

        Every action is traced, including read-only ones and including refusals. A
        trace that only recorded mutations would hide the observations that led to
        them, which is the part an investigation actually needs.
        """
        started = time.monotonic()
        if action in NON_INPUT_ACTIONS:
            resp = await self._dispatch(action, params)
        else:
            async with self._input_lock:
                resp = await self._dispatch(action, params)
        level = resp.pop("_risk", None) if isinstance(resp, dict) else None
        resp = self._annotate_session(resp)
        try:
            self._record_trace(action, params, resp, time.monotonic() - started, level)
        except Exception as exc:  # pragma: no cover - tracing must never break an action
            logger.debug("trace record failed: %s", exc)
        return resp

    @staticmethod
    def _annotate_session(resp: dict[str, Any]) -> dict[str, Any]:
        """Attaches workstation lock state to every successful response.

        A locked workstation is the single most misleading state to hide: screen
        capture still succeeds and returns the lock screen, so the agent sees a
        PIN prompt with no indication that it is not looking at the target
        application at all. It then either reports the app closed or tries to
        interact with a session that is not accepting input.

        Measured live: with the session locked, get_screen_state returned a
        normal-looking frame of the lock screen and nothing else was different.
        """
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            return resp

        info = session_state.session_state()
        data = resp.setdefault("data", {})
        data["session"] = {"locked": info["locked"], "state": info["state"]}
        # describe takes the dict rather than re-querying: describe_state would run
        # the same WTS call a second time on every successful response.
        note = session_state.describe(info)
        if note:
            data["session_warning"] = note
        return resp

    # ------------------------------------------------------ safety layer
    # ADR-0004. Three things that must happen around every mutating action:
    # refuse input into the host's own process tree, classify the resolved target
    # rather than the stated intent, and record both outcomes in the trace.

    def _learn_host_pid(self, pid: Any) -> None:
        """Builds the protected process set from the gateway's own PID.

        Done here rather than trusting a PID list from the client: the client says
        who it is, the daemon walks the tree itself. A list could be incomplete, and
        an incomplete list is a guard with holes that looks like a guard.
        """
        if self.host_tree is not None:
            return
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return
        if pid <= 0:
            return
        self.host_tree = host_guard.build_host_tree(pid)
        logger.info(
            "Host process tree armed: pid=%s ancestors=%s protected=%d%s",
            pid, [self.host_tree.names.get(p, p) for p in self.host_tree.ancestors],
            len(self.host_tree.pids),
            " (TRUNCATED)" if self.host_tree.truncated else "",
        )
        if self.host_tree.error:
            logger.warning("Host tree unusable, guard fails closed: %s",
                           self.host_tree.error)

    def _host_refusal(self, pid: int | None, allow: bool, what: str,
                      action: str) -> dict[str, Any] | None:
        """Refuses input into the host's own process tree, or returns None to allow.

        Fails closed in every direction, including the one that is easy to get
        wrong: a gateway that never sent its PID gets a refusal, not a permissive
        guard. Refusing is diagnosable; silently allowing input into the window that
        renders the conversation is not, and the result is text the agent then reads
        back as user input.
        """
        if not config.HOST_GUARD_ENABLED:
            return None
        if allow:
            # Only tools that address a window explicitly can pass this. Coordinate
            # actions never do: nothing in such a request says what it will land on.
            self.audit.record(audit.TraceRecord(
                action=action, kind="host_override", target=what,
                message="ввод в дерево процессов хоста разрешён явно",
                klass=audit.CLASS_B, ok=True, verified=None))
            return None
        if self.host_tree is None:
            return {
                "status": "refused",
                "guard": "host_process_tree",
                "message": (
                    "Защита дерева процессов хоста не вооружена: шлюз не передал "
                    "host_pid. Действие не выполнено. Отказ намеренный -- разрешить "
                    "ввод, не зная, какое окно принадлежит хосту, значило бы открыть "
                    "ровно тот путь, которым агент печатает в собственный чат."
                ),
            }
        if not self.host_tree.blocks(pid):
            return None
        return {
            "status": "refused",
            "guard": "host_process_tree",
            "message": (
                f"Отказано: цель ({self.host_tree.describe(pid)}) принадлежит дереву "
                "процессов хоста, который управляет этой сессией. Ввод туда не "
                f"выполнен. Защищённые PID: {len(self.host_tree.pids)}."
            ),
            "protected": sorted(self.host_tree.pids)[:32],
        }

    @staticmethod
    def _risk_of(kind: str, verb: str = "", control_name: str = "",
                 app: str = "") -> risk.Risk:
        """Classifies one action against config.CRITICAL_APPS."""
        if kind == "element":
            return risk.classify_action(verb, control_name=control_name, app=app,
                                        critical_apps=config.CRITICAL_APPS)
        if kind == "hotkey":
            return risk.classify_hotkey(verb, app=app,
                                        critical_apps=config.CRITICAL_APPS)
        return risk.classify_click(app=app, critical_apps=config.CRITICAL_APPS)

    def _record_trace(self, action: str, params: dict[str, Any],
                      resp: dict[str, Any], elapsed_s: float,
                      level: risk.Risk | None = None) -> None:
        """Writes one trace record from whatever the response actually carries.

        Reads fields rather than requiring each action to build a record: the
        response envelope already holds ok/verified/status/message, and a second
        source of truth for the same facts is how a trace starts disagreeing with
        what was returned.
        """
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        target = params.get("path") or params.get("hwnd") or ""
        if not target:
            x, y = params.get("x"), params.get("y")
            if x is not None and y is not None:
                target = f"{x},{y}"
        frame_after = data.get("screenshot")
        rec = audit.TraceRecord(
            action=action,
            klass=(audit.CLASS_B if action in ("element_action", "window_action",
                                               "write_terminal", "read_terminal")
                   else audit.CLASS_A if action in ("mouse_action", "keyboard_action",
                                                    "scroll_screen") else ""),
            target=str(target),
            element=str(data.get("path") or ""),
            level=(level.level if level else ""),
            ok=resp.get("ok", data.get("ok")),
            verified=data.get("verified", resp.get("verified")),
            status=str(resp.get("status", "")),
            message=str(resp.get("message") or data.get("message") or ""),
            # Hashed from the base64 the response already carries, not from decoded
            # bytes: decoding every frame just to hash it would cost more than the
            # record is worth, and the digest only has to correlate records.
            frame_after=(audit.frame_hash(frame_after.encode("utf-8"))
                         if isinstance(frame_after, str) else None),
            elapsed_ms=elapsed_s * 1000.0,
            extra=({"guard": resp["guard"]} if resp.get("guard") else {}),
        )
        self.audit.record(rec)

    async def _dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatches and executes requested tool action."""
        current_state = self.input_ctrl.state

        # Immediate return if user override is active. Read-only actions stay
        # available so the agent can still observe and re-plan while paused.
        if current_state == AutopilotState.USER_OVERRIDE and action not in NON_INPUT_ACTIONS:
            return {
                "status": "interrupted",
                "state": current_state.value,
                "message": pause_message(),
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
            elif action == "probe_target":
                return await self._action_probe_target(params)
            elif action == "get_snapshot":
                return await self._action_get_snapshot(params)
            elif action == "resolve_clickable":
                return await self._action_resolve_clickable(params)
            elif action == "element_action":
                return await self._action_element_action(params)
            elif action == "window_action":
                return await self._action_window_action(params)
            elif action == "read_terminal":
                return await self._action_read_terminal(params)
            elif action == "write_terminal":
                return await self._action_write_terminal(params)
            elif action == "wait_terminal":
                return await self._action_wait_terminal(params)
            elif action == "read_screen_text":
                return await self._action_read_screen_text(params)
            elif action == "find_text_on_screen":
                return await self._action_find_text_on_screen(params)
            elif action == "wait_for_text":
                return await self._action_wait_for_text(params)
            elif action == "hud_state":
                return await self._action_hud_state()
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
                "message": pause_message(),
            }
        except ScreenCaptureError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.error(f"Error while dispatching action {action}: {exc}", exc_info=True)
            return {"status": "error", "message": str(exc)}

    async def idle_resume_loop(self) -> None:
        """Polls for user stillness and releases USER_OVERRIDE on its own.

        Runs for the daemon's lifetime. The check is cheap -- one GetLastInputInfo
        call -- and only acts while paused, so the steady-state cost is a timer tick
        every half second.

        Released here rather than inside the hook callback, because the hook only
        fires on input: a human who stops touching the machine produces no event at
        all, so nothing inside the hook could ever notice the stillness.
        """
        if config.IDLE_RESUME_SECONDS <= 0:
            logger.info("USER_OVERRIDE auto-release disabled (IDLE_RESUME_SECONDS=0); "
                        "only an explicit reset or Ctrl+Alt+Space resumes.")
            return
        logger.info(
            f"USER_OVERRIDE auto-release armed at {config.IDLE_RESUME_SECONDS:g} s "
            f"of user inactivity (poll every {config.IDLE_RESUME_POLL_SECONDS:g} s)."
        )
        while True:
            await asyncio.sleep(config.IDLE_RESUME_POLL_SECONDS)
            try:
                # No manual overlay or tray update here: set_state invokes the
                # _on_input_state_changed callback, which already synchronizes both.
                await asyncio.to_thread(self.input_ctrl.maybe_auto_resume)
            except Exception as exc:  # a failed poll must not kill the loop
                logger.debug(f"idle-resume poll failed: {exc}")
                continue

    async def _action_take_control(self, params: dict[str, Any]) -> dict[str, Any]:
        """Locks session control to AI, activates persistent HUD neon frame, and returns initial screenshot."""
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", False)
        # Record whether this re-acquires control from a pause, so the response says
        # so. Taking control back after a human intervened is visible by design --
        # the HUD changes colour and the caller is told -- rather than happening
        # silently under an ordinary "control acquired" message.
        was_paused = self.input_ctrl.state == AutopilotState.USER_OVERRIDE
        self._update_overlay_target(mon_idx)
        # set_state drives the overlay and tray through _on_input_state_changed, so
        # no separate emit is needed here.
        self.input_ctrl.set_state(AutopilotState.AI_ACTIVE)

        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=False,
            show_grid=show_grid,
        )

        cursor_frame = self._cursor_in_frame(cursor_abs, mon, sx, sy)

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "cursor": cursor_frame,
                "cursor_desktop": list(cursor_abs),
                "message": (
                    "Управление возвращено агенту после перехвата пользователем. "
                    f"Рамка HUD снова активна на мониторе {mon.index} ('{mon.name}'). "
                    "Любое физическое движение мыши или нажатие клавиши снова "
                    "приостановит агента."
                    if was_paused else
                    f"Управление захвачено. Рамка HUD активна на мониторе "
                    f"{mon.index} ('{mon.name}')."
                ),
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
        show_grid = params.get("show_grid", False)
        self._update_overlay_target(mon_idx)

        cursor_abs = self.input_ctrl.get_cursor_pos()
        encoded_webp, w, h, sx, sy, mon = await asyncio.to_thread(
            self.vision.capture_screen,
            monitor_index=mon_idx,
            cursor_pos=cursor_abs,
            wait_stabilization=False,
            show_grid=show_grid,
        )

        cursor_frame = self._cursor_in_frame(cursor_abs, mon, sx, sy)

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "cursor": cursor_frame,
                "cursor_desktop": list(cursor_abs),
                "monitor": {
                    "index": mon.index,
                    "name": mon.name,
                    "bounds": [mon.left, mon.top, mon.width, mon.height],
                },
            },
        }

    def _frame_size(self, monitor_index: int | None) -> tuple[int, int]:
        """Frame dimensions in the coordinate space callers must use.

        Callers give coordinates relative to the scaled frame, not to the desktop.
        Without knowing the frame size the daemon cannot tell a coordinate that
        belongs to another monitor from one that is simply wrong.
        """
        bbox, _mon = self.vision.get_monitor_bounds(monitor_index)
        width = config.TARGET_IMAGE_WIDTH
        height = int(round(bbox["height"] * (width / float(bbox["width"]))))
        return width, height

    def _reject_out_of_frame(
        self, mon_idx: int | None, points: list[tuple[str, int | None, int | None]]
    ) -> dict[str, Any] | None:
        """Returns an error response if any point falls outside the frame.

        Coordinates outside the frame used to be scaled, then clamped by the move
        itself, and the click landed anyway. A live check with (-99999, -99999)
        reported "cursor now at (0, 0)" -- the corner of the monitor was clicked.
        Silent clamping is the worst outcome available here: the action succeeds, the
        cursor ends up somewhere predictable but unrelated to the request, and nothing
        tells the caller that its coordinates were wrong. Stale or miscomputed
        coordinates are exactly what a bounds check should stop.
        """
        frame_w, frame_h = self._frame_size(mon_idx)
        for label, px, py in points:
            if px is None or py is None:
                continue
            if not (0 <= px <= frame_w and 0 <= py <= frame_h):
                return {
                    "status": "error",
                    "message": (
                        f"{label} ({px}, {py}) вне кадра {frame_w}x{frame_h} этого "
                        f"монитора. Действие не выполнено. Обрезание координат "
                        f"привело бы к клику в непредсказуемой точке: в замере "
                        f"(-99999, -99999) завершилось кликом в углу монитора. "
                        f"Проверьте monitor_index и координаты; если окно на другом "
                        f"мониторе, укажите его индекс."
                    ),
                    "frame": [frame_w, frame_h],
                    "point": [px, py],
                }
        return None

    async def _action_mouse_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Executes mouse move, click, or drag with HUD ripple notification."""
        sub_action = params.get("action", "click")
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", False)

        # Rejected here rather than left to the dispatch chain below, because the
        # chain has no else branch: an unrecognized sub_action used to fall through
        # every elif, do nothing at all, and still return status "ok" with a fresh
        # screenshot. That is the most damaging silent acceptance in the daemon --
        # the caller sees a successful mouse action that never happened, on a frame
        # that looks like evidence it did. Validating first also keeps the rejection
        # ahead of the ripple and the cursor move, so a bad request changes nothing.
        if sub_action not in MOUSE_SUB_ACTIONS:
            return {
                "status": "error",
                "message": (
                    f"Неизвестное действие мыши {sub_action!r}; ничего не выполнено. "
                    f"Допустимые значения: {', '.join(sorted(MOUSE_SUB_ACTIONS))}."
                ),
            }

        points = [("Координаты", x, y)]
        if sub_action == "drag":
            points.append(("Конечные координаты", params.get("end_x"),
                           params.get("end_y")))
        rejected = self._reject_out_of_frame(mon_idx, points)
        if rejected is not None:
            return rejected

        left, top, sx, sy = self._update_overlay_target(mon_idx)
        abs_x = left + int(round(x * sx))
        abs_y = top + int(round(y * sy))

        # Guarded before the state change and before the ripple, so a refusal leaves
        # no visible trace on the desktop. Coordinate actions get no override: unlike
        # a window-addressed verb, nothing in the request says what the point will
        # land on, so an "I meant to" from the caller is not evidence.
        guard = self._host_refusal(
            await asyncio.to_thread(host_guard.pid_at_point, abs_x, abs_y),
            False, f"точка ({abs_x}, {abs_y})", "mouse_action")
        if guard is not None:
            return guard

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
        else:
            # Unreachable while MOUSE_SUB_ACTIONS and this chain agree, which is
            # precisely why it is spelled out: the accepted set is a literal above
            # and the handlers are literals here, and two literals drift. Falling
            # through silently is what it did before.
            return {
                "status": "error",
                "message": (
                    f"Действие мыши {sub_action!r} принято, но не обработано. "
                    "Список MOUSE_SUB_ACTIONS и цепочка обработчиков разошлись."
                ),
            }

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
        # None means the cursor is not on this monitor at all, which is reported
        # as such rather than as a bogus negative coordinate.
        actual = self._cursor_in_frame(cursor_abs, mon, sx, sy)
        actual_desc = (
            f"({actual[0]}, {actual[1]})"
            if actual
            else f"off-frame (desktop {cursor_abs[0]}, {cursor_abs[1]})"
        )

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "_risk": self._risk_of("click"),
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "scale_x": sx,
                "scale_y": sy,
                "requested": [x, y],
                "cursor": actual,
                "cursor_desktop": list(cursor_abs),
                "monitor": {
                    "index": mon.index,
                    "name": mon.name,
                    "bounds": [mon.left, mon.top, mon.width, mon.height],
                },
                "message": (
                    f"Mouse {sub_action} at ({x}, {y}); cursor now at {actual_desc}."
                ),
            },
        }

    async def _action_keyboard_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Executes text typing or key combination."""
        text = params.get("text")
        hotkey = params.get("hotkey")
        mon_idx = params.get("monitor_index")
        show_grid = params.get("show_grid", False)

        # A request with neither payload used to return status "ok" plus a
        # screenshot, having typed nothing. Same defect as the mouse chain above:
        # success reported for an action that never happened. Rejected before the
        # state change so a malformed request cannot flip the session to AI_ACTIVE.
        if text is None and hotkey is None:
            return {
                "status": "error",
                "message": (
                    "Ни text, ни hotkey не переданы; ничего не отправлено. "
                    "Укажите хотя бы одно из двух."
                ),
            }

        # Keyboard input carries no target of its own -- it lands on whatever holds
        # focus -- so the guard has to resolve the focused window first. Text and a
        # hotkey are classified separately: typing into a field is a write, while
        # Enter in a messenger is the send action.
        fg_pid = await asyncio.to_thread(host_guard.foreground_pid)
        app = (self.host_tree.names.get(fg_pid, "") if self.host_tree and fg_pid
               else "")
        guard = self._host_refusal(fg_pid, False,
                                   f"активное окно (pid {fg_pid})", "keyboard_action")
        if guard is not None:
            return guard
        if hotkey:
            level = self._risk_of("hotkey", verb=str(hotkey), app=app)
        else:
            # Typing into a field is a write. Its level cannot come from a control
            # name, because keyboard input names no control -- only the focused
            # application, which is what makes a critical app matter here.
            level = self._risk_of("element", verb="set_value", app=app)

        if params.get("dry_run"):
            return {
                "status": "ok",
                "dry_run": True,
                "_risk": level,
                "data": {
                    "resolved_pid": fg_pid, "app": app,
                    "risk_level": level.level, "risk_reason": level.reason,
                    "batchable": level.batchable,
                    "message": (
                        f"DRY RUN: ввод {'hotkey ' + str(hotkey) if hotkey else 'text'} "
                        f"в приложение {app or '(не определено)'}; уровень риска "
                        f"{level.level}. Ничего не отправлено."
                    ),
                },
            }

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
            "_risk": level,
            "data": {
                "screenshot": encoded_webp,
                "scaled_width": w,
                "scaled_height": h,
                "risk_level": level.level,
                "risk_reason": level.reason,
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
        show_grid = params.get("show_grid", False)

        # InputController.scroll raises on the same two conditions, and that stays
        # the authority. Checking here as well is not duplication of the rule but of
        # its timing: without it a bad direction is discovered after the cursor has
        # already been moved to (x, y), so a rejected request still leaves a visible
        # side effect.
        if direction not in SCROLL_DIRECTIONS:
            return {
                "status": "error",
                "message": (
                    f"Неизвестное направление прокрутки {direction!r}; ничего не "
                    f"выполнено. Допустимые значения: "
                    f"{', '.join(sorted(SCROLL_DIRECTIONS))}."
                ),
            }
        if amount < 0:
            return {
                "status": "error",
                "message": (
                    f"Количество шагов прокрутки не может быть отрицательным "
                    f"(получено {amount}); ничего не выполнено."
                ),
            }

        # A wheel event goes to whatever is under the cursor, so the guard resolves
        # the point being scrolled over. Without coordinates that is the cursor's
        # current position, which is what will actually receive the event.
        if x is not None and y is not None:
            probe_x, probe_y = int(x), int(y)
            left, top, sx, sy = self._update_overlay_target(mon_idx)
            probe_x = left + int(round(probe_x * sx))
            probe_y = top + int(round(probe_y * sy))
        else:
            probe_x, probe_y = self.input_ctrl.get_cursor_pos()
        guard = self._host_refusal(
            await asyncio.to_thread(host_guard.pid_at_point, probe_x, probe_y),
            False, f"точка прокрутки ({probe_x}, {probe_y})", "scroll_screen")
        if guard is not None:
            return guard

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
                        "on_monitor": w.on_monitor,
                        "class_name": w.class_name,
                        "pid": w.pid,
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
        hwnd = params.get("hwnd")
        cls = params.get("class_name")

        result = await asyncio.to_thread(
            self.vision.inspect_ui_elements,
            window_title=win_title,
            monitor_index=mon_idx,
            hwnd=hwnd,
            class_name=cls,
        )
        bbox, mon = await asyncio.to_thread(self.vision.get_monitor_bounds, mon_idx)
        monitor_info = {
            "index": mon.index,
            "name": mon.name,
            "bounds": [bbox["left"], bbox["top"], bbox["width"], bbox["height"]],
        }

        tgt = result.target
        if tgt is None:
            # No window resolved at all. Returning an empty element list here
            # would be indistinguishable from a window that exposes nothing.
            return {
                "status": "error",
                "message": result.error or "No window matched the given criteria.",
                "data": {"monitor": monitor_info},
            }

        data: dict[str, Any] = {
            "count": len(result.elements),
            "monitor": monitor_info,
            "frame": [
                config.TARGET_IMAGE_WIDTH,
                round(bbox["height"] / (bbox["width"] / config.TARGET_IMAGE_WIDTH)),
            ],
            "target": {
                "handle": tgt.handle,
                "title": tgt.title,
                "class_name": tgt.class_name,
                "pid": tgt.pid,
                "bounds": list(tgt.rect),
                "resolution": tgt.resolution,
            },
            "elements": [
                {
                    "control_type": e.control_type,
                    "title": e.title,
                    "automation_id": e.automation_id,
                    "click_target": [e.scaled_center_x, e.scaled_center_y],
                    "bounds": list(e.rect),
                }
                for e in result.elements[:60]
            ],
        }

        if tgt.resolution.startswith("largest-of-"):
            # Several top-level windows share this title. The largest was chosen,
            # but the caller should not have to guess: an exact handle removes the
            # doubt entirely.
            data["ambiguous_title"] = True
            data["hint_ambiguous"] = (
                "Несколько окон имеют одинаковый заголовок; выбрано наибольшее по "
                "площади. Передайте точный handle из list_windows_and_monitors, "
                "чтобы исключить неоднозначность."
            )
        if result.skipped_off_monitor:
            # Tells the agent that the window straddles or sits outside the
            # captured monitor, so it can re-target with a different monitor_index
            # instead of concluding the window has no controls.
            data["skipped_off_monitor"] = result.skipped_off_monitor
            data["hint"] = (
                f"{result.skipped_off_monitor} элемент(ов) лежат вне монитора "
                f"{mon.index} и не имеют корректного click_target. Повторите "
                "запрос с monitor_index того монитора, на котором находится "
                "окно, используя указанные границы."
            )
        if result.error:
            data["warning"] = result.error

        return {"status": "ok", "data": data}

    # ------------------------------------------------------- UIA-first path
    async def _resolve_target(self, params: dict[str, Any]) -> tuple[int | None, str]:
        """Resolves a target handle, or returns a reason the caller can act on.

        A title alone does not identify a window, so the reason string distinguishes
        a dead handle from nothing matching. Sending both back as one generic
        message would make the agent try both remedies at once.
        """
        handle, how, _candidates = await asyncio.to_thread(
            self.uia.resolve_handle,
            params.get("hwnd"),
            params.get("window_title"),
            params.get("class_name"),
        )
        if handle is not None:
            return handle, how
        if how == "hwnd-invalid":
            return None, (
                f"Дескриптор окна {params.get('hwnd')} недействителен "
                "(IsWindow вернул false) — окно могло быть закрыто. Запросите "
                "list_windows_and_monitors для получения актуальных дескрипторов."
            )
        return None, (
            "Ни одно видимое окно не подошло под заданные критерии. Запросите "
            "list_windows_and_monitors и передайте точный handle."
        )

    @staticmethod
    def _path_recommendation(classification: str, iconic: bool) -> str:
        """Which control path a target actually supports, from its classification.

        Derived from measurement, not from guesswork: ADR-0007/0008 for the
        tree-based classes and ADR-0009 for terminals.
        """
        base = {
            "terminal": (
                "Терминал. Читается через TextPattern и пишется WM_CHAR в дочернее "
                "окно ввода — обе операции Класса B, курсор не требуется. "
                "ValuePattern принимать запись не будет, несмотря на IsReadOnly=0."
            ),
            "rich": (
                "Семантический путь. Снимок дерева и действия через паттерны "
                "(Invoke, Value, Toggle) — Класс B, курсор и фокус не требуются."
            ),
            "partial": (
                "Смешанный путь. Имена заполнены частично: сверяйте элементы по "
                "роли, позиции и доступным паттернам, а не только по тексту."
            ),
            "opaque": (
                "Пиксельный путь. Дерево недоступно или пусто — используйте снимок "
                "экрана и координаты. Это действия Класса A: они занимают курсор."
            ),
            "expensive": (
                "Цель патологически дорогая для обхода. Снимок будет заведомо "
                "неполным; задайте малый max_nodes и работайте по токенам."
            ),
        }.get(classification, "Классификация неизвестна; начните с probe_target.")
        if iconic:
            base += (
                " Окно свёрнуто: координаты недействительны, а захват вернёт чёрный "
                "кадр. Разверните его перед наблюдением по пикселям."
            )
        return base

    async def _action_probe_target(self, params: dict[str, Any]) -> dict[str, Any]:
        """Classifies a target and reports which control path it supports."""
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}

        probe = await asyncio.to_thread(self.uia.probe_target, handle)
        return {
            "status": "ok",
            "data": {
                "handle": probe.handle,
                "title": probe.title,
                "class_name": probe.class_name,
                "pid": probe.pid,
                "resolution": how,
                "iconic": probe.iconic,
                "child_count": probe.child_count,
                "tree_sample": probe.tree_sample,
                "useful_nodes": probe.useful,
                "area": probe.area,
                "us_per_node": (
                    round(probe.us_per_node, 1) if probe.us_per_node else None
                ),
                "classification": probe.classification,
                "patterns": list(probe.patterns),
                "notes": list(probe.notes),
                "recommendation": self._path_recommendation(
                    probe.classification, probe.iconic
                ),
            },
        }

    def _frame_point(self, bbox: dict[str, int], x: int, y: int) -> list[int] | None:
        """Absolute desktop point -> frame coordinate, or None if off this monitor.

        Off-monitor elements get no frame coordinate rather than a negative one:
        a point outside the captured frame is not a place the agent can aim at in
        the image it is looking at.
        """
        if not (bbox["left"] <= x < bbox["left"] + bbox["width"]
                and bbox["top"] <= y < bbox["top"] + bbox["height"]):
            return None
        scale_x = bbox["width"] / float(config.TARGET_IMAGE_WIDTH)
        scaled_h = round(bbox["height"] / scale_x)
        scale_y = bbox["height"] / float(scaled_h)
        return [round((x - bbox["left"]) / scale_x), round((y - bbox["top"]) / scale_y)]

    def _containing_monitor_index(self, handle: int) -> int | None:
        """Index of the monitor holding the window's centre, or None.

        Index 0 is skipped: the virtual desktop contains every window, so it can
        never answer "which screen is this on".
        """
        bounds = self.uia.window_bounds(handle)
        if not bounds:
            return None
        cx, cy = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        for m in self.vision.list_monitors():
            if m.index == 0:
                continue
            if m.left <= cx < m.left + m.width and m.top <= cy < m.top + m.height:
                return m.index
        return None

    async def _action_get_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bounded UIA snapshot with frame-space coordinates and a continuation token."""
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}

        budget_ms = params.get("time_budget_ms")
        snap = await asyncio.to_thread(
            lambda: self.uia.snapshot(
                handle,
                max_depth=int(params.get("max_depth") or DEFAULT_MAX_DEPTH),
                max_nodes=int(params.get("max_nodes") or DEFAULT_MAX_NODES),
                time_budget_s=(float(budget_ms) / 1000.0 if budget_ms
                               else DEFAULT_TIME_BUDGET_S),
                token=params.get("token"),
                auto_restore=bool(params.get("auto_restore")),
            )
        )
        mon_idx = params.get("monitor_index")
        bbox, mon = await asyncio.to_thread(self.vision.get_monitor_bounds, mon_idx)

        # A target that does not lie on the requested monitor produces no usable
        # coordinate at all. Saying so, with the index that would work, beats
        # returning a tree where every element reads "outside the frame".
        containing = await asyncio.to_thread(self._containing_monitor_index, handle)
        if containing is not None and containing != mon.index:
            snap_hint = (
                f"Окно находится на мониторе {containing}, а снимок запрошен для "
                f"монитора {mon.index}: координаты недействительны. Повторите с "
                f"monitor_index={containing}."
            )
        else:
            snap_hint = None

        # Frame coordinates must be in the text the model reads, not only in the
        # structured payload, or the model has to convert them itself and will
        # occasionally get it wrong.
        def to_frame(point):
            if point is None:
                return None
            return self._frame_point(bbox, point[0], point[1])

        nodes = []
        for n in snap.nodes:
            fp = to_frame(n.clickable)
            if not (fp or (set(n.patterns) & ACTIONABLE_PATTERNS)):
                continue
            nodes.append({
                "path": n.path,
                "depth": n.depth,
                "role": n.role,
                "text": n.text,
                "text_source": n.text_source,
                "text_usable": n.text_usable,
                "patterns": list(n.patterns),
                "enabled": n.enabled,
                "offscreen": n.offscreen,
                "abs_point": list(n.clickable) if n.clickable else None,
                "frame_point": fp,
            })

        data: dict[str, Any] = {
            "handle": handle,
            "resolution": how,
            "monitor": {
                "index": mon.index, "name": mon.name,
                "bounds": [bbox["left"], bbox["top"], bbox["width"], bbox["height"]],
            },
            "frame": [config.TARGET_IMAGE_WIDTH,
                      round(bbox["height"] / (bbox["width"] / config.TARGET_IMAGE_WIDTH))],
            "node_count": len(snap.nodes),
            "actionable_count": len(nodes),
            "truncated": snap.truncated,
            "truncated_reason": snap.truncated_reason,
            "next_token": snap.next_token,
            "elapsed_ms": round(snap.elapsed_ms, 1),
            "queries": snap.queries,
            "failed_queries": snap.failed_queries,
            "minimized": snap.minimized,
            "auto_restored": snap.auto_restored,
            "monitor_hint": snap_hint,
            "warnings": snap.warnings,
            "tree": self.uia.format_snapshot(snap, to_frame=to_frame),
            "nodes": nodes[:150],
        }
        return {"status": "ok", "data": data}

    async def _action_resolve_clickable(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolves one node's precise clickable point, in frame coordinates.

        The snapshot carries a rect centre because UIA's own clickable point is a
        live round trip that costs 162 us per node. This resolves the precise one
        for the single element about to be acted on, which is also fresher.
        """
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}
        path = params.get("path")
        if not path:
            return {"status": "error", "message": "Не указан path узла из снимка."}

        point = await asyncio.to_thread(self.uia.clickable_point, handle, str(path))
        bbox, mon = await asyncio.to_thread(
            self.vision.get_monitor_bounds, params.get("monitor_index")
        )
        if point is None:
            return {
                "status": "error",
                "message": (
                    f"Узел {path} не имеет точки клика: элемент вне экрана, свёрнут "
                    "или путь больше не разрешается (дерево изменилось). Сделайте "
                    "новый снимок вместо клика по устаревшей координате."
                ),
            }
        fp = self._frame_point(bbox, point[0], point[1])
        containing = None
        if fp is None:
            # Without this the caller only learns that the point is not on the
            # requested monitor, and has to enumerate monitors to find the right one.
            containing = await asyncio.to_thread(self._containing_monitor_index, handle)
        return {
            "status": "ok",
            "data": {
                "handle": handle, "resolution": how, "path": str(path),
                "abs_point": list(point),
                "frame_point": fp,
                "monitor_index": mon.index,
                "on_monitor": fp is not None,
                "containing_monitor": containing,
            },
        }

    async def _action_element_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Performs one Class B pattern action and reports whether it verified.

        Pattern actions do not touch the input queue, so they need no cursor. They do
        mutate application state, so they take the input lock (serializing against
        any in-flight input) and they stay blocked during USER_OVERRIDE: a human who
        has taken over is also manipulating that application.

        Dispatch goes through ELEMENT_ACTIONS rather than an if/elif chain, because
        a chain needs a separate list of accepted names and the two drift. The
        mouse_action chain had exactly that gap: an unrecognized action fell through
        every branch and reported success.
        """
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}
        path = params.get("path")
        kind = params.get("action")
        if not path:
            return {"status": "error", "message": "Не указан path узла из снимка."}

        entry = ELEMENT_ACTIONS.get(kind)
        if entry is None:
            return {
                "status": "error",
                "message": f"Неизвестное действие {kind!r}. Допустимо: "
                           f"{', '.join(sorted(ELEMENT_ACTIONS))}.",
            }
        handler, required = entry
        missing = [name for name in required if params.get(name) is None]
        if missing:
            return {
                "status": "error",
                "message": f"Для действия {kind!r} требуются параметры: "
                           f"{', '.join(missing)}.",
            }

        # Classification reads the element the path resolved to, not the verb the
        # caller named. That is the whole point: a mis-bind is the likeliest way this
        # system does damage, and it is invisible to every check that trusts the
        # request.
        identity = await asyncio.to_thread(self.uia.node_identity, handle, str(path))
        control_name = (identity or {}).get("name", "")
        app = ""
        pid = (identity or {}).get("pid")
        if pid is None:
            pid = await asyncio.to_thread(host_guard.pid_of_window, handle)
        if self.host_tree is not None and pid:
            app = self.host_tree.names.get(pid, "")
        level = self._risk_of("element", verb=str(kind), control_name=control_name,
                              app=app)

        refusal = self._host_refusal(pid, bool(params.get("allow_host_input")),
                                     f"узел {path} окна {handle}", "element_action")
        if refusal is not None:
            refusal["_risk"] = level
            return refusal

        if params.get("dry_run"):
            return {
                "status": "ok",
                "dry_run": True,
                "_risk": level,
                "data": {
                    "handle": handle, "resolution": how, "path": str(path),
                    "resolved_name": control_name,
                    "resolved_role": (identity or {}).get("role", ""),
                    "resolved_pid": pid,
                    "app": app,
                    "risk_level": level.level,
                    "risk_reason": level.reason,
                    "batchable": level.batchable,
                    "host_pid": pid,
                    "message": (
                        f"DRY RUN: действие {kind!r} разрешено на элементе "
                        f"{control_name!r} ({(identity or {}).get('role', '?')}); "
                        f"уровень риска {level.level}. Ничего не выполнено."
                    ),
                },
            }

        res = await asyncio.to_thread(handler, self.uia, handle, str(path), params)
        env = self._action_envelope(handle, how, res)
        env["_risk"] = level
        env["data"]["risk_level"] = level.level
        env["data"]["risk_reason"] = level.reason
        env["data"]["resolved_name"] = control_name
        return env

    @staticmethod
    def _action_envelope(handle: int, how: str, res: Any) -> dict[str, Any]:
        """One response shape for every Class B action.

        Shared between element and window verbs so the two cannot drift in what they
        report. `verified` is passed through as the tri-state it is: None means the
        action has no readable state and success is only "the call was accepted",
        which the gateway renders as a distinctly weaker claim.
        """
        return {
            "status": "ok",
            "data": {
                "handle": handle,
                "resolution": how,
                "action": res.action,
                "path": res.path,
                "ok": res.ok,
                "verified": res.verified,
                "before": res.before,
                "after": res.after,
                "message": res.message,
            },
        }

    async def _action_window_action(self, params: dict[str, Any]) -> dict[str, Any]:
        """Performs one window-level Class B verb and reports whether it verified.

        Same gating as element actions: these change application state, so they take
        the input lock and stay blocked during USER_OVERRIDE. A human who just took
        the machine may be arranging those windows.

        Closing and minimizing are the two that matter most for observation: a
        minimized window captures as a black frame, and a window left open by a
        failed close usually has a modal on it that the agent has to answer.
        """
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}
        kind = params.get("action")

        entry = WINDOW_ACTIONS.get(kind)
        if entry is None:
            return {
                "status": "error",
                "message": f"Неизвестное действие окна {kind!r}. Допустимо: "
                           f"{', '.join(sorted(WINDOW_ACTIONS))}.",
            }
        handler, required = entry
        missing = [name for name in required if params.get(name) is None]
        if missing:
            return {
                "status": "error",
                "message": f"Для действия {kind!r} требуются параметры: "
                           f"{', '.join(missing)}.",
            }

        info = await asyncio.to_thread(self._safe_target_info, handle)
        pid = info.get("pid") or await asyncio.to_thread(host_guard.pid_of_window,
                                                         handle)
        app = (self.host_tree.names.get(pid, "") if self.host_tree and pid else "")
        # The window title stands in for a control name: these verbs act on the
        # window itself, so the title is the only semantics the target carries.
        level = self._risk_of("element", verb=str(kind),
                              control_name=str(info.get("title", "")), app=app)

        refusal = self._host_refusal(pid, bool(params.get("allow_host_input")),
                                     f"окно {handle} {info.get('title', '')!r}",
                                     "window_action")
        if refusal is not None:
            refusal["_risk"] = level
            return refusal

        if params.get("dry_run"):
            return {
                "status": "ok",
                "dry_run": True,
                "_risk": level,
                "data": {
                    "handle": handle, "resolution": how, "path": f"window:{handle}",
                    "resolved_name": str(info.get("title", "")),
                    "resolved_pid": pid, "app": app,
                    "risk_level": level.level, "risk_reason": level.reason,
                    "batchable": level.batchable,
                    "message": (
                        f"DRY RUN: {kind!r} над окном {info.get('title', '')!r}; "
                        f"уровень риска {level.level}. Ничего не выполнено."
                    ),
                },
            }

        res = await asyncio.to_thread(handler, self.uia, handle, params)
        env = self._action_envelope(handle, how, res)
        env["_risk"] = level
        env["data"]["risk_level"] = level.level
        env["data"]["risk_reason"] = level.reason
        return env

    def _safe_target_info(self, handle: int) -> dict[str, Any]:
        """target_info that degrades to an empty dict instead of raising.

        A window can close between resolution and the call, and the guard and the
        classifier both run on every mutation. Losing the title there must not lose
        the action.
        """
        try:
            return self.uia.target_info(handle) or {}
        except Exception as exc:
            logger.debug("target_info(%s) failed: %s", handle, exc)
            return {}

    async def _action_read_terminal(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reads a console buffer through TextPattern and returns a slice of it.

        The whole buffer is reachable in one read -- measured at 9000 rows, about
        1.1 MB, in 30 ms -- which is far too much to hand back, so a slice is
        selected here. The default is the tail, because that is where the result of
        the last command is.
        """
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}

        buf = await asyncio.to_thread(self.uia.read_terminal, handle)
        if buf.error:
            return {"status": "error", "message": buf.error}

        lines = buf.lines
        contains = params.get("contains")
        matched = None
        if contains:
            matched = sum(1 for ln in lines if str(contains) in ln)
            lines = [ln for ln in lines if str(contains) in ln]

        head = params.get("head")
        start = params.get("start")
        count = params.get("count")
        tail = params.get("tail")
        if head:
            selected, mode = lines[: int(head)], "head"
        elif start is not None or count:
            s = int(start or 0)
            n = int(count or 200)
            selected, mode = lines[s:s + n], "range"
        else:
            n = int(tail or 200)
            selected, mode = lines[-n:] if n else [], "tail"

        warnings: list[str] = []
        if buf.buffer_full:
            warnings.append(
                f"Буфер консоли полон ({TERMINAL_BUFFER_ROWS} строк): самый старый "
                "вывод уже потерян безвозвратно. Для длинных команд читайте буфер "
                "по мере поступления, а не один раз в конце."
            )
        if matched == 0:
            warnings.append(
                f"Строка {contains!r} в буфере не найдена. Это не то же самое, что "
                "пустой буфер: прочитано строк — "
                f"{buf.row_count}."
            )

        return {
            "status": "ok",
            "data": {
                "handle": handle,
                "resolution": how,
                "host": buf.host,
                "row_count": buf.row_count,
                "char_count": buf.char_count,
                "buffer_full": buf.buffer_full,
                "read_ms": round(buf.read_ms, 1),
                "mode": mode,
                "selected_count": len(selected),
                "matched_count": matched,
                "lines": selected,
                "warnings": warnings,
            },
        }

    async def _action_write_terminal(self, params: dict[str, Any]) -> dict[str, Any]:
        """Types into a console through posted WM_CHAR. Class B: no cursor.

        Mutating, so it takes the input lock and stays blocked during USER_OVERRIDE.
        """
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}
        text = params.get("text")
        if text is None:
            return {"status": "error", "message": "Не указан text для записи."}

        expect = params.get("expect")

        # Guarded like every other mutation. A terminal owned by the host is the
        # agent's own tool channel: typing into it does not fail visibly, it changes
        # what the agent's next command does.
        pid = await asyncio.to_thread(host_guard.pid_of_window, handle)
        guard = self._host_refusal(pid, bool(params.get("allow_host_input")),
                                   f"терминал окна {handle}", "write_terminal")
        if guard is not None:
            return guard

        res = await asyncio.to_thread(
            lambda: self.uia.write_terminal(
                handle, str(text),
                enter=bool(params.get("enter", True)),
                expect=None if expect is None else str(expect),
                timeout_s=float(params.get("timeout_s") or 3.0),
            )
        )
        return {
            "status": "ok",
            "data": {
                "handle": handle, "resolution": how, "ok": res.ok,
                "verified": res.verified, "weak_verification": res.weak_verification,
                "input_hwnd": res.input_hwnd, "posted": res.posted,
                "chars": res.chars, "host": res.host, "message": res.message,
                "tail": res.tail,
            },
        }

    async def _action_wait_terminal(self, params: dict[str, Any]) -> dict[str, Any]:
        """Waits for text in a console buffer instead of sleeping a fixed time."""
        handle, how = await self._resolve_target(params)
        if handle is None:
            return {"status": "error", "message": how}
        expect = params.get("expect")
        if not expect:
            return {"status": "error",
                    "message": "Не указан expect — строка, которую нужно дождаться."}

        res = await asyncio.to_thread(
            lambda: self.uia.wait_terminal(
                handle, str(expect),
                timeout_s=float(params.get("timeout_s") or 30.0),
                interval_s=float(params.get("interval_s") or 0.25),
                after_text=params.get("after_text"),
            )
        )
        return {
            "status": "ok",
            "data": {
                "handle": handle, "resolution": how, "found": res.found,
                "elapsed_s": round(res.elapsed_s, 2), "polls": res.polls,
                "tail": res.tail, "message": res.message,
            },
        }

    # --------------------------------------------------------------- OCR path
    async def _ocr_capture(self, params: dict[str, Any]):
        """Recognizes a native-resolution capture, returning frame-space coordinates.

        Two concerns that must not be conflated. Recognition runs on the native
        capture, because the model-facing frame is scaled to a fixed width and that
        costs OCR roughly half its characters: measured at 76 lines and 2555
        characters natively versus 51 lines and 1358 at 1280 wide on the same screen,
        and zero lines on the 4480-wide virtual desktop. But the caller acts in frame
        space, so the recognized boxes are converted to it here rather than being
        handed back in native pixels.

        The grid overlay and cursor marker are off, since both draw over the text.
        """
        image, bbox, mon = await asyncio.to_thread(
            self.vision.capture_for_ocr, params.get("monitor_index")
        )
        page = await self.ocr.recognize_image(image)
        frame_w = config.TARGET_IMAGE_WIDTH
        if page.error:
            return page, frame_w, 0, mon
        scale_x = frame_w / float(bbox["width"])
        frame_h = round(bbox["height"] * scale_x)
        scale_y = frame_h / float(bbox["height"])
        # Origin is (0, 0), not the monitor's desktop offset. capture_for_ocr grabs
        # just this monitor, so the recognized boxes are already relative to its own
        # top-left corner; subtracting bbox left/top shifts them off the frame and
        # produces coordinates outside 0..frame_w. Caught by a bounds check, not by
        # comparing against read_screen_text, which used the same wrong offset and so
        # agreed with itself.
        page = page.scaled(0, 0, scale_x, scale_y)
        return page, frame_w, frame_h, mon

    @staticmethod
    def _ocr_envelope(mon, width: int, height: int, page) -> dict[str, Any]:
        return {
            "monitor": {"index": mon.index, "name": mon.name},
            "frame": [width, height],
            "language": page.language,
            "ocr_ms": round(page.elapsed_ms, 1),
            "line_count": len(page.lines),
        }

    async def _action_read_screen_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reads all recognized text on a monitor, with boxes, for opaque targets."""
        page, width, height, mon = await self._ocr_capture(params)
        if page.error:
            return {"status": "error", "message": page.error}

        limit = int(params.get("limit") or 120)
        with_words = bool(params.get("with_words"))
        selected = page.lines[:limit]
        data = self._ocr_envelope(mon, width, height, page)
        data["truncated"] = len(page.lines) > limit
        data["lines"] = [
            {
                "text": ln.text,
                "box": [ln.x, ln.y, ln.w, ln.h],
                "center": list(ln.center),
                **({"words": [{"text": w.text, "box": [w.x, w.y, w.w, w.h]}
                              for w in ln.words[:16]]} if with_words else {}),
            }
            for ln in selected
        ]
        return {"status": "ok", "data": data}

    async def _action_find_text_on_screen(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Locates recognized text and returns frame coordinates to click."""
        needle = params.get("text")
        if not needle or not str(needle).strip():
            return {"status": "error", "message": "Не указан text для поиска."}

        page, width, height, mon = await self._ocr_capture(params)
        if page.error:
            return {"status": "error", "message": page.error}

        fuzzy = bool(params.get("fuzzy", True))
        matches = page.find(str(needle), fuzzy=fuzzy)
        data = self._ocr_envelope(mon, width, height, page)
        data.update({
            "needle": str(needle),
            "fuzzy": fuzzy,
            "match_count": len(matches),
            "matches": [
                {"text": m.text, "kind": m.kind, "box": [m.x, m.y, m.w, m.h],
                 "center": list(m.center), "line_text": m.line_text}
                for m in matches[:40]
            ],
        })
        if not matches:
            # A miss here is evidence about the recognizer, not about the screen.
            data["hint"] = (
                "Совпадений нет, но это не доказательство отсутствия текста. "
                "Распознаватель ошибается в отдельных буквах: в замере латинское "
                "ALPHA было прочитано как кириллическое АЕРНА, а подчёркивания "
                "теряются. Нормализация уже сводит регистр, омоглифы и пунктуацию, "
                "но не исправляет подмену букв. Попробуйте более короткую строку, "
                "либо прочитайте экран целиком через read_screen_text и посмотрите, "
                "что распозналось на самом деле."
            )
        return {"status": "ok", "data": data}

    async def _action_wait_for_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Waits for text to appear on (or disappear from) screen, instead of sleeping.

        Polling is coarser than the terminal equivalent because each pass captures
        and recognizes a frame rather than reading a buffer.
        """
        needle = params.get("text")
        if not needle or not str(needle).strip():
            return {"status": "error", "message": "Не указан text для ожидания."}

        timeout_s = float(params.get("timeout_s") or 20.0)
        interval_s = max(float(params.get("interval_s") or 0.75), 0.15)
        appear = bool(params.get("appear", True))
        fuzzy = bool(params.get("fuzzy", True))

        started = time.monotonic()
        polls = 0
        matches: list = []
        envelope: dict[str, Any] = {}
        error = None
        while True:
            polls += 1
            page, width, height, mon = await self._ocr_capture(params)
            envelope = self._ocr_envelope(mon, width, height, page)
            if page.error:
                error = page.error
            else:
                error = None
                matches = page.find(str(needle), fuzzy=fuzzy)
                if bool(matches) == appear:
                    envelope.update({
                        "found": True, "elapsed_s": round(time.monotonic() - started, 2),
                        "polls": polls, "needle": str(needle), "appear": appear,
                        "match_count": len(matches),
                        "matches": [{"text": m.text, "box": [m.x, m.y, m.w, m.h],
                                     "center": list(m.center)}
                                    for m in matches[:10]],
                    })
                    return {"status": "ok", "data": envelope}
            if time.monotonic() - started >= timeout_s:
                break
            await asyncio.sleep(interval_s)

        envelope.update({
            "found": False, "elapsed_s": round(time.monotonic() - started, 2),
            "polls": polls, "needle": str(needle), "appear": appear,
            "match_count": 0, "error": error,
            "message": (
                f"За {timeout_s:.0f} с текст {str(needle)!r} "
                + ("не появился" if appear else "не исчез")
                + f" ({polls} опросов)."
                + (f" Последняя ошибка распознавания: {error}" if error else "")
            ),
        })
        return {"status": "ok", "data": envelope}

    async def _action_hud_state(self) -> dict[str, Any]:
        """Reports live overlay geometry so frame placement is checkable, not assumed.

        The overlay carries WDA_EXCLUDEFROMCAPTURE, so a mis-placed frame never
        appears in any captured screenshot. These numbers are the only
        machine-checkable evidence that the border is where it should be:
        window origin plus target_local must equal target_global.
        """
        diag = self.overlay.diagnostics()
        window = diag["window"]
        local = diag["target_local"]
        target_global = diag["target_global"]

        painted_at = None
        if local is not None:
            painted_at = [window[0] + local[0], window[1] + local[1]]

        placement_correct = (
            painted_at is not None
            and target_global is not None
            and painted_at[0] == target_global[0]
            and painted_at[1] == target_global[1]
        )

        return {
            "status": "ok",
            "state": self.input_ctrl.state.value,
            "data": {
                **diag,
                "painted_at_global": painted_at,
                "placement_correct": placement_correct,
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
    loop.create_task(daemon.idle_resume_loop())

    try:
        loop.run_forever()
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    main()

