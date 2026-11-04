"""Low-level OS input hooks, human override detection, and hardware SendInput execution."""

import contextlib
import ctypes
import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

import pyperclip

import config
from config import AutopilotState

logger = logging.getLogger(__name__)

# Apply Per-Monitor DPI Awareness before any coordinate calls
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


class InterruptedByUserError(Exception):
    """Raised when physical user activity interrupts an autonomous action."""


# =====================================================================
# Win32 Structures and Constants for LL Hooks and SendInput
# =====================================================================
if sys.platform == "win32":
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

    LRESULT = ctypes.c_ssize_t
    HHOOK = ctypes.c_void_p
    HOOKPROC = ctypes.WINFUNCTYPE(
        LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    ctypes.windll.user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    ctypes.windll.user32.CallNextHookEx.restype = LRESULT
    ctypes.windll.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    ctypes.windll.user32.SetWindowsHookExW.restype = HHOOK
    ctypes.windll.user32.UnhookWindowsHookEx.argtypes = [HHOOK]
    ctypes.windll.user32.UnhookWindowsHookEx.restype = wintypes.BOOL

    WH_KEYBOARD_LL: int = 13
    WH_MOUSE_LL: int = 14

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    ctypes.windll.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    ctypes.windll.user32.GetLastInputInfo.restype = wintypes.BOOL
    ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD

    INPUT_MOUSE: int = 0
    INPUT_KEYBOARD: int = 1

    MOUSEEVENTF_MOVE: int = 0x0001
    MOUSEEVENTF_LEFTDOWN: int = 0x0002
    MOUSEEVENTF_LEFTUP: int = 0x0004
    MOUSEEVENTF_RIGHTDOWN: int = 0x0008
    MOUSEEVENTF_RIGHTUP: int = 0x0010
    MOUSEEVENTF_MIDDLEDOWN: int = 0x0020
    MOUSEEVENTF_MIDDLEUP: int = 0x0040
    MOUSEEVENTF_WHEEL: int = 0x0800
    MOUSEEVENTF_VIRTUALDESK: int = 0x4000
    MOUSEEVENTF_ABSOLUTE: int = 0x8000

    KEYEVENTF_EXTENDEDKEY: int = 0x0001
    KEYEVENTF_KEYUP: int = 0x0002
    KEYEVENTF_UNICODE: int = 0x0004

    SM_XVIRTUALSCREEN: int = 76
    SM_YVIRTUALSCREEN: int = 77
    SM_CXVIRTUALSCREEN: int = 78
    SM_CYVIRTUALSCREEN: int = 79

    LLMHF_INJECTED: int = 0x00000001
    LLKHF_INJECTED: int = 0x00000010
    LLKHF_EXTENDED: int = 0x00000001

    # Stamped into dwExtraInfo of every event this process injects.
    #
    # Reading it back in the low-level hook is race-free. The boolean
    # ``is_injecting`` flag is not: it is cleared the moment SendInput returns,
    # while the hook callback may still be pending on the hook thread, which
    # made the daemon classify its own events as physical input and trip a
    # spurious USER_OVERRIDE. The flag is kept only as a secondary guard.
    #
    # Note this only works for SendInput. SetCursorPos cannot carry dwExtraInfo,
    # which is why cursor movement also goes through SendInput.
    AUTOPILOT_SIGNATURE: int = 0x4155544F  # ASCII "AUTO"

    VK_MAP = {
        "ctrl": 0x11,
        "control": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "win": 0x5B,
        "super": 0x5B,
        "enter": 0x0D,
        "return": 0x0D,
        "tab": 0x09,
        "space": 0x20,
        "backspace": 0x08,
        "delete": 0x2E,
        "esc": 0x1B,
        "escape": 0x1B,
        "up": 0x26,
        "down": 0x28,
        "left": 0x25,
        "right": 0x27,
        "pageup": 0x21,
        "pagedown": 0x22,
        "home": 0x24,
        "end": 0x23,
    }


def seconds_since_physical_input() -> float | None:
    """Seconds since the session last saw physical input, or None if unavailable.

    Defined outside the win32 block on purpose: placing it inside would end that block
    early and silently turn every constant below it into a local of this function.

    Counts mouse movement as well as clicks and keys, which is what makes it usable as
    a stillness test: a person reading the screen without touching anything is idle,
    while one moving the pointer while thinking is not.

    Injected input also lands in the session queue and would reset this clock, but the
    only caller is the USER_OVERRIDE release path, where the daemon injects nothing by
    definition.
    """
    if sys.platform != "win32":
        return None
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    now = ctypes.windll.kernel32.GetTickCount()
    # GetTickCount wraps after about 49.7 days; the unsigned difference survives it.
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


class InputController:
    """Manages low-level user input hooks, override state, and hardware input injection."""

    def __init__(self, on_state_change: Callable[[AutopilotState], None] | None = None) -> None:
        self._state: AutopilotState = AutopilotState.IDLE
        self._state_lock = threading.Lock()
        self._on_state_change = on_state_change

        # Hook thread management
        self._hook_thread: threading.Thread | None = None
        self._hook_thread_id: int = 0
        self._mouse_hook = None
        self._keyboard_hook = None
        self._stop_event = threading.Event()

        # Input tracking
        self._last_phys_x: int | None = None
        self._last_phys_y: int | None = None
        self.is_injecting: bool = False
        # Depth counter rather than a boolean: nested injection (drag calls
        # smooth_move and then sends button events) would otherwise have the
        # inner call clear the flag while the outer one is still running.
        self._inject_depth: int = 0
        self._inject_lock = threading.Lock()

        # Key state tracking for reset hotkey (Ctrl + Alt + Space)
        self._ctrl_down: bool = False
        self._alt_down: bool = False

        self._start_hooks()

    @property
    def state(self) -> AutopilotState:
        with self._state_lock:
            return self._state

    def set_state(self, new_state: AutopilotState) -> None:
        """Transitions state and triggers state change callback."""
        with self._state_lock:
            if self._state == new_state:
                return
            old_state = self._state
            self._state = new_state
            logger.info(f"InputController state transition: {old_state} -> {new_state}")

        if self._on_state_change:
            self._on_state_change(new_state)

    def reset_override(self) -> None:
        """Manually or hotkey resets USER_OVERRIDE back to IDLE."""
        if self.state == AutopilotState.USER_OVERRIDE:
            logger.info("Resetting USER_OVERRIDE back to IDLE.")
            # Drop the last physical position too. It is stale by definition --
            # the agent has usually moved the cursor since -- so leaving it in
            # place makes the very next physical movement compute a huge delta
            # and immediately re-trigger USER_OVERRIDE.
            self._last_phys_x = None
            self._last_phys_y = None
            self.set_state(AutopilotState.IDLE)

    def maybe_auto_resume(self) -> bool:
        """Releases USER_OVERRIDE on its own once the human stops touching input.

        Returns True when the state was actually released, so the caller can update
        the overlay.

        This makes the pause symmetric with the interrupt. Interrupting the agent has
        always taken a single physical mouse movement; resuming used to take a
        conversation turn, because policy forbade an unprompted reset_user_override.
        That asymmetry bought no safety: the interrupt trigger stays armed after an
        auto-release, so the human retakes the machine instantly by touching the
        mouse. What it did buy was a stall on every incidental movement.

        Idleness is measured with GetLastInputInfo, which counts any physical input in
        the session, including mouse movement -- so a human holding a key or moving
        while reading keeps the pause latched. Only genuine stillness releases it.
        """
        if self.state != AutopilotState.USER_OVERRIDE:
            return False
        threshold = config.IDLE_RESUME_SECONDS
        if threshold <= 0:
            return False
        idle = seconds_since_physical_input()
        if idle is None or idle < threshold:
            return False
        logger.info(
            f"USER_OVERRIDE auto-released after {idle:.1f} s without physical input "
            f"(threshold {threshold:g} s)."
        )
        # Delegates so the stale physical position is dropped too: leaving it makes
        # the next movement compute a huge delta and re-trigger immediately.
        self.reset_override()
        return True

    @contextlib.contextmanager
    def _injecting(self):
        """Marks a region as self-injection, reentrant-safe."""
        with self._inject_lock:
            self._inject_depth += 1
            self.is_injecting = True
        try:
            yield
        finally:
            with self._inject_lock:
                self._inject_depth -= 1
                if self._inject_depth == 0:
                    self.is_injecting = False

    def _start_hooks(self) -> None:
        """Starts OS-specific background hook thread."""
        if sys.platform == "win32":
            self._hook_thread = threading.Thread(
                target=self._win32_hook_loop, name="Win32HookThread", daemon=True
            )
            self._hook_thread.start()
        else:
            logger.info("Non-Windows OS: Using software fallback for user override detection.")

    def _win32_hook_loop(self) -> None:
        """Dedicated message pump thread for Windows low-level input hooks."""
        config.ensure_interactive_desktop()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._hook_thread_id = kernel32.GetCurrentThreadId()

        # Keep function references alive to avoid garbage collection crashes
        self._mouse_hook_proc = HOOKPROC(self._low_level_mouse_proc)
        self._keyboard_hook_proc = HOOKPROC(self._low_level_keyboard_proc)

        # For WH_MOUSE_LL and WH_KEYBOARD_LL, hMod can be NULL (None)
        self._mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_hook_proc, None, 0
        )
        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_hook_proc, None, 0
        )

        if not self._mouse_hook or not self._keyboard_hook:
            logger.error("Failed to install Windows Low-Level Hooks.")
            return

        logger.info("Windows Low-Level Hooks (Mouse & Keyboard) installed successfully.")

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            # Process Windows message queue for hooks
            res = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if res <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
        if self._keyboard_hook:
            user32.UnhookWindowsHookEx(self._keyboard_hook)
        logger.info("Windows Low-Level Hooks removed.")

    def _low_level_mouse_proc(self, nCode: int, wParam: wintypes.WPARAM, lParam: wintypes.LPARAM) -> int:
        """Evaluates mouse events for hardware flags."""
        if nCode >= 0:
            hook_struct = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

            # Race-free self-recognition. Checked before is_injecting, which may
            # already be cleared by the time this callback runs on the hook thread.
            if hook_struct.dwExtraInfo == AUTOPILOT_SIGNATURE:
                return ctypes.windll.user32.CallNextHookEx(
                    self._mouse_hook, nCode, wParam, lParam
                )

            # LLMHF_INJECTED covers injections by *other* software. Those are not
            # the human either, so they must not trip an override.
            is_injected = bool(hook_struct.flags & LLMHF_INJECTED)

            if not is_injected and not self.is_injecting:
                # Physical mouse event
                px = hook_struct.pt.x
                py = hook_struct.pt.y

                if self._last_phys_x is not None and self._last_phys_y is not None:
                    delta = math.hypot(px - self._last_phys_x, py - self._last_phys_y)
                    if (
                        delta >= config.USER_OVERRIDE_MOUSE_DELTA_PX
                        and self.state == AutopilotState.AI_ACTIVE
                    ):
                        logger.warning(
                            f"Physical mouse movement detected (delta={delta:.1f}px). "
                            "Switching to USER_OVERRIDE."
                        )
                        self.set_state(AutopilotState.USER_OVERRIDE)

                self._last_phys_x = px
                self._last_phys_y = py

        return ctypes.windll.user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)

    def _low_level_keyboard_proc(self, nCode: int, wParam: wintypes.WPARAM, lParam: wintypes.LPARAM) -> int:
        """Evaluates keyboard events for physical intervention and reset hotkey."""
        if nCode >= 0:
            hook_struct = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

            # Race-free self-recognition, and it also keeps our own injected
            # modifiers out of the reset-hotkey tracking below.
            if hook_struct.dwExtraInfo == AUTOPILOT_SIGNATURE:
                return ctypes.windll.user32.CallNextHookEx(
                    self._keyboard_hook, nCode, wParam, lParam
                )

            # LLKHF_INJECTED is 0x10. This check previously also tested 0x01,
            # which is LLKHF_EXTENDED -- the marker for right-hand Ctrl/Alt and
            # the arrow/Home/End/nav cluster. Conflating the two meant a physical
            # press of any extended key was treated as injected and never
            # triggered USER_OVERRIDE.
            is_injected = bool(hook_struct.flags & LLKHF_INJECTED)
            vk = hook_struct.vkCode

            # Track modifiers regardless of state
            is_key_down = (wParam == 0x0100 or wParam == 0x0104)  # WM_KEYDOWN or WM_SYSKEYDOWN

            if vk in (config.VK_CONTROL, 0xA2, 0xA3):
                self._ctrl_down = is_key_down
            elif vk in (config.VK_MENU, 0xA4, 0xA5):
                self._alt_down = is_key_down

            # Hotkey check: Ctrl + Alt + Space resets override
            if is_key_down and vk == config.VK_SPACE and self._ctrl_down and self._alt_down:
                logger.info("Ctrl + Alt + Space pressed: Resetting override state.")
                self.reset_override()
                return 1  # Consume hotkey

            if (
                not is_injected
                and not self.is_injecting
                and is_key_down
                and self.state == AutopilotState.AI_ACTIVE
            ):
                logger.warning(f"Physical key press detected (vk={vk:#x}). Switching to USER_OVERRIDE.")
                self.set_state(AutopilotState.USER_OVERRIDE)

        return ctypes.windll.user32.CallNextHookEx(self._keyboard_hook, nCode, wParam, lParam)

    # =================================================================
    # Hardware Injection Execution Methods
    # =================================================================
    def get_cursor_pos(self) -> tuple[int, int]:
        """Returns current cursor position (x, y) in desktop coordinates."""
        config.ensure_interactive_desktop()
        if sys.platform == "win32":
            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        return (0, 0)

    def _send_win32_mouse_input(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        """Injects hardware-level mouse event using Win32 SendInput."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = dx
        inp.union.mi.dy = dy
        inp.union.mi.mouseData = data
        inp.union.mi.dwFlags = flags
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = AUTOPILOT_SIGNATURE

        with self._injecting():
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_absolute_move(self, x: int, y: int) -> None:
        """Moves the cursor via SendInput in absolute virtual-desktop coordinates.

        Preferred over SetCursorPos for three reasons: it carries the
        dwExtraInfo signature that the hooks use to recognize our own events, it
        produces genuine WM_MOUSEMOVE traffic that OLE drag-and-drop requires,
        and MOUSEEVENTF_VIRTUALDESK makes it correct across monitors with
        negative origins.
        """
        nx, ny = self._pixel_to_normalized_coords(x, y)
        self._send_win32_mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            dx=nx,
            dy=ny,
        )

    def _pixel_to_normalized_coords(self, x: int, y: int) -> tuple[int, int]:
        """Converts screen pixel coordinates into normalized 0..65535 coordinates."""
        user32 = ctypes.windll.user32
        v_left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        v_top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        v_width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        v_height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        if v_width <= 0:
            v_width = 1920
        if v_height <= 0:
            v_height = 1080

        norm_x = int(((x - v_left) * 65535) / (v_width - 1))
        norm_y = int(((y - v_top) * 65535) / (v_height - 1))
        return norm_x, norm_y

    def smooth_move(self, target_x: int, target_y: int) -> None:
        """Moves cursor smoothly using ease-out interpolation, checking for interruption."""
        config.ensure_interactive_desktop()
        start_x, start_y = self.get_cursor_pos()
        steps = config.MOUSE_MOVE_STEPS
        duration = config.MOUSE_MOVE_DURATION_MS / 1000.0
        step_delay = duration / steps

        with self._injecting():
            for step in range(1, steps + 1):
                if self.state == AutopilotState.USER_OVERRIDE:
                    raise InterruptedByUserError("Mouse movement interrupted by physical human input.")

                # Ease-out quadratic interpolation
                t = step / float(steps)
                ease_t = 1.0 - (1.0 - t) ** 2
                cur_x = int(round(start_x + (target_x - start_x) * ease_t))
                cur_y = int(round(start_y + (target_y - start_y) * ease_t))

                if sys.platform == "win32":
                    self._send_absolute_move(cur_x, cur_y)
                time.sleep(step_delay)

            # Ensure exact landing
            if sys.platform == "win32":
                self._send_absolute_move(target_x, target_y)

    def click(self, button: str = "left") -> None:
        """Sends mouse down and up events."""
        if self.state == AutopilotState.USER_OVERRIDE:
            raise InterruptedByUserError("Click aborted: System is in USER_OVERRIDE mode.")

        if sys.platform == "win32":
            if button == "left":
                down = MOUSEEVENTF_LEFTDOWN
                up = MOUSEEVENTF_LEFTUP
            elif button == "right":
                down = MOUSEEVENTF_RIGHTDOWN
                up = MOUSEEVENTF_RIGHTUP
            elif button == "middle":
                down = MOUSEEVENTF_MIDDLEDOWN
                up = MOUSEEVENTF_MIDDLEUP
            else:
                down = MOUSEEVENTF_LEFTDOWN
                up = MOUSEEVENTF_LEFTUP

            self._send_win32_mouse_input(down)
            time.sleep(0.04)
            self._send_win32_mouse_input(up)

    def double_click(self) -> None:
        """Executes a double click with proper inter-click timing."""
        self.click("left")
        time.sleep(0.08)
        self.click("left")

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int) -> None:
        """Drags mouse from start to end coordinates."""
        self.smooth_move(start_x, start_y)
        if self.state == AutopilotState.USER_OVERRIDE:
            raise InterruptedByUserError("Drag aborted: Interrupted by human input.")

        self._send_win32_mouse_input(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.05)
        try:
            self.smooth_move(end_x, end_y)
            time.sleep(0.05)
        finally:
            self._send_win32_mouse_input(MOUSEEVENTF_LEFTUP)

    def scroll(self, direction: str = "down", amount: int = 3) -> None:
        """Scrolls mouse wheel up or down."""
        if self.state == AutopilotState.USER_OVERRIDE:
            raise InterruptedByUserError("Scroll aborted: System is in USER_OVERRIDE mode.")

        clicks = amount if direction == "up" else -amount
        wheel_delta = clicks * 120
        if sys.platform == "win32":
            self._send_win32_mouse_input(MOUSEEVENTF_WHEEL, data=wheel_delta)

    def _send_win32_key(self, vk: int, key_up: bool = False) -> None:
        """Sends synthetic keyboard key event."""
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.wScan = 0
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
        inp.union.ki.time = 0
        inp.union.ki.dwExtraInfo = AUTOPILOT_SIGNATURE

        with self._injecting():
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_unicode_text(self, text: str) -> None:
        """Types text as KEYEVENTF_UNICODE events, bypassing keyboard layout.

        Each character is sent as a down/up pair in a single SendInput call so
        the two events cannot be interleaved with anything else. Characters above
        U+FFFF are encoded as UTF-16 surrogate pairs, which is what the API
        expects. Newline and tab are mapped to their virtual keys because a raw
        U+000A does not act as Enter in most applications.
        """
        for ch in text:
            if self.state == AutopilotState.USER_OVERRIDE:
                raise InterruptedByUserError("Text typing interrupted by physical human input.")

            if ch in ("\n", "\r"):
                self._send_win32_key(VK_MAP["enter"], key_up=False)
                self._send_win32_key(VK_MAP["enter"], key_up=True)
                continue
            if ch == "\t":
                self._send_win32_key(VK_MAP["tab"], key_up=False)
                self._send_win32_key(VK_MAP["tab"], key_up=True)
                continue

            encoded = ch.encode("utf-16-le")
            units = [
                int.from_bytes(encoded[i : i + 2], "little")
                for i in range(0, len(encoded), 2)
            ]

            events = []
            for unit in units:
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                    inp = INPUT()
                    inp.type = INPUT_KEYBOARD
                    inp.union.ki.wVk = 0
                    inp.union.ki.wScan = unit
                    inp.union.ki.dwFlags = flags
                    inp.union.ki.time = 0
                    inp.union.ki.dwExtraInfo = AUTOPILOT_SIGNATURE
                    events.append(inp)

            arr = (INPUT * len(events))(*events)
            with self._injecting():
                ctypes.windll.user32.SendInput(
                    len(events), ctypes.byref(arr), ctypes.sizeof(INPUT)
                )
            time.sleep(config.UNICODE_CHAR_DELAY_SEC)

    def press_hotkey(self, hotkey_str: str) -> None:
        """Parses and executes key combinations like 'ctrl+c', 'alt+tab', 'enter'.

        An unrecognized token aborts the whole combination rather than being skipped.
        Skipping sent real keystrokes for partial garbage: a live check with
        "not+a+key+combo" pressed the letter A, because "a" is a single-character
        token and was sent while the other three were silently dropped. Nothing is
        sent unless every token is understood, and the message names the ones that
        were not, so a caller can tell a typo from a missing mapping.
        """
        if self.state == AutopilotState.USER_OVERRIDE:
            raise InterruptedByUserError("Hotkey aborted: System is in USER_OVERRIDE mode.")

        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        vks = []
        unknown = []
        for part in parts:
            if not part:
                unknown.append("<пусто>")
            elif part in VK_MAP:
                vks.append(VK_MAP[part])
            elif len(part) == 1:
                # Character key (A-Z, 0-9)
                vks.append(ord(part.upper()))
            elif (part.startswith("f") and part[1:].isdigit()
                    and 1 <= int(part[1:]) <= 24):
                # F1 - F24. Bounded because an out-of-range number yields a virtual
                # key code that does not exist, which would send a meaningless event.
                vks.append(0x6F + int(part[1:]))
            else:
                unknown.append(part)

        if unknown:
            raise ValueError(
                f"Неизвестные токены в hotkey {hotkey_str!r}: "
                f"{', '.join(unknown)}. Клавиши НЕ отправлены. "
                f"Распознано {len(vks)} из {len(parts)}."
            )
        if not vks:
            raise ValueError(
                f"hotkey {hotkey_str!r} не содержит ни одной клавиши; "
                "ничего не отправлено."
            )

        # Press all keys in order
        for vk in vks:
            self._send_win32_key(vk, key_up=False)
            time.sleep(0.02)

        time.sleep(0.04)

        # Release all keys in reverse order
        for vk in reversed(vks):
            self._send_win32_key(vk, key_up=True)
            time.sleep(0.02)

    def type_text(self, text: str) -> None:
        """Types text, layout-independent and clipboard-free for normal payloads.

        KEYEVENTF_UNICODE is the primary path. The clipboard is used only above
        ``CLIPBOARD_TYPING_THRESHOLD``, where per-character injection becomes
        measurably slow -- and it is the worse path, not merely the slower one:
        it destroys non-text clipboard formats, fails wherever Ctrl+V is not
        paste (vim, ssh sessions, many terminals, games), and contends for a
        single global resource the user may be using at the same time.
        """
        if self.state == AutopilotState.USER_OVERRIDE:
            raise InterruptedByUserError("Text typing aborted: System is in USER_OVERRIDE mode.")

        if not text:
            return

        if len(text) <= config.CLIPBOARD_TYPING_THRESHOLD:
            self._send_unicode_text(text)
            return

        self._paste_via_clipboard(text)

    def _paste_via_clipboard(self, text: str) -> None:
        """Fallback for very large payloads: paste through the clipboard."""
        old_clipboard = ""
        with contextlib.suppress(Exception):
            old_clipboard = pyperclip.paste()

        try:
            pyperclip.copy(text)
            time.sleep(0.03)
            paste_hotkey = "command+v" if sys.platform == "darwin" else "ctrl+v"
            self.press_hotkey(paste_hotkey)
            time.sleep(config.CLIPBOARD_RESTORE_DELAY_SEC)
        finally:
            # Restore original clipboard. Text-only round trip: non-text formats
            # held before the call are lost, which is one of the reasons this
            # path is a fallback rather than the default.
            with contextlib.suppress(Exception):
                pyperclip.copy(old_clipboard)

    def stop(self) -> None:
        """Stops background hook thread."""
        self._stop_event.set()
        if self._hook_thread_id and sys.platform == "win32":
            ctypes.windll.user32.PostThreadMessageW(self._hook_thread_id, 0x0012, 0, 0)  # WM_QUIT

