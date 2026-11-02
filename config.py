"""Configuration settings for AI Desktop Autopilot (Overlay Daemon and MCP Gateway)."""

import contextlib
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class AutopilotState(StrEnum):
    IDLE = "IDLE"
    AI_ACTIVE = "AI_ACTIVE"
    USER_OVERRIDE = "USER_OVERRIDE"


class RGBAColor(NamedTuple):
    r: int
    g: int
    b: int
    a: int = 255


# ---------------------------------------------------------
# Network and Security Settings
# ---------------------------------------------------------
WS_HOST: str = "127.0.0.1"
WS_PORT: int = 8765
TOKEN_FILE_PATH: Path = Path(__file__).resolve().parent / ".autopilot_token"
WS_HEARTBEAT_INTERVAL: float = 20.0
WS_OPEN_TIMEOUT: float = 5.0

# Per-action response timeouts, in seconds. A single global timeout is wrong in
# both directions: too short for a UIA tree walk on a large Electron app, and so
# long for a click that a hung daemon stalls the host for half a minute.
WS_DEFAULT_TIMEOUT: float = 30.0
WS_ACTION_TIMEOUTS: dict[str, float] = {
    "take_control": 15.0,
    "release_control": 10.0,
    "reset_override": 5.0,
    "get_screen_state": 20.0,
    "mouse_action": 25.0,
    "keyboard_action": 25.0,
    "scroll_screen": 25.0,
    "list_windows_and_monitors": 15.0,
    # Descendants walks on Chrome / VS Code / Electron can legitimately take a
    # while; this is the one action that needs a generous ceiling.
    "get_ui_elements": 90.0,
}


def ws_timeout_for(action: str) -> float:
    """Resolves the response timeout for a daemon action."""
    return WS_ACTION_TIMEOUTS.get(action, WS_DEFAULT_TIMEOUT)

# ---------------------------------------------------------
# HUD Overlay Settings
# ---------------------------------------------------------
HUD_BORDER_WIDTH: int = 7
HUD_GLOW_RADIUS: int = 16

# State Colors: (R, G, B, A)
COLOR_IDLE: RGBAColor = RGBAColor(0, 0, 0, 0)
COLOR_AI_ACTIVE_PRIMARY: RGBAColor = RGBAColor(58, 134, 255, 230)     # Neon Cyan / Electric Blue
COLOR_AI_ACTIVE_SECONDARY: RGBAColor = RGBAColor(123, 44, 191, 230)  # Violet / Purple
COLOR_USER_OVERRIDE: RGBAColor = RGBAColor(255, 183, 3, 235)         # Vivid Amber / Yellow

# Predictive Ripple Marker Settings
TARGET_RIPPLE_DURATION_MS: int = 250
TARGET_RIPPLE_MAX_RADIUS: int = 36
TARGET_RIPPLE_TICK_MS: int = 15

# ---------------------------------------------------------
# Input and Interruption Settings
# ---------------------------------------------------------
USER_OVERRIDE_MOUSE_DELTA_PX: float = 15.0
MOUSE_MOVE_DURATION_MS: int = 180
MOUSE_MOVE_STEPS: int = 18
CLIPBOARD_RESTORE_DELAY_SEC: float = 0.10

# Per-character delay for KEYEVENTF_UNICODE typing. Zero works for most
# applications, but some drop events sent strictly back-to-back.
UNICODE_CHAR_DELAY_SEC: float = 0.002

# Above this many characters, typing switches from per-character SendInput to a
# clipboard paste. Paste is the fallback, not the default: see
# InputController.type_text.
CLIPBOARD_TYPING_THRESHOLD: int = 4000

# Win32 Virtual Keys for reset hotkey (Ctrl + Alt + Space)
VK_CONTROL: int = 0x11
VK_MENU: int = 0x12  # Alt key
VK_SPACE: int = 0x20

# ---------------------------------------------------------
# Vision and Screenshot Settings
# ---------------------------------------------------------
TARGET_IMAGE_WIDTH: int = 1280
WEBP_QUALITY: int = 80
STABILIZATION_DIFF_THRESHOLD: float = 1.5
STABILIZATION_MAX_ITERATIONS: int = 10
STABILIZATION_INTERVAL_SEC: float = 0.10
STABILIZATION_RESIZE_DIMS: tuple[int, int] = (64, 64)

# Cursor Overlay Appearance on Screenshot
CURSOR_COLOR_FILL: tuple[int, int, int] = (255, 0, 50)       # High-contrast Red
CURSOR_COLOR_OUTLINE: tuple[int, int, int] = (0, 0, 0)       # Black border
CURSOR_SIZE_PX: int = 16


def configure_stdio_encoding() -> None:
    """Forces UTF-8 on stdout and stderr with replacement instead of exceptions.

    On a Russian-locale Windows, a piped ``sys.stdout`` reports ``cp1251``. The
    MCP SDK re-wraps *stdout* as UTF-8 before serving, so protocol traffic is
    safe -- but it does not touch *stderr*, and ``logging`` writes there. Any
    character outside cp1251 (a box-drawing glyph, an emoji, a checkmark) then
    raises ``UnicodeEncodeError`` from inside a log call, which can propagate
    into a tool invocation and fail it for a reason unrelated to the tool.

    ``errors="replace"`` guarantees a log line can never take the process down.
    """
    import sys

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")


def ensure_interactive_desktop() -> None:
    """Ensures current thread is attached to the user's interactive desktop and has Per-Monitor DPI awareness."""
    import contextlib
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # Per-Monitor DPI Awareness V2 (-4)
            with contextlib.suppress(Exception):
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))

            h_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
            buf = ctypes.create_unicode_buffer(256)
            user32.GetUserObjectInformationW(h_desk, 2, buf, 256, None)
            if buf.value.lower() != "default":
                GENERIC_ALL = 0x10000000
                h_interactive = user32.OpenDesktopW("default", 0, False, GENERIC_ALL)
                if h_interactive:
                    user32.SetThreadDesktop(h_interactive)
        except Exception:
            pass


