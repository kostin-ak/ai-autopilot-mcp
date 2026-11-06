"""Configuration settings for AI Desktop Autopilot (Overlay Daemon and MCP Gateway)."""

import contextlib
import logging
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("config")

# DPI awareness actually in force, set by set_process_dpi_awareness(). Read it rather
# than assuming: "we asked for V2" is not the same as "we have V2", and the previous
# code wrapped the call in contextlib.suppress(Exception), which is how the
# documentation came to promise Per-Monitor V2 while the process ran V1.
DPI_MODE: str = "unset"


def set_process_dpi_awareness() -> str:
    """Sets process DPI awareness, best available first, and reports what took effect.

    Order matters and is not a preference. Measured 2026-11-06:

      SetProcessDpiAwareness(2) first           succeeds, value becomes 2
      SetProcessDpiAwarenessContext(-4) after   ok=False, GetLastError=5 (ACCESS_DENIED)
      SetProcessDpiAwarenessContext(-4) first   ok=True
      SetProcessDpiAwareness(2) after           hr=0x80070005 (E_ACCESSDENIED)

    Awareness can be set once per process; the first call wins and every later call is
    refused. The daemon used to call the V1 API at import time, which permanently
    blocked the V2 upgrade the rest of the code and the documentation assumed. V2 must
    therefore be attempted first.

    Qt does not override awareness that is already set: with V2 established before
    QApplication, the reported value was unchanged afterwards. Qt sets its own
    awareness only when it finds none, so calling this before importing PyQt6 is what
    makes the process V2 rather than whatever Qt prefers.

    GetProcessDpiAwareness cannot confirm V2, because it predates V2 and reports 2 for
    both per-monitor levels. The evidence that V2 took is that the context call returned
    True and the subsequent V1 call was denied -- the denial proves awareness was already
    locked to whatever the first call set.
    """
    global DPI_MODE
    import ctypes
    from ctypes import wintypes

    if DPI_MODE != "unset":
        return DPI_MODE

    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore

    # Per-Monitor V2, Windows 10 1703+.
    with contextlib.suppress(Exception):
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        if user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4)):
            DPI_MODE = "per-monitor-v2"
            return DPI_MODE

    # Per-Monitor V1, Windows 8.1+.
    with contextlib.suppress(Exception):
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:
            DPI_MODE = "per-monitor-v1"
            logger.warning("Per-Monitor DPI V2 unavailable; running V1. Window "
                           "non-client areas and dialogs will not rescale on a DPI "
                           "change, so coordinates read across such a change can be "
                           "stale.")
            return DPI_MODE

    # System aware, Vista+. Better than unaware: unaware processes get bitmap-scaled
    # screenshots, which is what makes OCR unreliable.
    with contextlib.suppress(Exception):
        if user32.SetProcessDPIAware():
            DPI_MODE = "system"
            logger.warning("only system DPI awareness available; per-monitor scaling "
                           "will be wrong on a mixed-DPI setup")
            return DPI_MODE

    DPI_MODE = "unavailable"
    logger.error("could not set any DPI awareness; screenshots will be bitmap-scaled "
                 "and every coordinate derived from them is suspect")
    return DPI_MODE


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

# Named mutex for the single-instance guard. The "Local\" prefix scopes it to the
# terminal-server session rather than the whole machine, which is what a desktop
# automation daemon wants: two users in separate sessions are not two instances
# fighting over one cursor, and "Global\" would wrongly refuse the second of them.
INSTANCE_MUTEX_NAME: str = r"Local\ai_autopilot_daemon"
# Exit code when another instance already holds it. Distinct from a crash so a launcher
# script can tell "already running" from "failed to start".
EXIT_ALREADY_RUNNING: int = 3
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


# How far the gateway's transport timeout must sit above a waiting tool's own
# polling deadline. The waiting tools (wait_terminal, wait_for_text) are the only
# actions whose duration is the caller's choice, so a fixed table entry cannot
# cover them: with the transport timeout equal to the polling timeout the two race,
# and when the transport wins the caller gets a generic error instead of the
# structured "did not arrive, here is what the buffer actually says" result.
# Ten seconds is comfortably above one poll interval plus the round trip.
WS_WAIT_MARGIN_SEC: float = 10.0


def ws_timeout_for(action: str) -> float:
    """Resolves the response timeout for a daemon action."""
    return WS_ACTION_TIMEOUTS.get(action, WS_DEFAULT_TIMEOUT)


