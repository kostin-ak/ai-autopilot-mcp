# AI Desktop Autopilot (MCP + Overlay Daemon)

Автономная система компьютерного зрения и физического ввода для LLM-агентов. Оснащена визуальным HUD-оверлеем на PyQt6, аппаратной защитой от попадания в скриншоты (`WDA_EXCLUDEFROMCAPTURE`), визуальной координатной сеткой и бесконфликтным детектором физического вмешательства человека.

---

## Архитектура системы

Проект разделен на два изолированных процесса, взаимодействующих по локальному WebSocket (`ws://127.0.0.1:8765`):

```text
ai-autopilot-mcp/
├── config.py             # Настройки геометрии, таймаутов, портов и Per-Monitor DPI
├── overlay_ui.py         # Полноэкранный HUD-оверлей PyQt6 (аппаратное скрытие WDA_EXCLUDEFROMCAPTURE)
├── input_controller.py   # Низкоуровневые хуки Win32 (WH_MOUSE_LL / WH_KEYBOARD_LL), SendInput
├── vision_engine.py      # Захват mss, координатная сетка (100px/50px), перцептивная стабилизация, pywinauto
├── autopilot_daemon.py   # Фоновый сервер: qasync (PyQt6 + asyncio WebSocket), трей
├── mcp_server.py         # MCP-шлюз (MCPServer из mcp 2.x) для подключения к LLM
├── pyproject.toml        # Метаданные пакета, закреплённые зависимости, точки запуска
├── requirements.txt      # Зависимости Python (дублирует pyproject для pip -r)
├── tests/                # Регрессионные тесты чистой логики
├── docs/                 # ADR, глоссарий и план развития
├── install.bat           # Создание .venv и установка зависимостей
├── start_daemon.bat      # Скрипт быстрого запуска фонового демона
└── mcp_config.example.json # Пример конфигурации MCP для клиентов
```

---

## Быстрый старт на новом устройстве

### 1. Требования
- ОС: Windows 10 версии 2004 и новее, либо Windows 11 (64-bit).
  `WDA_EXCLUDEFROMCAPTURE` появился только в 2004.
- Python 3.11, 3.12 или 3.13, добавленный в `PATH`.
  Минимум 3.11 обязателен: `config.py` использует `enum.StrEnum`.

### 2. Установка зависимостей
Запустите `install.bat` — он создаст виртуальное окружение `.venv` и установит
проект в редактируемом режиме. То же самое вручную:
```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[win32]"
```

Установка даёт две точки запуска вместо обращения к файлам скриптов:
`.venv\Scripts\autopilot-daemon.exe` и `.venv\Scripts\autopilot-mcp.exe`.

Зависимости закреплены в `pyproject.toml` с верхними границами: проект работает
с нативными API Windows и чувствителен к смене мажорных версий SDK. В частности,
требуется `mcp>=2.0,<3` — в mcp 2.x модуль `mcp.server.fastmcp` удалён, и код,
рассчитанный на `FastMCP`, не запустится.

Для проверки:
```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check .
```

### 3. Запуск демона оверлея
Запустите `start_daemon.bat`. В трее появится иконка агента, а в консоли отобразится статус запуска WebSocket-сервера.

---

## Подключение к LLM-клиентам (MCP)

Во всех примерах используется точка запуска из `.venv`, поэтому интерпретатор и
состав зависимостей не зависят от того, что установлено в системе. Замените
`C:\path\to` на реальный путь к проекту.

> [!IMPORTANT]
> Не указывайте пустой объект `"env": {}` в конфигурации на Windows. Это стирает
> переменные среды `SystemRoot` и приводит к сбою загрузчика DLL.

### Kilo
В `kilo.json` или через `ctrl+p` → MCP:
```json
{
  "mcpServers": {
    "desktop-autopilot": {
      "command": "C:\\path\\to\\ai-autopilot-mcp\\.venv\\Scripts\\autopilot-mcp.exe"
    }
  }
}
```

### Claude Code
```powershell
claude mcp add desktop-autopilot -- "C:\path\to\ai-autopilot-mcp\.venv\Scripts\autopilot-mcp.exe"
```

### Claude Desktop
В файле `%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "desktop-autopilot": {
      "command": "C:\\path\\to\\ai-autopilot-mcp\\.venv\\Scripts\\autopilot-mcp.exe"
    }
  }
}
```

### Cursor
В файле `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "desktop-autopilot": {
      "command": "C:/path/to/ai-autopilot-mcp/.venv/Scripts/autopilot-mcp.exe"
    }
  }
}
```

### Google Antigravity
В файле `~/.gemini/config/mcp_config.json` — та же структура, что и для Cursor.

---

## Справочник инструментов (Tools API)

### `list_windows_and_monitors()` -> `str`
Возвращает список всех подключенных мониторов и открытых окон приложений.

### `take_control(monitor_index: Optional[int] = None, show_grid: bool = True)` -> `list[Image | TextContent]`
Захватывает сессионное управление, включает непрерывную неоновую рамку HUD на целевом экране и возвращает первый стабилизированный снимок экрана с координатной сеткой.

### `release_control()` -> `str`
Освобождает сессионное управление и гасит неоновую рамку HUD. Вызывается агентом строго в конце всей составной задачи.

### `get_screen_state(monitor_index: Optional[int] = None, show_grid: bool = True)` -> `list[Image | TextContent]`
Делает снимок указанного экрана, возвращает статус системы (`IDLE`, `AI_ACTIVE`, `USER_OVERRIDE`), разрешение кадра и текущие координаты курсора.

### `get_ui_elements(window_title: Optional[str] = None, monitor_index: Optional[int] = None)` -> `str`
Сканирует интерактивные элементы окна через Windows UI Automation. Возвращает готовые координаты `click_target: [x, y]` в сетке скриншота.

### `mouse_action(action, x, y, end_x=None, end_y=None, monitor_index=None, show_grid=True)` -> `list[Image | TextContent]`
Выполняет действие мышью с предиктивной подсветкой цели за 250 мс до клика.
- `action`: `"click"`, `"double_click"`, `"right_click"`, `"move"`, `"drag"`.
- `x`, `y`: Координаты на сжатом скриншоте (0–1280).

### `keyboard_action(text=None, hotkey=None, monitor_index=None, show_grid=True)` -> `list[Image | TextContent]`
Выполняет ввод текста или нажатие комбинаций клавиш.
- `text`: Текст для безопасной вставки через системный буфер (не зависит от раскладки RU/EN).
- `hotkey`: Сочетание клавиш (`"ctrl+s"`, `"alt+tab"`, `"enter"`, `"esc"`).

### `scroll_screen(direction="down", amount=3, x=None, y=None, monitor_index=None, show_grid=True)` -> `list[Image | TextContent]`
Скроллит колесико мыши в указанном направлении (`"up"`, `"down"`).

### `reset_user_override()` -> `str`
Сбрасывает защитный режим паузы `USER_OVERRIDE` в `IDLE` после согласования с человеком.

---

## Защита от конфликта с пользователем

Демон перехватывает физические движения мыши и нажатия клавиш на уровне Windows Low-Level Hooks:
1. Если человек сдвинул мышь более чем на 15 пикселей во время работы агента, выполнение немедленно блокируется.
2. Неоновая рамка меняет свечение на янтарно-желтое.
3. Агент получает статус `PAUSE` и обязан запросить разрешение на продолжение у пользователя.
4. Горячие клавиши `Ctrl + Alt + Space` на физической клавиатуре сбрасывают паузу вручную.
