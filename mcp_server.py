"""MCP gateway connecting LLM tools to the Autopilot Daemon.

Requires MCP SDK v2 (``mcp>=2.0,<3``). The former FastMCP fallback is gone:
``mcp.server.fastmcp`` no longer exists in v2 and the branch could only ever
raise, silently masking a real installation problem.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Literal

import websockets
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

import config

# Must run before logging.basicConfig: StreamHandler captures sys.stderr at
# construction time, so the encoding has to be correct first.
config.configure_stdio_encoding()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [MCPGateway]: %(message)s",
)
logger = logging.getLogger("MCPGateway")

mcp = MCPServer("desktop-autopilot")

# The explicit union annotation is load-bearing, not decoration.
#
# Annotating a tool as ``-> list[Any]`` makes the SDK publish an output_schema
# for it and then serialise the *entire* return value into ``structured_content``
# in addition to ``content``. For screenshot-bearing tools that means the base64
# frame is transmitted twice -- measured at exactly 2.00x on mcp 2.3.0 -- and a
# host that surfaces ``structured_content`` to the model injects the raw base64
# into context as text.
#
# Any change to a tool's return annotation must be re-measured, not assumed.
#
# The same doubling applies to a plain ``-> str`` return, which the SDK treats as
# structured by default: it publishes ``{name}Output`` with a single ``result``
# field and emits the whole string again as ``structured_content``. For a tool
# that returns one short label this is harmless. For a tool that returns a tree,
# a console buffer or an OCR page it doubles the token cost of every call, on
# exactly the tools called most often.
#
# So the four highest-volume text tools pass ``structured_output=False``. The
# rule is about payload size, not about the annotation: a short string keeps the
# schema.
ContentBlock = TextContent | ImageContent


def _read_auth_token() -> str:
    """Reads session token from environment or local file."""
    token_env = os.environ.get("AUTOPILOT_TOKEN")
    if token_env:
        return token_env.strip()

    if config.TOKEN_FILE_PATH.exists():
        return config.TOKEN_FILE_PATH.read_text(encoding="utf-8").strip()

    return ""


async def _send_daemon_request(
    action: str,
    params: dict[str, Any],
    timeout: float | None = None,
) -> dict[str, Any]:
    """Sends a JSON command to the local autopilot daemon over WebSocket.

    Every await is bounded. An unbounded ``recv()`` turns a hung daemon -- a UIA
    tree walk on a large Electron app is the usual cause -- into a tool call that
    never returns, which the host eventually kills with a useless error.

    ``timeout`` overrides the per-action table. Waiting tools need it: their
    deadline is the caller's, not the daemon's, and a transport timeout that
    equals the polling deadline races it. Losing that race replaces the
    structured "did not arrive within N s, here is the buffer" answer with a
    generic transport error, which is strictly less information at the moment
    the caller most needs it.
    """
    token = _read_auth_token()
    uri = f"ws://{config.WS_HOST}:{config.WS_PORT}"
    if timeout is None:
        timeout = config.ws_timeout_for(action)

    req_id = str(uuid.uuid4())[:8]
    payload = {
        "id": req_id,
        "token": token,
        "action": action,
        "params": params,
        # Who is asking. The daemon walks the process tree from this PID itself and
        # refuses input into any window the host owns. Sending the PID rather than a
        # PID list is deliberate: a list the client assembled could be incomplete,
        # and an incomplete list is a guard with holes that still looks like a guard.
        "host_pid": os.getpid(),
    }

    try:
        async with websockets.connect(
            uri,
            ping_interval=config.WS_HEARTBEAT_INTERVAL,
            open_timeout=config.WS_OPEN_TIMEOUT,
            close_timeout=5.0,
        ) as ws:
            await ws.send(json.dumps(payload))
            raw_resp = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(raw_resp)
    except TimeoutError:
        return {
            "status": "error",
            "message": (
                f"Демон не ответил на действие '{action}' за {timeout:.0f} с. "
                "Вероятная причина: зависшая инспекция дерева доступности или "
                "захвата экрана. Повторите с меньшим окном или увеличьте "
                "WS_ACTION_TIMEOUTS для этого действия."
            ),
        }
    except OSError:
        return {
            "status": "error",
            "message": (
                f"Не удалось подключиться к демону по адресу {uri}. "
                "Убедитесь, что запущен процесс 'autopilot-daemon' (или start_daemon.bat)."
            ),
        }
    except json.JSONDecodeError as exc:
        return {"status": "error", "message": f"Демон вернул не-JSON: {exc}"}
    except Exception as exc:
        return {"status": "error", "message": f"Ошибка связи с демоном: {type(exc).__name__}: {exc}"}


def _text(text: str) -> TextContent:
    """Builds a TextContent block."""
    return TextContent(type="text", text=text)


def _format_image_and_text(resp: dict[str, Any], default_text: str = "") -> list[ContentBlock]:
    """Constructs MCP ImageContent and TextContent responses."""
    status = resp.get("status", "error")
    data = resp.get("data", {})
    state = resp.get("state", "UNKNOWN")

    if status == "unauthorized":
        msg = f"ОШИБКА АВТОРИЗАЦИИ: Неверный токен сессии. Проверьте файл {config.TOKEN_FILE_PATH}."
        return [_text(msg)]

    if status == "interrupted":
        msg = resp.get("message") or data.get("message") or "Действие прервано пользователем."
        return [_text(f"ВНИМАНИЕ: {msg}")]

    if status != "ok":
        err_msg = resp.get("message", "Неизвестная ошибка")
        return [_text(f"ОШИБКА: {err_msg}")]

    results: list[ContentBlock] = []

    screenshot_b64 = data.get("screenshot")
    if screenshot_b64:
        try:
            results.append(
                ImageContent(type="image", data=screenshot_b64, mime_type="image/webp")
            )
        except Exception as exc:
            logger.warning(f"Failed to process screenshot data: {exc}")

    w = data.get("scaled_width")
    h = data.get("scaled_height")
    cursor = data.get("cursor")
    cursor_desktop = data.get("cursor_desktop")
    msg = data.get("message", default_text)

    summary_lines: list[str] = []
    if data.get("session_warning"):
        # First, before anything else. A frame of the lock screen must not be
        # mistaken for a frame of the target application.
        summary_lines.append(f"ВНИМАНИЕ: {data['session_warning']}")
    summary_lines.append(f"Статус демона: {state}")
    if msg:
        summary_lines.append(f"Результат: {msg}")
    if w and h:
        summary_lines.append(f"Разрешение кадра: {w}x{h} px")
    if cursor:
        summary_lines.append(f"Позиция курсора на кадре: ({cursor[0]}, {cursor[1]})")
    elif cursor_desktop:
        # A null cursor means the pointer is on a different monitor. Saying so
        # beats omitting it: the agent otherwise assumes the click landed.
        summary_lines.append(
            f"Курсор вне этого кадра: desktop ({cursor_desktop[0]}, {cursor_desktop[1]}). "
            "Передайте monitor_index того монитора, на котором он находится."
        )

    results.append(_text("\n".join(summary_lines)))
    return results


@mcp.tool()
async def take_control(
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Takes control of the desktop session for the agent: lights a persistent neon frame
    around the chosen monitor. Call this first, before any interactive action.

    The frame is drawn on the agent's own overlay window, which carries
    WDA_EXCLUDEFROMCAPTURE, so it never appears in captured frames. Use get_hud_state
    to verify where it actually landed.

    Args:
        monitor_index: 1 = primary monitor, 0 = every display as one virtual desktop,
            None = automatic. An out-of-range index is an error, not a silent fallback,
            because falling back to 0 changes the frame scale and invalidates every
            coordinate accumulated so far.
        show_grid: Overlay a 100px coordinate grid. Off by default; the grid draws over
            the title-bar area where window controls live.
    """
    params = {"monitor_index": monitor_index, "show_grid": show_grid}
    resp = await _send_daemon_request("take_control", params)
    return _format_image_and_text(resp, "Управление рабочим столом успешно захвачено агентом.")