def ws_timeout_for_wait(timeout_s: float) -> float:
    """Transport timeout for a waiting action: the caller's deadline plus margin."""
    return max(timeout_s, 0.0) + WS_WAIT_MARGIN_SEC

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
#
# These were 250 / 36 / 15 and the marker was effectively invisible: a ring 72 px across
# on a 2560x1440 desktop, for a quarter of a second, with its alpha ramping to 12 of 240
# by the time it reached full size. The operator watching the screen during a live check
# on 2026-11-06 saw nothing and asked whether it ran at all. It did -- offscreen rendering
# against a blank surface showed 124 to 944 changed pixels across the animation -- which
# is the point: it was correct and unusable, and nothing in the tool's own output could
# have said so, because the overlay carries WDA_EXCLUDEFROMCAPTURE and appears in no
# screenshot.
#
# DURATION is not free: mouse_action sleeps for exactly this long between emitting the
# ripple and moving the cursor, so it is added to the latency of every mouse action.
TARGET_RIPPLE_DURATION_MS: int = 380
TARGET_RIPPLE_MAX_RADIUS: int = 84
TARGET_RIPPLE_TICK_MS: int = 12
# Fraction of the animation over which the marker fades out. The rest plays at full
# alpha. A linear ramp to zero -- what the code did before -- makes the largest and
# therefore most visible frames the faintest ones, which wastes exactly the part of the
# animation that could be seen.
TARGET_RIPPLE_FADE_FRACTION: float = 0.35

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
# USER_OVERRIDE auto-release
# ---------------------------------------------------------
# Without this, a pause was a dead end in practice: interrupting the agent took one
# physical mouse movement, but resuming took a conversation turn, because policy
# forbade calling reset_user_override without explicit human consent. The asymmetry
# had no safety benefit, since the interrupt trigger stays armed either way -- the
# human can always retake the machine instantly by touching the mouse.
#
# So USER_OVERRIDE is a pause, not a lock. After this many seconds without physical
# input the daemon returns the session to IDLE on its own and the agent may act again.
# Set to 0 to keep the old behaviour, where only an explicit reset resumes.
IDLE_RESUME_SECONDS: float = 5.0
# How often the daemon checks for idleness. Half a second keeps the resume latency
# close to the threshold without polling in a tight loop.
IDLE_RESUME_POLL_SECONDS: float = 0.5

# ---------------------------------------------------------
# Vision and Screenshot Settings
# ---------------------------------------------------------
TARGET_IMAGE_WIDTH: int = 1280
WEBP_QUALITY: int = 80
STABILIZATION_DIFF_THRESHOLD: float = 1.5
STABILIZATION_MAX_ITERATIONS: int = 10
STABILIZATION_INTERVAL_SEC: float = 0.10
STABILIZATION_RESIZE_DIMS: tuple[int, int] = (64, 64)

# Stabilisation strategy. "frames" is the original behaviour: capture repeatedly until
# the perceptual difference between consecutive frames settles, up to
# STABILIZATION_MAX_ITERATIONS captures at ~100 ms each. "events" waits for the UI's
# own event stream to go quiet and then captures once, which measured at about a
# millisecond of delivery instead of a 100 ms polling floor.
#
# "events" falls back to "frames" automatically whenever it cannot be used: the
# watcher failed to start, or no action supplied a tick to measure from. That fallback
# is the reason this is a mode and not a replacement -- an event stream that silently
# stops must degrade to the path that was working, not to a frame captured mid-animation.
STABILIZATION_MODE: str = "events"

# How long the UI's event stream must be silent before a frame is considered settled.
# Shorter than one frame-polling interval, which is the point: the old path could not
# answer faster than STABILIZATION_INTERVAL_SEC no matter how idle the UI was.
EVENT_QUIET_MS: float = 120.0

# Upper bound on an event wait. A UI that never settles (an animation, a spinner, a
# blinking caret) must still return a frame rather than hang the action.
EVENT_QUIET_TIMEOUT_S: float = 3.0

# The watcher is one dedicated thread with a system-wide hook. Disabling it returns
# the daemon to frame-difference stabilisation everywhere with no other change.
EVENT_WATCHER_ENABLED: bool = True

