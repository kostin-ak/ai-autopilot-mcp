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

    summary_lines = [f"Статус демона: {state}"]
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
    Захватывает управление сессией для ИИ: зажигает постоянную неоновую рамку вокруг выбранного экрана.
    Вызывайте этот инструмент первым шагом перед началом выполнения интерактивных действий.

    Параметры:
        monitor_index: Номер экрана (1 = основной монитор, 0 = все мониторы, None = автовыбор).
        show_grid: Накладывать ли координатную сетку с шагом 100px и шкалами (по умолчанию False).
    """
    params = {"monitor_index": monitor_index, "show_grid": show_grid}
    resp = await _send_daemon_request("take_control", params)
    return _format_image_and_text(resp, "Управление рабочим столом успешно захвачено агентом.")


@mcp.tool()
async def release_control() -> str:
    """
    Освобождает управление рабочим столом и выключает постоянную рамку HUD.
    Вызывайте этот инструмент строго в конце выполнения всей составной задачи.
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
    Запрашивает свежий снимок экрана, статус системы (IDLE, AI_ACTIVE, USER_OVERRIDE),
    разрешение кадра и текущие координаты мыши.

    Параметры:
        monitor_index: Индекс монитора (1 = основной монитор, 0 = все экраны вместе, None = автовыбор).
        show_grid: Накладывать ли координатную сетку с шагом 100px (по умолчанию False).
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
    Выполняет действие мышью с предиктивным визуальным маркером и проверкой перехвата человеком.

    Параметры:
        action: Тип действия ('click', 'double_click', 'right_click', 'move', 'drag')
        x: Координата X на сжатом скриншоте (от 0 до 1280)
        y: Координата Y на сжатом скриншоте
        end_x: Конечная координата X для перетаскивания (drag)
        end_y: Конечная координата Y для перетаскивания (drag)
        monitor_index: Индекс монитора
        show_grid: Накладывать ли координатную сетку на итоговый снимок (по умолчанию False)
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
    Выполняет ввод текста через буфер обмена или нажимает системную комбинацию клавиш.

    Параметры:
        text: Текст для безопасного ввода через буфер обмена (не зависит от текущей раскладки RU/EN)
        hotkey: Сочетание клавиш (например: 'ctrl+c', 'ctrl+v', 'alt+tab', 'enter', 'esc')
        monitor_index: Индекс монитора для возврата итогового снимка
        show_grid: Накладывать ли координатную сетку на итоговый снимок (по умолчанию False)
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
    Прокручивает экран вверх или вниз колесом мыши с ожиданием стабилизации изображения.

    Параметры:
        direction: Направление прокрутки ('up' или 'down')
        amount: Количество шагов прокрутки (по умолчанию 3)
        x: Опциональная координата X, куда предварительно переместить курсор
        y: Опциональная координата Y, куда предварительно переместить курсор
        monitor_index: Индекс монитора
        show_grid: Накладывать ли координатную сетку на итоговый снимок (по умолчанию False)
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
    Возвращает список всех подключенных мониторов и открытых окон приложений с их координатами.
    Позволяет агенту сориентироваться, какие программы запущены и на каких экранах они находятся.
    """
    resp = await _send_daemon_request("list_windows_and_monitors", {})
    if resp.get("status") != "ok":
        return f"Ошибка получения списка окон: {resp.get('message', 'Неизвестная ошибка')}"

    data = resp.get("data", {})
    monitors = data.get("monitors", [])
    windows = data.get("windows", [])

    output = ["=== ПОДКЛЮЧЕННЫЕ МОНИТОРЫ ==="]
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
            f"- '{w['title']}' (Монитор {w['monitor_index']}, границы={w['bounds']}){marks}"
        )

    return "\n".join(output)


@mcp.tool()
async def get_ui_elements(
    window_title: str | None = None,
    monitor_index: int | None = None,
) -> str:
    """
    Выполняет инспекцию интерактивных элементов UI (кнопки, поля ввода, чекбоксы) через Windows Accessibility.
    Возвращает список элементов с уже готовыми координатами клика, масштабированными под скриншот.

    Параметры:
        window_title: Опциональный фильтр по заголовку окна (если не указан, берется активное окно)
        monitor_index: Индекс монитора
    """
    params = {
        "window_title": window_title,
        "monitor_index": monitor_index,
    }
    resp = await _send_daemon_request("get_ui_elements", params)
    if resp.get("status") != "ok":
        return f"Ошибка инспекции элементов: {resp.get('message', 'Неизвестная ошибка')}"

    data = resp.get("data", {})
    elements = data.get("elements", [])
    skipped = data.get("skipped_off_monitor", 0)
    mon = data.get("monitor", {})
    frame = data.get("frame", [])

    header = [
        f"Найдено интерактивных элементов: {len(elements)}",
    ]
    if mon:
        header.append(
            f"Монитор {mon.get('index')} '{mon.get('name')}' "
            f"границы={mon.get('bounds')} кадр={frame}"
        )
    if skipped:
        header.append(f"Пропущено вне этого монитора: {skipped}")
        if data.get("hint"):
            header.append(data["hint"])

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
async def reset_user_override() -> str:
    """
    Сбрасывает режим паузы (USER_OVERRIDE) в исходное состояние (IDLE).
    Используйте этот инструмент только после того, как пользователь в чате разрешил продолжить работу.
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
    Возвращает живую геометрию HUD-оверлея: границы окна, обрамляемую область в
    глобальных и локальных координатах окна, точку, где рамка фактически рисуется,
    и признак корректности размещения.

    Используйте для проверки того, что неоновая рамка окружает именно нужный монитор.
    Оверлей аппаратно исключён из скриншотов (WDA_EXCLUDEFROMCAPTURE), поэтому
    неверно расположенную рамку на снимке не увидеть — эти числа являются единственно
    проверяемым свидетельством её положения.
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