@mcp.tool()
async def release_control() -> str:
    """
    Releases desktop control and turns the persistent HUD frame off.

    Always available, including while USER_OVERRIDE is active. Before that was made
    explicit, a human interruption blocked this call too, so a pause left the neon
    frame lit with no way out short of reset_user_override or killing the daemon.

    Call it at the end of the whole composite task, not between steps.
    """
    resp = await _send_daemon_request("release_control", {})
    if resp.get("status") == "ok":
        return "Управление освобождено. Неоновый оверлей выключен."
    return f"Ошибка при освобождении управления: {resp.get('message', 'Неизвестная ошибка')}"


@mcp.tool()
async def get_screen_state(
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Requests a fresh screen capture plus system state (IDLE, AI_ACTIVE, USER_OVERRIDE),
    frame resolution, and the cursor position.

    The response may carry a session warning. When the workstation is locked, capture
    still succeeds and returns the lock screen; the warning is the only thing that
    distinguishes that from a frame of the target application.

    Args:
        monitor_index: 1 = primary, 0 = every display as one virtual desktop,
            None = automatic.
        show_grid: Overlay a 100px coordinate grid. Off by default.
    """
    resp = await _send_daemon_request(
        "get_screen_state", {"monitor_index": monitor_index, "show_grid": show_grid}
    )
    return _format_image_and_text(resp, "Снимок экрана успешно получен.")


@mcp.tool()
async def mouse_action(
    action: Literal["click", "double_click", "right_click", "middle_click",
                    "triple_click", "move", "drag", "mouse_down", "mouse_up"],
    x: int,
    y: int,
    end_x: int | None = None,
    end_y: int | None = None,
    modifiers: list[Literal["ctrl", "shift", "alt", "win"]] | None = None,
    button: Literal["left", "right", "middle"] | None = None,
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Performs a mouse action with a predictive visual marker and human-interruption check.

    This is a Class A action: it consumes the session's single cursor, so it cannot run
    concurrently with the human using the machine. A physical mouse movement or keypress
    switches the daemon to USER_OVERRIDE and aborts the action mid-flight. Prefer
    element-based actions when the target exposes them.

    Coordinates are in the downscaled frame, not physical pixels. Coordinates outside
    that frame are rejected with an error rather than clamped: a click at invalid
    coordinates would land at a predictable point unrelated to the request, and the
    response used to report success while doing it.

    The response reports where the cursor actually ended up, which differs from the
    requested point whenever a move was clipped or interrupted -- that difference is
    the signal a click failed.

    Args:
        action: 'click', 'double_click', 'right_click', 'middle_click', 'triple_click',
            'move', 'drag', 'mouse_down' or 'mouse_up'.
        x: X in the downscaled frame (0..scaled_width).
        y: Y in the downscaled frame (0..scaled_height).
        end_x: Destination X for 'drag'.
        end_y: Destination Y for 'drag'.
        modifiers: Keys held across a click, for shift+click range selection and
            ctrl+click toggling. Pressed before the button goes down and released after
            it comes up, which is what makes the combination a single gesture rather than
            a click followed by a stray keystroke.
        button: Which button 'mouse_down' and 'mouse_up' act on. Defaults to 'left'.
        monitor_index: Which monitor the frame coordinates refer to.
        show_grid: Overlay a grid on the resulting frame. Off by default.
    """
    params = {
        "action": action,
        "x": x,
        "y": y,
        "end_x": end_x,
        "end_y": end_y,
        "modifiers": modifiers,
        "button": button,
        "monitor_index": monitor_index,
        "show_grid": show_grid,
    }
    resp = await _send_daemon_request("mouse_action", params)
    return _format_image_and_text(resp)


@mcp.tool()
async def keyboard_action(
    text: str | None = None,
    hotkey: str | None = None,
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Types text or presses a key combination.

    Text is injected per character via KEYEVENTF_UNICODE, not pasted from the clipboard.
    That means it is independent of the RU/EN keyboard layout, it does not touch the
    user's clipboard, and it works where Ctrl+V is not paste (vim, ssh sessions, some
    terminals). The clipboard is used only above CLIPBOARD_TYPING_THRESHOLD characters
    and only as a fallback; it destroys non-text clipboard formats.

    This is a Class A action: it consumes the session keyboard, so a physical keypress
    switches the daemon to USER_OVERRIDE and aborts typing mid-flight.

    Args:
        text: Literal text to type. Newline and tab are sent as their virtual keys.
        hotkey: Key combination such as 'ctrl+c', 'alt+tab', 'enter', 'esc', 'f5'.
            Every token must be recognized. An unknown token aborts the whole
            combination and sends nothing, because skipping unknown tokens once
            caused malformed input to press a real key.
        monitor_index: Which monitor the returned frame should show.
        show_grid: Overlay a grid on the resulting frame. Off by default.
    """
    params = {
        "text": text,
        "hotkey": hotkey,
        "monitor_index": monitor_index,
        "show_grid": show_grid,
    }
    resp = await _send_daemon_request("keyboard_action", params)
    return _format_image_and_text(resp)


@mcp.tool()
async def scroll_screen(
    direction: Literal["up", "down", "left", "right"] = "down",
    amount: int = 3,
    x: int | None = None,
    y: int | None = None,
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Scrolls the screen with the mouse wheel and waits for the image to settle.

    Class A action: consumes the session cursor. Only vertical direction is supported;
    horizontal scrolling is not implemented.

    Args:
        direction: 'up' or 'down'.
        amount: Number of wheel steps.
        x: Optional X in the downscaled frame to move the cursor to first.
        y: Optional Y in the downscaled frame to move the cursor to first.
        monitor_index: Which monitor the frame coordinates refer to.
        show_grid: Overlay a grid on the resulting frame. Off by default.
    """
    params = {
        "direction": direction,
        "amount": amount,
        "x": x,
        "y": y,
        "monitor_index": monitor_index,
        "show_grid": show_grid,
    }
    resp = await _send_daemon_request("scroll_screen", params)
    return _format_image_and_text(resp)


@mcp.tool()
async def list_windows_and_monitors() -> str:
    """
    Lists connected monitors and open application windows with their bounds.

    Use this to decide what to target, then pass a window's exact handle to
    get_ui_elements. Handles are listed because titles are ambiguous: one application
    can own several top-level windows with the same title.

    Each window reports its Win32 class and pid, which disambiguate when titles collide,
    and an on_monitor flag. on_monitor=False means the window's centre fell in a gap
    between displays -- monitors do not necessarily tile the virtual desktop -- so its
    monitor_index is the nearest screen rather than the containing one.
    """
    resp = await _send_daemon_request("list_windows_and_monitors", {})
    if resp.get("status") != "ok":
        return f"Ошибка получения списка окон: {resp.get('message', 'Неизвестная ошибка')}"

    data = resp.get("data", {})
    monitors = data.get("monitors", [])
    windows = data.get("windows", [])

    output = []
    if data.get("session_warning"):
        output.append(f"ВНИМАНИЕ: {data['session_warning']}")
        output.append("")
    output.append("=== ПОДКЛЮЧЕННЫЕ МОНИТОРЫ ===")
    for m in monitors:
        primary_mark = " [PRIMARY]" if m.get("is_primary") else ""
        if m["index"] == 0:
            primary_mark += " [весь виртуальный стол, не отдельный экран]"
        output.append(f"Монитор {m['index']}: '{m['name']}' границы={m['bounds']}{primary_mark}")

    output.append("\n=== ОТКРЫТЫЕ ОКНА ПРОГРАММ ===")
    for w in windows:
        marks = ""
        if w.get("is_active"):
            marks += " [ACTIVE]"
        if w.get("on_monitor") is False:
            # Center point landed in a gap between monitors, so the index is the
            # nearest screen rather than the containing one.
            marks += " [вне мониторов: индекс приблизительный]"
        output.append(
            f"- handle={w.get('handle')} '{w['title']}'"
            f" (Монитор {w['monitor_index']}, границы={w['bounds']}){marks}"
        )
        if w.get("class_name") or w.get("pid"):
            output.append(
                f"    class='{w.get('class_name', '')}' pid={w.get('pid')}"
            )

    return "\n".join(output)


# Unstructured on purpose: up to 60 elements, each a full line. See ContentBlock.
@mcp.tool(structured_output=False)
async def get_ui_elements(
    window_title: str | None = None,
    monitor_index: int | None = None,
    hwnd: int | None = None,
    class_name: str | None = None,
) -> str:
    """
    Inspects a window's interactive UI elements (buttons, edits, checkboxes) through
    Windows UI Automation and returns them with click coordinates pre-scaled to the frame.

    A title alone does not identify a window. One application can own several top-level
    windows with the same title -- a main window plus notification popups -- and picking
    the wrong one yields an empty element list that looks exactly like an application
    exposing no accessibility tree at all. Pass hwnd to remove all doubt.

    The response always states which window was chosen and how, so an empty result is
    distinguishable from a mis-resolved target. Resolution is one of: 'hwnd',
    'unique', 'largest-of-N' (the title matched several windows and the largest by area
    won), 'win32-title' (matched by the Win32 title rather than the UIA name -- these are
    different strings for the same window), or 'foreground'.

    Elements whose centre lies outside the given monitor are counted in
    skipped_off_monitor rather than returned with a negative click_target.

    Args:
        window_title: Substring of the window title.
        monitor_index: Which monitor's frame the coordinates are scaled to.
        hwnd: Exact handle from list_windows_and_monitors. Takes priority over the title.
        class_name: Win32 window class, narrows a title search.
    """
    params = {
        "window_title": window_title,
        "monitor_index": monitor_index,
        "hwnd": hwnd,
        "class_name": class_name,
    }
    resp = await _send_daemon_request("get_ui_elements", params)
    if resp.get("status") != "ok":
        return f"Ошибка инспекции элементов: {resp.get('message', 'Неизвестная ошибка')}"

    data = resp.get("data", {})
    elements = data.get("elements", [])
    skipped = data.get("skipped_off_monitor", 0)
    mon = data.get("monitor", {})
    frame = data.get("frame", [])
    tgt = data.get("target") or {}

    header: list[str] = []
    if data.get("session_warning"):
        header.append(f"ВНИМАНИЕ: {data['session_warning']}")
    header.append(f"Найдено интерактивных элементов: {len(elements)}")
    if tgt:
        header.append(
            f"Целевое окно: handle={tgt.get('handle')} '{tgt.get('title')}' "
            f"class='{tgt.get('class_name')}' pid={tgt.get('pid')} "
            f"границы={tgt.get('bounds')} выбрано={tgt.get('resolution')}"
        )
    if mon:
        header.append(
            f"Монитор {mon.get('index')} '{mon.get('name')}' "
            f"границы={mon.get('bounds')} кадр={frame}"
        )
    if data.get("ambiguous_title") and data.get("hint_ambiguous"):
        header.append(data["hint_ambiguous"])
    if skipped:
        header.append(f"Пропущено вне этого монитора: {skipped}")
        if data.get("hint"):
            header.append(data["hint"])
    if data.get("warning"):
        header.append(f"Предупреждение: {data['warning']}")

    if not elements:
        detail = "\n".join(header)
        return (
            f"Интерактивные элементы UI не найдены или окно не поддерживает Accessibility.\n{detail}"
        )

    output = list(header)
    for el in elements:
        title = el.get("title") or el.get("automation_id") or "Без названия"
        c_type = el.get("control_type", "Control")
        target = el.get("click_target", [0, 0])
        output.append(f"[{c_type}] '{title}' -> click_target: x={target[0]}, y={target[1]}")

    return "\n".join(output)


@mcp.tool()
async def probe_target(
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
) -> str:
    """
    Classifies a target window before a control path is chosen, and says which path it supports.

    Call this before deciding between the accessibility tree and pixel coordinates.
    It reports the target's classification, the pattern inventory found inside the
    target, the measured cost per element in microseconds, how many nodes a trial
    walk found, whether the window is minimized, and a concrete recommendation.

    Classification is based on a trial walk, not on first-level fan-out. Chromium
    and Electron place a view chain four to six levels deep above the real content,
    so a window reporting two children at depth one can still expose hundreds of
    usable nodes.

    Classifications:
      'terminal'  - a console host. Readable through TextPattern and writable through
                    WM_CHAR to its input child window, both without using the cursor.
                    ValuePattern reports IsReadOnly=0 there and then does nothing.
      'rich'      - a usable accessibility tree. Semantic path, Class B.
      'partial'   - tree present but names sparsely populated. Match on role and
                    position as well as text.
      'opaque'    - no usable tree. Pixel path, which means Class A and the cursor.
      'expensive' - walking costs orders of magnitude more than a normal window, so
                    any snapshot will be incomplete. Use a small max_nodes.

    Cost per element varies by a factor of 100 between targets (217 us on one
    browser page, 16.9 ms on another, 41 ms on a Qt window), which is why it is
    measured here rather than assumed.

    Args:
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
    """
    resp = await _send_daemon_request("probe_target", {
        "hwnd": hwnd, "window_title": window_title, "class_name": class_name,
    })
    if resp.get("status") != "ok":
        return f"Ошибка обследования цели: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    lines = [
        f"Цель: handle={d.get('handle')} '{d.get('title')}' "
        f"class='{d.get('class_name')}' pid={d.get('pid')} выбрано={d.get('resolution')}",
        f"Классификация: {d.get('classification')}",
        f"Элементов первого уровня: {d.get('child_count')}  "
        f"узлов в пробном обходе: {d.get('tree_sample')} "
        f"(пригодных {d.get('useful_nodes')})  "
        f"площадь окна: {d.get('area')} px²  "
        f"стоимость узла: {d.get('us_per_node')} мкс  свёрнуто: {d.get('iconic')}",
    ]
    patterns = d.get("patterns") or []
    lines.append(
        "Паттерны цели: "
        + (", ".join(patterns[:14]) if patterns else "не обнаружено")
    )
    for note in d.get("notes") or []:
        lines.append(f"Замечание: {note}")
    lines.append(f"Рекомендация: {d.get('recommendation')}")
    return "\n".join(lines)