# Cursor Overlay Appearance on Screenshot
#
# The arrow's geometry lives in vision_engine._draw_cursor_pointer, whose vertices
# reach 21 px from the tip. A CURSOR_SIZE_PX constant used to sit here declaring 16
# and was read nowhere, so it described a cursor that does not exist -- removing it
# is better than leaving a number that invites someone to trust it.
CURSOR_COLOR_FILL: tuple[int, int, int] = (255, 0, 50)       # High-contrast Red
CURSOR_COLOR_OUTLINE: tuple[int, int, int] = (0, 0, 0)       # Black border

# How the pointer is shown in a captured frame.
#
# "native" composites the real system cursor, so the frame shows the shape the cursor
# actually has -- I-beam over text, wait while the application is busy, "no" over
# something that would refuse the action -- and occludes only what the cursor itself
# covers. "drawn" is the previous behaviour: a fixed red arrow polygon at the cursor
# position, which reported the same shape every time and painted over the pixels
# underneath, including the control the agent was about to click.
#
# Default is "native"; any failure in capturing the real cursor falls back to "drawn"
# rather than losing the frame, because a frame with an approximate marker beats no frame.
# Set to "drawn" to restore the old rendering without touching code.
CURSOR_RENDER_MODE: str = "native"

# Upper bound on hold_key's duration, in milliseconds. Capped rather than trusted: an
# unbounded hold keeps a key pressed and the daemon's input thread busy for as long as the
# caller asked, and nothing else in the system would explain why the keyboard had stopped
# behaving. Ten seconds covers a long press in any application that distinguishes one.
HOLD_KEY_MAX_MS: int = 10_000

# Upper bound on the plain wait action, in milliseconds. The condition-based waits
# (wait_for_event, wait_for_text, wait_terminal) return as soon as the thing happens; a
# fixed sleep returns after a guess. Capping it keeps the guess from becoming a way to
# block the daemon for an arbitrary time.
WAIT_MAX_MS: int = 30_000


# ---------------------------------------------------------
# Safety layer (ADR-0004)
# ---------------------------------------------------------
# Protection of the host's own process tree.
#
# On by default and not overridable from the gateway for coordinate actions, because
# a click by coordinate carries no element semantics -- nothing in the request says
# what it will land on. Tools that address a window explicitly accept
# allow_host_input, which the operator can pass deliberately; that asymmetry is the
# point.
HOST_GUARD_ENABLED: bool = True

# Applications in which a neutral action can still commit something: a message, a
# mail, a payment. In these, nothing is classified as freely repeatable, and Enter
# is treated as the send action rather than as navigation.
#
# This is a starting list, not a settled one. It is matched as a lowercase substring
# against both the process image name and the window title, so a client that renames
# itself per install has to be added here explicitly. Add rather than remove: the
# cost of an extra entry is a stricter level, the cost of a missing one is a message
# sent to the wrong recipient.
CRITICAL_APPS: tuple[str, ...] = (
    "telegram", "ayugram", "unigram", "whatsapp", "signal", "discord", "slack",
    "viber", "skype", "teams", "matrix", "element", "thunderbird", "outlook",
    "thebat", "почта", "телеграм",
)

# Audit trail.
#
# Bounded on purpose: an unbounded log on a machine left running for weeks is a disk
# fill waiting to happen. 8 MiB times five generations is roughly half a million
# records, which is far more than any single investigation needs.
TRACE_ENABLED: bool = True
TRACE_PATH: Path = Path(__file__).resolve().parent / "logs" / "autopilot_trace.jsonl"
TRACE_MAX_BYTES: int = 8 * 1024 * 1024
TRACE_KEEP: int = 5

# Whether to capture a frame *before* a mutating action so the trace carries both
# hashes. Off by default: the "after" hash is free because mutating actions already
# capture a frame, while the "before" hash needs a second capture that roughly
# doubles the cost of every action. Turn it on when investigating a mis-bind, which
# is exactly the case where seeing what the screen looked like beforehand matters.
TRACE_CAPTURE_BEFORE: bool = False

# Actions that never need classification because they change nothing.
READ_ONLY_ACTIONS: frozenset[str] = frozenset({
    "get_screen_state", "list_windows_and_monitors", "get_ui_elements",
    "get_snapshot", "probe_target", "resolve_clickable", "read_terminal",
    "wait_terminal", "read_screen_text", "find_text_on_screen", "wait_for_text",
    "get_hud_state",
})


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


