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
ContentBlock = TextContent | ImageContent


def _read_auth_token() -> str:
    """Reads session token from environment or local file."""
    token_env = os.environ.get("AUTOPILOT_TOKEN")
    if token_env:
        return token_env.strip()

    if config.TOKEN_FILE_PATH.exists():
        return config.TOKEN_FILE_PATH.read_text(encoding="utf-8").strip()

    return ""


async def _send_daemon_request(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Sends a JSON command to the local autopilot daemon over WebSocket.

    Every await is bounded. An unbounded ``recv()`` turns a hung daemon -- a UIA
    tree walk on a large Electron app is the usual cause -- into a tool call that
    never returns, which the host eventually kills with a useless error.
    """
    token = _read_auth_token()
    uri = f"ws://{config.WS_HOST}:{config.WS_PORT}"
    timeout = config.ws_timeout_for(action)

    req_id = str(uuid.uuid4())[:8]
    payload = {
        "id": req_id,
        "token": token,
        "action": action,
        "params": params,
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
    action: Literal["click", "double_click", "right_click", "move", "drag"],
    x: int,
    y: int,
    end_x: int | None = None,
    end_y: int | None = None,
    monitor_index: int | None = None,
    show_grid: bool = False,
) -> list[ContentBlock]:
    """
    Performs a mouse action with a predictive visual marker and human-interruption check.

    This is a Class A action: it consumes the session's single cursor, so it cannot run
    concurrently with the human using the machine. A physical mouse movement or keypress
    switches the daemon to USER_OVERRIDE and aborts the action mid-flight. Prefer
    element-based actions when the target exposes them.

    Coordinates are in the downscaled frame, not physical pixels. The response reports
    where the cursor actually ended up, which differs from the requested point whenever
    a move was clipped or interrupted -- that difference is the signal a click failed.

    Args:
        action: 'click', 'double_click', 'right_click', 'move' or 'drag'.
        x: X in the downscaled frame (0..scaled_width).
        y: Y in the downscaled frame (0..scaled_height).
        end_x: Destination X for 'drag'.
        end_y: Destination Y for 'drag'.
        monitor_index: Which monitor the frame coordinates refer to.
        show_grid: Overlay a grid on the resulting frame. Off by default.
    """
    params = {
        "action": action,
        "x": x,
        "y": y,
        "end_x": end_x,
        "end_y": end_y,
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
    direction: Literal["up", "down"] = "down",
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


@mcp.tool()
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


@mcp.tool()
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
        max_depth: Depth cap (default 20). Raising it is the only way past a
            depth truncation.
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


@mcp.tool()
async def act_on_element(
    action: Literal["invoke", "set_value", "toggle", "select", "expand", "collapse"],
    path: str,
    text: str | None = None,
    hwnd: int | None = None,
    window_title: str | None = None,
    class_name: str | None = None,
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
      'invoke'     - press a button or menu item. Not verifiable; confirm the effect
                     by observing afterwards.
      'set_value'  - write text into an edit or value field. Requires `text`.
      'toggle'     - flip a checkbox or toggle button; before/after states returned.
      'select'     - select a list, tree or tab item.
      'expand'     - open a combo box or tree node.
      'collapse'   - close one.

    Args:
        action: One of the six above.
        path: The node's path from a get_snapshot result, e.g. '1.0.2'.
        text: The value to write, for 'set_value' only.
        hwnd: Exact handle of the window the snapshot came from.
        window_title: Substring of the window title.
        class_name: Win32 window class.
    """
    resp = await _send_daemon_request("element_action", {
        "action": action, "path": path, "text": text,
        "hwnd": hwnd, "window_title": window_title, "class_name": class_name,
    })
    if resp.get("status") != "ok":
        return f"Действие не выполнено: {resp.get('message', 'Неизвестная ошибка')}"

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

    lines = [f"{status}: {d.get('action')} на узле {d.get('path')}"]
    if d.get("before") is not None or d.get("after") is not None:
        lines.append(f"  было: {d.get('before')!r}  стало: {d.get('after')!r}")
    lines.append(f"  {d.get('message')}")
    if ok and verified is None:
        lines.append(
            "  Проверьте результат наблюдением (новый снимок или снимок экрана), "
            "прежде чем считать действие состоявшимся."
        )
    if not ok:
        lines.append(
            "  Повторять вслепую нельзя. Сделайте новый снимок: путь мог устареть "
            "или элемент не поддерживает этот паттерн."
        )
    return "\n".join(lines)


@mcp.tool()
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
    """
    resp = await _send_daemon_request("write_terminal", {
        "text": text, "hwnd": hwnd, "window_title": window_title,
        "class_name": class_name, "enter": enter, "expect": expect,
        "timeout_s": timeout_s,
    })
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
    resp = await _send_daemon_request("wait_terminal", {
        "expect": expect, "hwnd": hwnd, "window_title": window_title,
        "class_name": class_name, "timeout_s": timeout_s,
        "interval_s": interval_s, "after_text": after_text,
    })
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


if __name__ == "__main__":
    main()