# Unstructured on purpose: up to 150 nodes per page. See ContentBlock.
@mcp.tool(structured_output=False)
async def get_snapshot(
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    monitor_index: int | None = None,
    max_nodes: int | None = None,
    max_depth: int | None = None,
    time_budget_ms: int | None = None,
    token: str | None = None,
    auto_restore: bool = False,
) -> str:
    """
    Returns a bounded semantic snapshot of a window's accessibility tree.

    This is the primary observation path. It does not use the cursor, works on
    inactive and occluded windows, and does not depend on the screenshot scale.
    Coordinates in the output are in frame space (width 1280), ready for
    mouse_action, and elements outside the given monitor are marked rather than
    given a negative coordinate.

    The snapshot is deliberately incomplete whenever the target is large. The
    header says which limit was hit and, for time and node limits, gives a token to
    continue from. Pass that token back to page through the rest. Truncation by
    depth cannot be resumed -- raise max_depth instead.

    Each line is: path [role] 'text' @x,y {patterns}. The path is a stable
    sibling-index address and is what resolve_clickable takes. Text annotated as
    class-sourced is a control class name, not UI content: good for telling
    elements apart, not for identifying a message recipient.

    Args:
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        monitor_index: Which monitor's frame the coordinates are scaled to.
        max_nodes: Node cap for this page (default 400).
        max_depth: Depth cap (default 40). Raising it is the only way past a
            depth truncation, which unlike the other two is terminal -- it emits
            no continuation token.
        time_budget_ms: Wall-clock budget for the walk (default 150). This is the
            primary limit, because cost per node varies by a factor of 100 between
            targets.
        token: Continuation token from a previous truncated snapshot.
        auto_restore: Restore the window first if it is minimized. Off by default
            because it changes the user's desktop.
    """
    resp = await _send_daemon_request("get_snapshot", {
        "hwnd": hwnd, "window_title": window_title, "class_name": class_name,
        "monitor_index": monitor_index, "max_nodes": max_nodes,
        "max_depth": max_depth, "time_budget_ms": time_budget_ms,
        "token": token, "auto_restore": auto_restore,
    })
    if resp.get("status") != "ok":
        return f"Ошибка снимка дерева: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    session_note = (
        f"ВНИМАНИЕ: {d['session_warning']}" if d.get("session_warning") else None
    )
    header = [session_note] if session_note else []
    header.append(
        f"Цель: handle={d.get('handle')} выбрано={d.get('resolution')}  "
        f"монитор {d.get('monitor', {}).get('index')} кадр={d.get('frame')}"
    )
    header.append(
        f"Узлов: {d.get('node_count')} (действующих {d.get('actionable_count')})  "
        f"время {d.get('elapsed_ms')} мс  запросов {d.get('queries')}  "
        f"отказов {d.get('failed_queries')}"
    )
    if d.get("monitor_hint"):
        header.append(f"НЕВЕРНЫЙ МОНИТОР: {d['monitor_hint']}")
    if d.get("minimized"):
        header.append(
            "ОКНО СВЁРНУТО: дерево читается, но координаты недействительны и захват "
            "вернёт чёрный кадр. Разверните окно перед действием по координатам."
        )
    if d.get("auto_restored"):
        header.append("Окно было развёрнуто автоматически.")
    for w in (d.get("warnings") or [])[:4]:
        header.append(f"Внимание: {w}")

    tree = d.get("tree") or ""
    nodes = d.get("nodes") or []
    offscreen_actionable = sum(
        1 for n in nodes if n.get("frame_point") is None and n.get("abs_point")
    )
    footer = []
    if offscreen_actionable:
        footer.append(
            f"{offscreen_actionable} действующих элементов лежат вне этого кадра; "
            "повторите с monitor_index их монитора."
        )
    return "\n".join([*header, "", tree, *footer])


@mcp.tool()
async def resolve_clickable(
    path: str,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    monitor_index: int | None = None,
) -> str:
    """
    Resolves the precise clickable point of one snapshot node, in frame coordinates.

    A snapshot carries a node's rectangle centre, because asking UIA for its own
    clickable point is a live round trip costing 162 us per node -- 26% of a walk.
    Call this for the single element you are about to act on. It is also fresher
    than the snapshot value, and it returns None rather than a stale coordinate
    when the element is offscreen, minimized, or the tree has changed.

    Args:
        path: The node's path from a snapshot, e.g. '1.0.2'.
        hwnd: Exact handle of the window the snapshot came from.
        window_title: Substring of the window title.
        class_name: Win32 window class.
        monitor_index: Which monitor's frame to scale the point into.
    """
    resp = await _send_daemon_request("resolve_clickable", {
        "path": path, "hwnd": hwnd, "window_title": window_title,
        "class_name": class_name, "monitor_index": monitor_index,
    })
    if resp.get("status") != "ok":
        return f"Точка клика не получена: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    fp = d.get("frame_point")
    if fp is None:
        containing = d.get("containing_monitor")
        where = (
            f"Повторите с monitor_index={containing}." if containing is not None
            else "Повторите с monitor_index того монитора, на котором лежит элемент."
        )
        return (
            f"Узел {d.get('path')} находится вне монитора {d.get('monitor_index')}. "
            + where
        )
    return (
        f"Узел {d.get('path')}: точка клика в кадре x={fp[0]}, y={fp[1]} "
        f"(абсолютные {d.get('abs_point')}, монитор {d.get('monitor_index')})."
    )


ELEMENT_ACTIONS = Literal[
    "invoke", "set_value", "toggle", "select", "expand", "collapse",
    "scroll_into_view", "focus", "set_range_value",
]
WINDOW_ACTIONS = Literal[
    "close", "minimize", "maximize", "restore", "move", "resize", "activate",
]
# Named event sets for wait_for_event. Kept in step with event_waiter.CONDITIONS by a
# test, because the two live in different modules with no import linking them: a name
# the gateway offers but the daemon does not know returns "unknown condition", and one
# the daemon accepts but the gateway does not offer is simply unreachable.
EVENT_CONDITIONS = Literal[
    "opened", "closed", "focused", "value_changed", "tree_changed", "appeared",
]


@mcp.tool()
async def act_on_element(
    action: ELEMENT_ACTIONS,
    path: str,
    text: str | None = None,
    value: float | None = None,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    dry_run: bool = False,
    allow_host_input: bool = False,
) -> str:
    """
    Acts on a snapshot node through a UIA pattern, without using the cursor.

    This is the Class B path: the window need not be focused or visible, and the
    human can keep using the machine. Prefer it over mouse_action and
    keyboard_action whenever the node's patterns allow it, because it is both more
    reliable and parallel-safe.

    Every action is confirmed by reading the element back, except invoke, which has
    no readable state of its own. The result says plainly which happened. A
    'verified: False' outcome means the call was accepted and the state did not
    change -- that is a real failure mode, not a theoretical one: on a terminal
    ValuePattern.SetValue reports IsReadOnly=0, accepts the call, and changes
    nothing at all.

    Actions:
      'invoke'           - press a button or menu item. Not verifiable; confirm the
                           effect by observing afterwards.
      'set_value'        - write text into an edit or value field. Requires `text`.
      'toggle'           - flip a checkbox or toggle button; before/after returned.
      'select'           - select a list, tree or tab item.
      'expand'           - open a combo box or tree node.
      'collapse'         - close one.
      'scroll_into_view' - scroll a node marked off-screen into view, then re-read its
                           coordinates. Verified through IsOffscreen.
      'focus'            - give a node the keyboard focus without moving the mouse, so
                           typed text lands there. Verified through HasKeyboardFocus.
      'set_range_value'  - set a slider or spinner. Requires `value`, a number inside
                           the element's own range; an out-of-range value is refused
                           rather than clamped.

    Args:
        action: One of the nine above.
        path: The node's path from a get_snapshot result, e.g. '1.0.2'.
        text: The value to write, for 'set_value' only.
        value: The number to set, for 'set_range_value' only.
        hwnd: Exact handle of the window the snapshot came from.
        window_title: Substring of the window title.
        class_name: Win32 window class.
        dry_run: Resolve and classify the target, report, change nothing.
        allow_host_input: Permit input into the host's own process tree. Refused by
            default, because the window that renders the conversation belongs to it
            and typing there becomes a message the agent then reads as user input.
    """
    resp = await _send_daemon_request("element_action", {
        "action": action, "path": path, "text": text, "value": value,
        "hwnd": hwnd, "window_title": window_title, "class_name": class_name,
        "dry_run": dry_run, "allow_host_input": allow_host_input,
    })
    if resp.get("status") == "refused":
        return _format_refusal(resp)
    if resp.get("status") != "ok":
        return f"Действие не выполнено: {resp.get('message', 'Неизвестная ошибка')}"
    if resp.get("dry_run"):
        return _format_dry_run(resp)

    return _format_class_b_result(resp, target="узле")


def _format_refusal(resp: dict[str, Any]) -> str:
    """Renders a guard refusal so the model knows not to work around it.

    Told apart from a plain error on purpose. An error says the action failed and may
    be retried with different parameters; a refusal says this target is off limits,
    and retrying it is not a solution.
    """
    return (
        f"ОТКАЗАНО СЛОЕМ БЕЗОПАСНОСТИ ({resp.get('guard', 'guard')}): "
        f"{resp.get('message', '')}"
    )


def _format_dry_run(resp: dict[str, Any]) -> str:
    """Renders a dry run: what would be acted on, and how it was classified."""
    d = resp.get("data", {})
    lines = [
        "DRY RUN — ничего не выполнено.",
        f"  цель: {d.get('resolved_name') or d.get('app') or '?'} "
        f"(pid {d.get('resolved_pid')}, приложение {d.get('app') or '?'})",
        f"  уровень риска: {d.get('risk_level')} — {d.get('risk_reason')}",
        f"  пригодно для пакета: {'да' if d.get('batchable') else 'нет'}",
    ]
    if d.get("message"):
        lines.append(f"  {d['message']}")
    return "\n".join(lines)


@mcp.tool()
async def window_action(
    action: WINDOW_ACTIONS,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    dry_run: bool = False,
    allow_host_input: bool = False,
) -> str:
    """
    Acts on a whole window through UIA, without using the cursor or taking focus.

    Class B throughout, so this runs against an inactive or occluded window and does
    not disturb the human. Two of these are prerequisites for observation rather than
    conveniences: a minimized window captures as a black frame that looks exactly like
    an empty one, so restore it before any screenshot, and a window left open by a
    failed close usually has a modal dialog on it that has to be answered first.

    Actions:
      'restore'   - bring a minimized or maximized window back to normal.
      'minimize'  - minimize it.
      'maximize'  - maximize it.
      'close'     - request a close. Verified by the handle ceasing to exist, so an
                    application that answers with an unsaved-changes dialog reports
                    "still open" instead of a false success.
      'move'      - move to screen coordinates. Requires `x` and `y`.
      'resize'    - resize in pixels. Requires `width` and `height`.
      'activate'  - bring to the foreground. Measured: a background process cannot do
                    this with SetForegroundWindow alone -- it returned false and brought
                    nothing forward 2 times out of 2 -- so this attaches the input
                    threads first and confirms through GetForegroundWindow, because the
                    API's return value is nonzero even when all it did was flash the
                    taskbar button. A minimized window is refused with a pointer to
                    'restore': activation does not un-minimize, and reporting success
                    would describe a window that is still an icon.

    'move' and 'resize' are confirmed against the window rectangle with a few pixels of
    tolerance, because Transform positions the bounding rectangle while some window
    managers place the client area a border-width away. A window reporting CanMove=0 or
    CanResize=0 refuses the call rather than reporting a change that did not happen.

    Args:
        action: One of the six above.
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        x: Target left edge in physical screen pixels, for 'move'.
        y: Target top edge in physical screen pixels, for 'move'.
        width: Target width in pixels, for 'resize'.
        height: Target height in pixels, for 'resize'.
        dry_run: Resolve and classify the target, report, change nothing.
        allow_host_input: Permit acting on a window owned by the host's own process
            tree. Refused by default.
    """
    resp = await _send_daemon_request("window_action", {
        "action": action, "hwnd": hwnd, "window_title": window_title,
        "class_name": class_name, "x": x, "y": y, "width": width, "height": height,
        "dry_run": dry_run, "allow_host_input": allow_host_input,
    })
    if resp.get("status") == "refused":
        return _format_refusal(resp)
    if resp.get("status") != "ok":
        return f"Действие не выполнено: {resp.get('message', 'Неизвестная ошибка')}"
    if resp.get("dry_run"):
        return _format_dry_run(resp)

    return _format_class_b_result(resp, target="окне")


def _format_class_b_result(resp: dict[str, Any], target: str) -> str:
    """Renders one Class B ActionResult into the four statuses the model needs.

    Shared by element and window verbs so both report verification the same way. The
    distinction that matters is the middle one: `verified: None` means the action has
    no readable state, so success is only "the call was accepted" -- a materially
    weaker claim that must not read like confirmation.
    """
    d = resp.get("data", {})
    ok = d.get("ok")
    verified = d.get("verified")
    if ok and verified is True:
        status = "ВЫПОЛНЕНО И ПОДТВЕРЖДЕНО"
    elif ok and verified is None:
        status = "ВЫЗОВ ПРИНЯТ, ЭФФЕКТ НЕ ПРОВЕРЕН"
    elif verified is False:
        status = "НЕ ПОДТВЕРЖДЕНО"
    else:
        status = "ОТКАЗ"

    lines = [f"{status}: {d.get('action')} на {target} {d.get('path')}"]
    if d.get("before") is not None or d.get("after") is not None:
        lines.append(f"  было: {d.get('before')!r}  стало: {d.get('after')!r}")
    if d.get("risk_level"):
        # Reported so an escalation is visible at the point it happens rather than
        # only in the trace afterwards.
        lines.append(f"  риск: {d.get('risk_level')} — {d.get('risk_reason', '')}")
    lines.append(f"  {d.get('message')}")
    if ok and verified is None:
        lines.append(
            "  Проверьте результат наблюдением (новый снимок или снимок экрана), "
            "прежде чем считать действие состоявшимся."
        )
    if not ok:
        lines.append(
            "  Повторять вслепую нельзя. Перечитайте цель: путь мог устареть, окно "
            "могло закрыться, или элемент не поддерживает этот паттерн."
        )
    return "\n".join(lines)


# Unstructured on purpose: a console slice can be thousands of characters.
@mcp.tool(structured_output=False)
async def read_terminal(
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    tail: int = 200,
    head: int | None = None,
    start: int | None = None,
    count: int | None = None,
    contains: str | None = None,
) -> str:
    """
    Reads a console buffer directly through UI Automation. No cursor, no screenshot, no OCR.

    Works on Windows Terminal and legacy conhost. The buffer is read through
    TextPattern, so text comes back exactly -- including Cyrillic -- rather than as
    recognised glyphs. Reading is Class B: it works on a minimized or occluded
    terminal and does not disturb the human.

    The whole screen buffer is reachable in one read, not just the visible viewport:
    measured at 9000 rows, about 1.1 MB, in 30 ms. That is far too much to return, so
    a slice is selected. The default is the last 200 rows, which is where the result
    of the last command lives.

    The buffer is a ring of about 9000 rows. Past that the oldest output is gone for
    good, and the response says so. For a long-running command, read while it runs
    rather than once at the end.

    Args:
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        tail: Number of trailing rows to return (default 200). Ignored if head,
            start or count is given.
        head: Number of leading rows to return instead.
        start: Zero-based first row of a range.
        count: Number of rows in a range starting at `start`.
        contains: Return only rows containing this substring. A zero match count is
            reported explicitly, because it is not the same as an empty buffer.
    """
    resp = await _send_daemon_request("read_terminal", {
        "hwnd": hwnd, "window_title": window_title, "class_name": class_name,
        "tail": tail, "head": head, "start": start, "count": count,
        "contains": contains,
    })
    if resp.get("status") != "ok":
        return f"Буфер консоли не прочитан: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    session_note = (
        f"ВНИМАНИЕ: {d['session_warning']}" if d.get("session_warning") else None
    )
    header = [session_note] if session_note else []
    header.append(
        f"Консоль: handle={d.get('handle')} host={d.get('host')} "
        f"выбрано={d.get('resolution')}"
    )
    header.append(
        f"Буфер: {d.get('row_count')} строк, {d.get('char_count')} символов, "
        f"чтение {d.get('read_ms')} мс. Отдано {d.get('selected_count')} строк "
        f"({d.get('mode')})."
    )
    if d.get("matched_count") is not None:
        header.append(f"Совпадений с фильтром: {d.get('matched_count')}.")
    for w in d.get("warnings") or []:
        header.append(f"Внимание: {w}")

    lines = d.get("lines") or []
    body = "\n".join(lines) if lines else "(выбранный диапазон пуст)"
    return "\n".join([*header, "", body])


@mcp.tool()
async def write_terminal(
    text: str,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    enter: bool = True,
    expect: str | None = None,
    timeout_s: float = 3.0,
    allow_host_input: bool = False,
) -> str:
    """
    Types into a console without using the keyboard or the cursor. Class B.

    Characters are posted as WM_CHAR to the window the console actually takes input
    from, so the human keeps the keyboard and this runs in parallel with their work.
    On Windows Terminal that window is a child -- the XAML island's input site -- and
    not the top-level window; posting to the top level is accepted and then ignored.
    Legacy conhost accepts it on the top-level window itself.

    Every write is confirmed by re-reading the buffer. This is mandatory, not a
    nicety: ValuePattern on a terminal reports IsReadOnly=0, accepts SetValue, and
    changes nothing at all. A write that is not read back is not known to have
    happened.

    `expect` defaults to the text itself, which is right for a shell that echoes its
    input. Pass an empty string when echo is suppressed, as at a password prompt;
    verification then falls back to "the buffer changed", which is weaker and is
    reported as weaker.

    Args:
        text: The characters to type.
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        enter: Send a carriage return afterwards (default True).
        expect: Text that must appear in the buffer to confirm the write.
        timeout_s: How long to poll the buffer for confirmation (default 3).
        allow_host_input: Permit writing into a terminal owned by the host's own
            process tree. That terminal is the agent's own tool channel, so writing
            there changes what its next command does rather than failing visibly.
    """
    resp = await _send_daemon_request("write_terminal", {
        "text": text, "hwnd": hwnd, "window_title": window_title,
        "class_name": class_name, "enter": enter, "expect": expect,
        "timeout_s": timeout_s, "allow_host_input": allow_host_input,
    })
    if resp.get("status") == "refused":
        return _format_refusal(resp)
    if resp.get("status") != "ok":
        return f"Запись в консоль не выполнена: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    if d.get("verified") and not d.get("weak_verification"):
        status = "ЗАПИСАНО И ПОДТВЕРЖДЕНО"
    elif d.get("verified"):
        status = "ЗАПИСАНО, ПОДТВЕРЖДЕНИЕ СЛАБОЕ"
    else:
        status = "НЕ ПОДТВЕРЖДЕНО"

    lines = [
        f"{status}: {d.get('chars')} символов в handle={d.get('handle')} "
        f"(host={d.get('host')}, окно ввода={d.get('input_hwnd')}, "
        f"доставлено WM_CHAR={d.get('posted')})",
        f"  {d.get('message')}",
    ]
    tail = d.get("tail") or []
    if tail:
        lines.append("  хвост буфера:")
        for ln in tail[-8:]:
            lines.append(f"    | {ln[:110]}")
    if not d.get("verified"):
        lines.append(
            "  Повторять вслепую нельзя: команда могла исполниться, а вывод ещё не "
            "дошёл. Прочитайте буфер или дождитесь строки через wait_terminal."
        )
    return "\n".join(lines)


@mcp.tool()
async def wait_terminal(
    expect: str,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    timeout_s: float = 30.0,
    interval_s: float = 0.25,
    after_text: str | None = None,
) -> str:
    """
    Waits for text to appear in a console buffer, instead of sleeping a fixed time.

    A terminal can be read directly, so waiting on a condition is both faster and
    more reliable than a guessed delay: it returns as soon as the text lands and says
    plainly when it never did. Use this after starting a build, a test run or an
    install, rather than sleeping and then reading.

    `after_text` matters more than it looks. A shell echoes the command line before
    running it, so waiting for a string that also appears in the command returns
    instantly on the echo and proves nothing. Measured: waiting for a marker from
    `ping -n 7 ... & echo MARKER` succeeded in 0.00 s, while the same wait anchored
    with after_text took 6.16 s and matched the real output. Pass the command that was
    typed, or wait for text the command does not literally contain.

    Args:
        expect: The text to wait for.
        hwnd: Exact handle from list_windows_and_monitors. Takes priority.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        timeout_s: How long to wait before reporting absence (default 30).
        interval_s: Poll interval (default 0.25).
        after_text: Only search buffer text occurring after the last occurrence of
            this string. Use it to skip past the echoed command line.
    """
    resp = await _send_daemon_request(
        "wait_terminal",
        {
            "expect": expect, "hwnd": hwnd, "window_title": window_title,
            "class_name": class_name, "timeout_s": timeout_s,
            "interval_s": interval_s, "after_text": after_text,
        },
        # The deadline belongs to the caller, so the transport has to be derived
        # from it: the table's 30 s default tied the tool's own default exactly.
        timeout=config.ws_timeout_for_wait(timeout_s),
    )
    if resp.get("status") != "ok":
        return f"Ожидание не выполнено: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    head = (
        f"ДОЖДАЛИСЬ: {expect!r} появилась через {d.get('elapsed_s')} с "
        f"({d.get('polls')} опросов)"
        if d.get("found") else
        f"НЕ ДОЖДАЛИСЬ: {d.get('message')}"
    )
    lines = [head]
    tail = d.get("tail") or []
    if tail:
        lines.append("  хвост буфера:")
        for ln in tail[-8:]:
            lines.append(f"    | {ln[:110]}")
    if d.get("found") and (d.get("elapsed_s") or 0) < 0.05:
        lines.append(
            "  Подозрительно быстро: строка могла совпасть с эхом набранной команды, "
            "а не с выводом. Повторите с after_text или ждите текст, которого нет в "
            "командной строке."
        )
    return "\n".join(lines)


@mcp.tool()
async def find_text_on_screen(
    text: str,
    monitor_index: int | None = None,
    fuzzy: bool = True,
) -> str:
    """
    Finds text on screen by OCR and returns frame coordinates to click.

    This is the fallback for targets with no accessibility tree -- Explorer's content
    pane, Java AWT applications, canvas surfaces. Prefer get_snapshot and
    act_on_element whenever the target exposes a tree: they give exact text, roles
    and stable addresses, while OCR gives glyphs with boxes and no semantics.

    Recognition uses Windows.Media.Ocr and is fast (about 13 ms for a 760x260 region),
    so there is no need to restrict it to small areas. Coordinates come back in frame
    space and can be passed straight to mouse_action.

    Matching normalizes case, folds Cyrillic and Latin homoglyphs, and discards
    punctuation, because a Russian recognizer renders Latin labels with Cyrillic
    look-alikes: measured output rendered the Latin word OK with two Cyrillic
    letters and dropped an underscore.
    Without normalization, plain substring matching reports text as absent when it is
    plainly visible.

    A miss is not proof of absence. The recognizer does substitute individual letters
    -- measured on a rendered sample, where Latin ALPHA came back as a Cyrillic
    look-alike that normalization cannot reach -- so on a miss try a shorter string,
    or call read_screen_text and look at what was actually recognized.

    Args:
        text: The text to locate.
        monitor_index: Which monitor to capture. Defaults to the one under control.
        fuzzy: Normalize case, homoglyphs and punctuation (default True). Set False
            to require an exact casefolded substring, which is for proving what the
            recognizer returned rather than for finding a control.
    """
    resp = await _send_daemon_request("find_text_on_screen", {
        "text": text, "monitor_index": monitor_index, "fuzzy": fuzzy,
    })
    if resp.get("status") != "ok":
        return f"Поиск текста не выполнен: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    header = [
        f"Поиск {d.get('needle')!r} на мониторе {d.get('monitor', {}).get('index')}, "
        f"кадр {d.get('frame')}, язык распознавания {d.get('language')}, "
        f"{d.get('ocr_ms')} мс, строк распознано {d.get('line_count')}.",
    ]
    matches = d.get("matches") or []
    if not matches:
        header.append("Совпадений: 0.")
        if d.get("hint"):
            header.append(d["hint"])
        return "\n".join(header)

    header.append(f"Совпадений: {d.get('match_count')}.")
    for m in matches[:12]:
        header.append(
            f"  x={m['center'][0]}, y={m['center'][1]}  ({m['kind']}) "
            f"{m['text']!r}  box={m['box']}"
            + (f"  в строке {m['line_text'][:56]!r}" if m.get("line_text") else "")
        )
    if len(matches) > 12:
        header.append(f"  ... ещё {len(matches) - 12} совпадений")
    return "\n".join(header)


# Unstructured on purpose: up to 120 OCR lines with boxes. See ContentBlock.
@mcp.tool(structured_output=False)
async def read_screen_text(
    monitor_index: int | None = None,
    limit: int = 120,
    with_words: bool = False,
) -> str:
    """
    Reads all recognized text on a monitor, with boxes, through OCR.

    For a target that exposes no accessibility tree this is the only way to see what
    is on screen as text rather than as pixels. It is also how to diagnose a failed
    find_text_on_screen: the recognizer substitutes individual letters, so reading
    what actually came back shows whether the text is absent or merely misread.

    Coordinates are in frame space. Recognition takes tens of milliseconds for a
    full frame.

    Args:
        monitor_index: Which monitor to capture. Defaults to the one under control.
        limit: Maximum number of text lines to return (default 120).
        with_words: Include per-word boxes, not just per-line (default False).
    """
    resp = await _send_daemon_request("read_screen_text", {
        "monitor_index": monitor_index, "limit": limit, "with_words": with_words,
    })
    if resp.get("status") != "ok":
        return f"Чтение текста с экрана не выполнено: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    header = [
        f"Экран: монитор {d.get('monitor', {}).get('index')} "
        f"({d.get('monitor', {}).get('name')}), кадр {d.get('frame')}, "
        f"язык {d.get('language')}, {d.get('ocr_ms')} мс.",
        f"Строк распознано: {d.get('line_count')}"
        + (f", отдано {len(d.get('lines') or [])}." if d.get("truncated") else "."),
    ]
    lines = d.get("lines") or []
    if not lines:
        return "\n".join([*header, "", "Текст не распознан."])

    body = []
    for ln in lines:
        c = ln.get("center") or [0, 0]
        body.append(f"@{c[0]},{c[1]}  {ln.get('text', '')[:150]}")
        for w in ln.get("words") or []:
            body.append(f"      word {w['text'][:60]!r} box={w['box']}")
    return "\n".join([*header, "", *body])


@mcp.tool()
async def wait_for_text(
    text: str,
    monitor_index: int | None = None,
    timeout_s: float = 20.0,
    interval_s: float = 0.75,
    appear: bool = True,
    fuzzy: bool = True,
) -> str:
    """
    Waits for text to appear on (or disappear from) screen, instead of sleeping.

    Use this to wait for a dialog, a progress message, a confirmation or an error,
    rather than sleeping a guessed duration and then looking. It returns as soon as
    the condition holds and says plainly when it never did.

    Polling is coarser than wait_terminal, because each pass captures and recognizes
    a frame instead of reading a buffer. The same caveat applies as for
    find_text_on_screen: OCR substitutes individual letters, so waiting on a long
    distinctive string can time out even when the text is on screen. Prefer a short
    stable fragment, and prefer get_snapshot plus a UIA condition when the target has
    a tree at all.

    Args:
        text: The text to wait for.
        monitor_index: Which monitor to capture. Defaults to the one under control.
        timeout_s: How long to wait before reporting failure (default 20).
        interval_s: Poll interval, minimum 0.15 (default 0.75).
        appear: True to wait for the text to show up, False to wait for it to go
            away -- for example a progress indicator clearing.
        fuzzy: Normalize case, homoglyphs and punctuation (default True).
    """
    resp = await _send_daemon_request(
        "wait_for_text",
        {
            "text": text, "monitor_index": monitor_index, "timeout_s": timeout_s,
            "interval_s": interval_s, "appear": appear, "fuzzy": fuzzy,
        },
        # Safe by coincidence at the 20 s default (transport is 30 s), and broken
        # for any longer wait the caller asks for. Deriving it removes the
        # coincidence.
        timeout=config.ws_timeout_for_wait(timeout_s),
    )
    if resp.get("status") != "ok":
        return f"Ожидание не выполнено: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    want = "появился" if d.get("appear") else "исчез"
    if d.get("found"):
        head = (
            f"ДОЖДАЛИСЬ: текст {d.get('needle')!r} {want} через "
            f"{d.get('elapsed_s')} с ({d.get('polls')} опросов)."
        )
        body = []
        for m in (d.get("matches") or [])[:6]:
            body.append(f"  x={m['center'][0]}, y={m['center'][1]}  "
                        f"{m['text']!r} box={m['box']}")
        return "\n".join([head, *body]) if body else head
    return (
        f"НЕ ДОЖДАЛИСЬ: {d.get('message')}\n"
        "  Отсутствие совпадения не доказывает отсутствие текста: распознаватель "
        "подменяет отдельные буквы. Попробуйте более короткий фрагмент или "
        "прочитайте экран через read_screen_text."
    )


@mcp.tool()
async def reset_user_override() -> str:
    """
    Resets USER_OVERRIDE back to IDLE.

    Call this only after the user has explicitly agreed in chat to continue. A physical
    mouse movement or keypress puts the daemon into USER_OVERRIDE and aborts the running
    action; that is the human reclaiming the machine, and it is not a condition to clear
    automatically.

    The human can also clear it themselves with Ctrl+Alt+Space.

    Note that release_control already works during USER_OVERRIDE, so this tool is not the
    only way out of a pause.
    """
    resp = await _send_daemon_request("reset_override", {})
    if resp.get("status") == "ok":
        return f"Режим паузы успешно сброшен. Текущее состояние: {resp.get('state', 'IDLE')}."
    return f"Не удалось сбросить режим: {resp.get('message', 'Неизвестная ошибка')}"


def main() -> None:
    """Entry point for the ``autopilot-mcp`` console script."""
    mcp.run()


@mcp.tool()
async def get_hud_state() -> str:
    """
    Returns live HUD overlay geometry: window bounds, the framed region in both global
    desktop and widget-local coordinates, the point where the frame is actually painted,
    and a placement_correct flag.

    Use this to confirm the neon frame surrounds the intended monitor. The overlay carries
    WDA_EXCLUDEFROMCAPTURE, so a mis-placed frame never appears in any screenshot -- these
    numbers are the only machine-checkable evidence of where it landed. That matters on
    multi-monitor setups: the overlay window spans the whole virtual desktop, whose origin
    is not (0, 0) when a secondary display sits to the left of or above the primary.
    """
    resp = await _send_daemon_request("hud_state", {})
    if resp.get("status") != "ok":
        return f"Ошибка получения состояния HUD: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    return "\n".join([
        f"Состояние: {resp.get('state')}   оверлей видим: {d.get('visible')}",
        f"Окно оверлея:              {d.get('window')}",
        f"Цель (глобальные коорд.):  {d.get('target_global')}",
        f"Цель (локальные коорд.):   {d.get('target_local')}",
        f"Рамка рисуется в точке:    {d.get('painted_at_global')}",
        f"Размещение корректно:      {d.get('placement_correct')}",
    ])


@mcp.tool()
async def wait_for_event(
    condition: EVENT_CONDITIONS | None = None,
    quiet_ms: float | None = None,
    hwnd: int | None = None,
    window_title: str | None = None,
    timeout_s: float = 30.0,
    since_tick: int | None = None,
    events: list[int] | None = None,
) -> str:
    """
    Waits for the UI's own change notifications instead of polling frames or sleeping.

    Delivery measured at about a millisecond against the 100 ms floor of frame polling,
    and it costs no capture per iteration. It also sees changes a pixel diff cannot: a
    value replaced by text of the same length, or a tree that reordered rows which look
    identical.

    Two modes, answering different questions. Give `condition` to wait for something to
    happen; give `quiet_ms` to wait for the interface to stop changing. `quiet_ms` is the
    better default after an action, because it does not require guessing which event a
    particular control emits.

    Conditions: 'opened', 'closed', 'focused', 'value_changed', 'tree_changed',
    'appeared'. Pass raw event codes through `events` only when a condition does not
    cover what you need.

    The wait is anchored to a tick. Omit `since_tick` to anchor at the moment of the
    call, which is what you want after performing an action in a previous turn. Passing
    an older tick deliberately re-reads history, and events older than the anchor are
    discarded and reported as `discarded_stale` rather than matched -- a backlog
    delivered late must not read as "the UI just changed".

    A timeout is a normal outcome of waiting, not a tool failure, so it comes back as a
    report rather than an error.

    Args:
        condition: Named event set to wait for.
        quiet_ms: Instead of a condition, wait for this many milliseconds of silence.
        hwnd: Restrict to one window. Events on its controls carry the control's handle,
            so this matches the whole top-level window, not just the frame.
        window_title: Substring of the window title, when no handle is known.
        timeout_s: How long to wait before reporting that nothing happened (default 30).
        since_tick: Anchor tick from an earlier response. Defaults to now.
        events: Raw WinEvent codes, for cases no named condition covers.
    """
    resp = await _send_daemon_request(
        "wait_for_event",
        {
            "condition": condition, "quiet_ms": quiet_ms, "hwnd": hwnd,
            "window_title": window_title, "timeout_s": timeout_s,
            "since_tick": since_tick, "events": events,
        },
        timeout=config.ws_timeout_for_wait(timeout_s),
    )
    if resp.get("status") != "ok":
        return f"Ожидание не выполнено: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    mode = d.get("mode")
    if d.get("matched"):
        if mode == "quiescence":
            head = (f"УСПОКОИЛОСЬ через {d.get('elapsed_ms')} мс "
                    f"(тишина {d.get('quiescent_ms')} мс).")
        else:
            ev = d.get("event") or {}
            head = (f"ДОЖДАЛИСЬ: {ev.get('kind')} через {d.get('elapsed_ms')} мс "
                    f"(hwnd={ev.get('hwnd')}).")
    else:
        head = f"НЕ ДОЖДАЛИСЬ за {timeout_s:.0f} с. {d.get('message', '')}"
    lines = [head,
             f"  увидено событий: {d.get('events_seen')}, "
             f"отброшено устаревших: {d.get('discarded_stale')}"]
    w = d.get("watcher") or {}
    lines.append(f"  наблюдатель: доставлено {w.get('delivered')}, "
                 f"в буфере {w.get('buffer')}, хук {w.get('hook')}")
    if d.get("since_tick") is not None:
        lines.append(f"  метка since_tick={d.get('since_tick')} — "
                     f"передайте её следующему ожиданию, чтобы не потерять события")
    return "\n".join(lines)


@mcp.tool()
async def execute_batch(
    steps: list[dict],
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
    stop_on: Literal["failure", "high_risk", "both"] = "both",
    observe_at_end: bool = True,
    monitor_index: int | None = None,
) -> list[ContentBlock]:
    """
    Runs a sequence of Class B steps in one call and returns one observation at the end.

    Each step is an act_on_element or window_action verb, addressed by `path` against the
    window the batch resolved. One frame for the whole batch instead of one per step is
    the point: eight steps cost one capture, not eight.

    Only Class B steps are accepted. Class A steps (mouse_action, keyboard_action,
    scroll_screen) are refused rather than run one at a time, because batching them
    properly needs a single input-channel acquisition for the whole batch, and that
    mechanism is unavailable from a non-elevated process. Running them per step inside
    something called a batch would be slower than calling them directly and would claim
    an atomicity it does not have.

    `stop_on` decides where the batch stops:
      'failure'   -- a step that did not verify
      'high_risk' -- a step classified as not batchable, stopped BEFORE it runs
      'both'      -- either (default)

    There is no 'never'. It would execute an irreversible step inside a batch with no
    chance to stop, which is the one thing the risk classification exists to prevent.

    A pre-flight check on a step reads the real target state before the step runs and
    compares it with what was declared:

        {"action": "invoke", "path": "3.1",
         "assert": {"path": "0.5", "name_contains": "Alice"}}

    A mismatch aborts the batch and returns both the declared and the actual value, so a
    stale path is distinguishable from a genuinely wrong recipient.

    Steps run through the same code path as single actions, so every step gets the same
    host-process guard, the same risk level and the same dry_run classification. The
    result reports the level each step resolved to, which is how a mis-bind becomes
    visible: a path that landed on Send instead of Cancel is reported as irreversible.

    Args:
        steps: Ordered list of steps. Each has `kind` ('element' or 'window'), the verb
            in `action`, a `path`, and any verb arguments (`text`, `value`, `x`, `y`).
        hwnd: Window the batch acts on. Takes priority over a title search.
        window_title: Substring of the window title.
        class_name: Win32 window class, narrows a title search.
        stop_on: Where to stop. Default 'both'.
        observe_at_end: Capture one frame after the batch (default True).
        monitor_index: Which monitor the final frame comes from.
    """
    resp = await _send_daemon_request(
        "execute_batch",
        {
            "steps": steps, "hwnd": hwnd, "window_title": window_title,
            "class_name": class_name, "stop_on": stop_on,
            "observe_at_end": observe_at_end, "monitor_index": monitor_index,
        },
    )
    if resp.get("status") != "ok":
        return [_text(f"ОШИБКА: {resp.get('message', 'Неизвестная ошибка')}")]

    d = resp.get("data", {})
    blocks: list[ContentBlock] = []
    shot = d.get("screenshot")
    if shot:
        blocks.append(ImageContent(type="image", data=shot, mime_type="image/webp"))

    lines = [d.get("message", ""),
             f"  исполнено {d.get('executed')} из {d.get('steps_total')}, "
             f"подтверждено {d.get('succeeded')}, "
             f"цель: handle={d.get('handle')} ({d.get('resolution')})"]
    if d.get("stabilization"):
        lines.append(f"  стабилизация: {d.get('stabilization')} "
                     f"за {d.get('stabilization_ms')} мс")
    if d.get("observation_error"):
        lines.append(f"  ВНИМАНИЕ: кадр после пакета не получен: "
                     f"{d.get('observation_error')}")

    for r in d.get("results", []):
        mark = {"ok": "+", "stopped_before": "!", "refused": "X",
                "preflight_mismatch": "?"}.get(r.get("status"),
                                              "-" if r.get("ok") else "x")
        if r.get("status") == "ok":
            mark = "+" if r.get("verified") else ("=" if r.get("verified") is None
                                                   else "x")
        detail = f"{r.get('index')}. [{mark}] {r.get('action')} -> {r.get('path')}"
        if r.get("resolved_name"):
            detail += f"  ({r.get('resolved_name')!r})"
        if r.get("risk_level"):
            detail += f"  риск={r.get('risk_level')}"
        lines.append(f"  {detail}")
        if r.get("message") and r.get("status") != "ok":
            lines.append(f"      {r.get('message')}")
        if r.get("actual") is not None or r.get("expected_contains") is not None:
            lines.append(f"      объявлено: {r.get('expected_equals') or r.get('expected_contains')!r}"
                         f"  прочитано: {r.get('actual')!r}")

    if d.get("stop_reason"):
        lines.append(f"  причина остановки: {d.get('stop_reason')}")
    blocks.append(_text("\n".join(lines)))
    return blocks


@mcp.tool()
async def get_cursor_state() -> str:
    """
    Read the pointer shape and the caret position without moving anything.

    Two facts no screenshot provides.

    The shape says what the thing under the pointer accepts: `ibeam` means the target
    takes text, `hand` means it is clickable, `no` means the action would be refused,
    `wait` or `appstarting` means the application is busy and any action now is
    unreliable. That is evidence about the target obtained *before* typing or clicking,
    which is when a wrong guess is still cheap.

    The caret rectangle says where typed text will actually land -- a different question
    from where the pointer is, since a field can hold focus and a blinking caret while the
    pointer sits anywhere else on screen. When no caret exists at all, no input field has
    focus and text typed now goes nowhere; that is worth knowing before blaming the
    application for swallowing it.

    Standard shapes are identified by comparing the pointer handle against the handles
    LoadCursor returns for each system cursor. Anything else is reported as `custom`,
    which is a real case (application-drawn cursors), not an error.

    The position is reported twice: `position` from GetCursorInfo, and `position_physical`
    from GetPhysicalCursorPos, which is unscaled by the thread's DPI context. They agree
    whenever process DPI awareness matches the monitor's and differ otherwise -- a
    `dpi_mismatch` of true means a click aimed at one of them lands somewhere other than
    the other. Reading only the first would look correct on a 100%-DPI machine and be
    wrong on a mixed-DPI one, so both are published rather than one being assumed.

    Observation only: it takes no input lock and works while a human has the machine.
    """
    resp = await _send_daemon_request("get_cursor_state", {})
    if resp.get("status") != "ok":
        return f"Ошибка чтения состояния курсора: {resp.get('message', 'Неизвестная ошибка')}"

    d = resp.get("data", {})
    cur = d.get("cursor") or {}
    car = d.get("caret") or {}
    lines = [d.get("message", "")]
    lines.append(f"  позиция: {cur.get('position')}  виден: {cur.get('showing')}  "
                 f"handle: {cur.get('handle')}")
    # Reported separately because the two agree at 100% DPI and differ otherwise. A
    # mismatch means the process DPI context does not match the monitor's, which is the
    # condition under which a click lands somewhere other than where it was aimed.
    lines.append(f"  физическая позиция: {cur.get('position_physical')}  "
                 f"расхождение DPI: {cur.get('dpi_mismatch')}")
    if car.get("available"):
        lines.append(f"  каретка: видна={car.get('caret_visible')} "
                     f"rect={car.get('caret_rect')} "
                     f"hwnd_caret={car.get('hwnd_caret')}")
        lines.append(f"  фокус:   hwnd={car.get('hwnd_focus')} "
                     f"класс={car.get('focus_class')!r} поток={car.get('thread_id')}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
