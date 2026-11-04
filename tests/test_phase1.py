"""Phase 1 regression tests.

Everything here is pure logic or monkeypatched at the ctypes boundary. No test
constructs a real InputController (that installs low-level OS hooks) and no test
moves the cursor, types into a window, or captures the screen.
"""

import asyncio
import base64
import ctypes
import sys

import pytest

import config
import input_controller as ic
from config import AutopilotState


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------
class StubController:
    """Stands in for InputController without touching the OS."""

    def __init__(self, state=AutopilotState.IDLE):
        self._state = state
        self.keys = []
        self.is_injecting = False
        self._inject_depth = 0
        self._inject_lock = __import__("threading").Lock()
        self._last_phys_x = None
        self._last_phys_y = None
        self._state_lock = __import__("threading").Lock()
        self.transitions = []
        # The real set_state reports through this callback, so recording here means
        # the stub exercises the shipped transition path unchanged.
        self._on_state_change = self.transitions.append

    @property
    def state(self):
        return self._state

    def _send_win32_key(self, vk, key_up=False):
        self.keys.append((vk, key_up))

    # Bound from the real class rather than reimplemented, so these tests exercise
    # the shipped state machine. Only the OS-facing parts are stubbed.
    _injecting = ic.InputController._injecting
    set_state = ic.InputController.set_state
    reset_override = ic.InputController.reset_override
    maybe_auto_resume = ic.InputController.maybe_auto_resume


@pytest.fixture
def captured_sendinput(monkeypatch):
    """Replaces SendInput with a recorder, returning copies of the INPUT structs.

    The copy matters. Indexing a POINTER(INPUT) yields a *view* onto the
    caller's buffer, not an owned object -- and the caller's INPUT is a local
    that dies as soon as the injection helper returns. Recording views produced
    garbage on read-back; memmove into a fresh struct keeps the data alive.
    """
    sent = []
    size = ctypes.sizeof(ic.INPUT)

    def fake_sendinput(count, ptr, _size):
        arr = ctypes.cast(ptr, ctypes.POINTER(ic.INPUT))
        for i in range(count):
            owned = ic.INPUT()
            ctypes.memmove(ctypes.byref(owned), ctypes.byref(arr[i]), size)
            sent.append(owned)
        return count

    monkeypatch.setattr(ctypes.windll.user32, "SendInput", fake_sendinput)
    return sent


# --------------------------------------------------------------------------
# 1. Hook flag semantics -- the LLKHF_EXTENDED / LLKHF_INJECTED regression
# --------------------------------------------------------------------------
class TestHookFlags:
    def test_injected_and_extended_are_distinct_bits(self):
        assert ic.LLKHF_INJECTED == 0x10
        assert ic.LLKHF_EXTENDED == 0x01
        assert ic.LLKHF_INJECTED != ic.LLKHF_EXTENDED

    @pytest.mark.parametrize(
        "flags, expected_injected",
        [
            (0x00, False),                      # plain physical key
            (ic.LLKHF_EXTENDED, False),         # physical arrow key -- was misread as injected
            (ic.LLKHF_INJECTED, True),          # our own injection
            (ic.LLKHF_INJECTED | ic.LLKHF_EXTENDED, True),
            (0x20, False),                      # LLKHF_ALTDOWN, physical
        ],
    )
    def test_injection_detection(self, flags, expected_injected):
        """The regression: 0x01 is EXTENDED, not INJECTED.

        Extended keys are right-hand Ctrl/Alt and the arrow/Home/End/nav cluster.
        Treating them as injected meant a physical press of any of them never
        triggered USER_OVERRIDE.
        """
        assert bool(flags & ic.LLKHF_INJECTED) is expected_injected

    def test_signature_is_stamped_in_every_mouse_event(self, captured_sendinput):
        stub = StubController()
        ic.InputController._send_win32_mouse_input(stub, ic.MOUSEEVENTF_LEFTDOWN)
        assert len(captured_sendinput) == 1
        assert captured_sendinput[0].union.mi.dwExtraInfo == ic.AUTOPILOT_SIGNATURE

    def test_signature_is_stamped_in_every_key_event(self, captured_sendinput):
        stub = StubController()
        ic.InputController._send_win32_key(stub, 0x41, key_up=False)
        ic.InputController._send_win32_key(stub, 0x41, key_up=True)
        assert len(captured_sendinput) == 2
        for inp in captured_sendinput:
            assert inp.union.ki.dwExtraInfo == ic.AUTOPILOT_SIGNATURE

    def test_signature_fits_in_ulong_ptr(self):
        assert 0 <= ic.AUTOPILOT_SIGNATURE < 2**64


# --------------------------------------------------------------------------
# 2. Coordinate normalisation across a negative-origin virtual desktop
# --------------------------------------------------------------------------
class TestCoordinateMath:
    def test_virtual_desktop_endpoints(self):
        """This machine: virtual desktop left=-1920, width=4480, height=1440."""
        user32 = ctypes.windll.user32
        v_left = user32.GetSystemMetrics(ic.SM_XVIRTUALSCREEN)
        v_top = user32.GetSystemMetrics(ic.SM_YVIRTUALSCREEN)
        v_w = user32.GetSystemMetrics(ic.SM_CXVIRTUALSCREEN)
        v_h = user32.GetSystemMetrics(ic.SM_CYVIRTUALSCREEN)

        stub = StubController()
        fn = ic.InputController._pixel_to_normalized_coords

        assert fn(stub, v_left, v_top) == (0, 0)
        assert fn(stub, v_left + v_w - 1, v_top + v_h - 1) == (65535, 65535)

    def test_secondary_monitor_maps_below_zero_boundary(self):
        """Negative desktop coordinates must not clamp or wrap."""
        stub = StubController()
        nx, ny = ic.InputController._pixel_to_normalized_coords(stub, -1000, 600)
        assert 0 <= nx <= 65535
        assert 0 <= ny <= 65535

    def test_monotonic_in_x(self):
        stub = StubController()
        fn = ic.InputController._pixel_to_normalized_coords
        prev = fn(stub, -1920, 0)[0]
        for x in range(-1920, 2560, 137):
            cur = fn(stub, x, 0)[0]
            assert cur >= prev, f"normalised x went backwards at {x}"
            prev = cur


# --------------------------------------------------------------------------
# 3. Hotkey parsing
# --------------------------------------------------------------------------
class TestHotkeyParsing:
    def test_modifier_plus_letter(self):
        stub = StubController()
        ic.InputController.press_hotkey(stub, "ctrl+c")
        downs = [vk for vk, up in stub.keys if not up]
        ups = [vk for vk, up in stub.keys if up]
        assert downs == [0x11, ord("C")]
        assert ups == [ord("C"), 0x11], "release order must be reverse of press order"

    def test_function_key(self):
        stub = StubController()
        ic.InputController.press_hotkey(stub, "f5")
        assert stub.keys == [(0x74, False), (0x74, True)]

    def test_single_named_key(self):
        stub = StubController()
        ic.InputController.press_hotkey(stub, "enter")
        assert stub.keys == [(0x0D, False), (0x0D, True)]

    def test_unknown_token_aborts_the_whole_combination(self):
        """Skipping unknown tokens sent real keystrokes for partial garbage.

        A live check with "not+a+key+combo" pressed the letter A: "a" is a
        single-character token and was sent while the other three were dropped.
        Nothing may be sent unless every token is understood.
        """
        stub = StubController()
        with pytest.raises(ValueError, match="Неизвестные токены"):
            ic.InputController.press_hotkey(stub, "notakey+ctrl")
        assert stub.keys == [], "no key may be sent when any token is unknown"

    def test_partial_garbage_does_not_press_the_tokens_it_did_understand(self):
        """The exact input that leaked a keystroke in the live check."""
        stub = StubController()
        with pytest.raises(ValueError):
            ic.InputController.press_hotkey(stub, "not+a+key+combo")
        assert 0x41 not in [vk for vk, _down in stub.keys], (
            "the letter A must not be pressed by a malformed combination"
        )
        assert stub.keys == []

    def test_error_names_the_unrecognized_tokens(self):
        stub = StubController()
        with pytest.raises(ValueError) as excinfo:
            ic.InputController.press_hotkey(stub, "ctrl+shifft+c")
        assert "shifft" in str(excinfo.value)
        assert "ctrl" not in str(excinfo.value).split("Распознано")[0].replace(
            "'ctrl+shifft+c'", "")

    def test_empty_token_is_rejected_not_ignored(self):
        stub = StubController()
        with pytest.raises(ValueError):
            ic.InputController.press_hotkey(stub, "ctrl+")
        assert stub.keys == []

    def test_out_of_range_function_key_is_rejected(self):
        """f99 previously computed a virtual key code that does not exist."""
        stub = StubController()
        with pytest.raises(ValueError):
            ic.InputController.press_hotkey(stub, "f99")
        assert stub.keys == []

    def test_valid_combination_still_sends(self):
        stub = StubController()
        ic.InputController.press_hotkey(stub, "ctrl+s")
        assert stub.keys == [(0x11, False), (0x53, False),
                             (0x53, True), (0x11, True)]

    def test_function_keys_within_range_still_work(self):
        stub = StubController()
        ic.InputController.press_hotkey(stub, "f12")
        assert stub.keys == [(0x6F + 12, False), (0x6F + 12, True)]

    def test_aborts_under_user_override(self):
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        with pytest.raises(ic.InterruptedByUserError):
            ic.InputController.press_hotkey(stub, "ctrl+c")
        assert stub.keys == []


# --------------------------------------------------------------------------
# 4. Unicode typing -- layout-free, clipboard-free
# --------------------------------------------------------------------------
class TestUnicodeTyping:
    def test_ascii_produces_one_downup_pair_per_char(self, captured_sendinput, monkeypatch):
        monkeypatch.setattr(config, "UNICODE_CHAR_DELAY_SEC", 0.0)
        stub = StubController()
        ic.InputController._send_unicode_text(stub, "ab")
        assert len(captured_sendinput) == 4
        for inp in captured_sendinput:
            assert inp.type == ic.INPUT_KEYBOARD
            assert inp.union.ki.wVk == 0
            assert inp.union.ki.dwFlags & ic.KEYEVENTF_UNICODE
        assert [i.union.ki.wScan for i in captured_sendinput[:2]] == [ord("a"), ord("a")]

    def test_cyrillic_bypasses_keyboard_layout(self, captured_sendinput, monkeypatch):
        """The point of KEYEVENTF_UNICODE: no dependence on RU/EN layout."""
        monkeypatch.setattr(config, "UNICODE_CHAR_DELAY_SEC", 0.0)
        stub = StubController()
        ic.InputController._send_unicode_text(stub, "Ф")
        assert captured_sendinput[0].union.ki.wScan == ord("Ф")

    def test_astral_char_splits_into_surrogate_pair(self, captured_sendinput, monkeypatch):
        monkeypatch.setattr(config, "UNICODE_CHAR_DELAY_SEC", 0.0)
        stub = StubController()
        emoji = "\U0001F600"
        ic.InputController._send_unicode_text(stub, emoji)

        raw = emoji.encode("utf-16-le")
        units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
        assert len(units) == 2
        assert 0xD800 <= units[0] <= 0xDBFF, "first unit must be a high surrogate"
        assert 0xDC00 <= units[1] <= 0xDFFF, "second unit must be a low surrogate"

        scans = [i.union.ki.wScan for i in captured_sendinput]
        assert len(scans) == 4, "two UTF-16 units, each a down/up pair"
        assert scans[0] == scans[1] == units[0]
        assert scans[2] == scans[3] == units[1]

    def test_newline_maps_to_vk_return(self, captured_sendinput, monkeypatch):
        """A raw U+000A does not act as Enter in most applications."""
        monkeypatch.setattr(config, "UNICODE_CHAR_DELAY_SEC", 0.0)
        stub = StubController()
        ic.InputController._send_unicode_text(stub, "\n")
        assert stub.keys == [(0x0D, False), (0x0D, True)]
        assert captured_sendinput == []

    def test_tab_maps_to_vk_tab(self, monkeypatch):
        monkeypatch.setattr(config, "UNICODE_CHAR_DELAY_SEC", 0.0)
        stub = StubController()
        ic.InputController._send_unicode_text(stub, "\t")
        assert stub.keys == [(0x09, False), (0x09, True)]


class TestTypeTextRouting:
    """type_text must route by size: unicode injection first, clipboard only for bulk."""

    @staticmethod
    def _instrument(stub, monkeypatch):
        calls = {"unicode": 0, "clipboard": 0}
        # Patched on the instance, not the class: the unicode-injection tests
        # above need the real InputController._send_unicode_text to stay intact.
        monkeypatch.setattr(
            stub, "_send_unicode_text",
            lambda text: calls.__setitem__("unicode", calls["unicode"] + 1),
            raising=False,
        )
        monkeypatch.setattr(
            stub, "_paste_via_clipboard",
            lambda text: calls.__setitem__("clipboard", calls["clipboard"] + 1),
            raising=False,
        )
        return calls

    def test_short_text_uses_unicode_not_clipboard(self, monkeypatch):
        stub = StubController()
        calls = self._instrument(stub, monkeypatch)
        ic.InputController.type_text(stub, "hello")
        assert calls == {"unicode": 1, "clipboard": 0}

    def test_threshold_boundary_still_uses_unicode(self, monkeypatch):
        stub = StubController()
        calls = self._instrument(stub, monkeypatch)
        ic.InputController.type_text(stub, "x" * config.CLIPBOARD_TYPING_THRESHOLD)
        assert calls == {"unicode": 1, "clipboard": 0}

    def test_long_text_falls_back_to_clipboard(self, monkeypatch):
        stub = StubController()
        calls = self._instrument(stub, monkeypatch)
        big = "x" * (config.CLIPBOARD_TYPING_THRESHOLD + 1)
        ic.InputController.type_text(stub, big)
        assert calls == {"unicode": 0, "clipboard": 1}

    def test_empty_text_is_a_noop(self, monkeypatch):
        stub = StubController()
        calls = self._instrument(stub, monkeypatch)
        ic.InputController.type_text(stub, "")
        assert calls == {"unicode": 0, "clipboard": 0}

    def test_aborts_under_user_override(self):
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        with pytest.raises(ic.InterruptedByUserError):
            ic.InputController.type_text(stub, "hello")


# --------------------------------------------------------------------------
# 5. Monitor index must error, never silently rescale
# --------------------------------------------------------------------------
class TestMonitorResolution:
    def _engine_with(self, monkeypatch, monitors):
        from vision_engine import VisionEngine

        eng = VisionEngine()
        monkeypatch.setattr(eng, "list_monitors", lambda: monitors)
        return eng

    def _fake_monitors(self):
        from vision_engine import MonitorInfo

        return [
            MonitorInfo(0, "All Displays", -1920, 0, 4480, 1440, False),
            MonitorInfo(1, "Display 1", 0, 0, 2560, 1440, True),
            MonitorInfo(2, "Display 2", -1920, 351, 1920, 1080, False),
        ]

    def test_out_of_range_raises(self, monkeypatch):
        from vision_engine import ScreenCaptureError

        eng = self._engine_with(monkeypatch, self._fake_monitors())
        with pytest.raises(ScreenCaptureError) as exc:
            eng.get_monitor_bounds(9)
        assert "does not exist" in str(exc.value)
        assert "Available indexes: 0, 1, 2" in str(exc.value)

    def test_negative_index_raises(self, monkeypatch):
        from vision_engine import ScreenCaptureError

        eng = self._engine_with(monkeypatch, self._fake_monitors())
        with pytest.raises(ScreenCaptureError):
            eng.get_monitor_bounds(-1)

    def test_none_selects_primary(self, monkeypatch):
        eng = self._engine_with(monkeypatch, self._fake_monitors())
        bbox, mon = eng.get_monitor_bounds(None)
        assert mon.is_primary
        assert bbox["width"] == 2560

    def test_scale_difference_is_why_fallback_was_fatal(self, monkeypatch):
        """2560 -> 1280 is 2.0x; 4480 -> 1280 is 3.5x.

        A silent fallback between them invalidates every coordinate the model
        has accumulated, with nothing in the response to reveal it.
        """
        eng = self._engine_with(monkeypatch, self._fake_monitors())
        primary, _ = eng.get_monitor_bounds(1)
        whole, _ = eng.get_monitor_bounds(0)
        w = config.TARGET_IMAGE_WIDTH
        assert primary["width"] / w == pytest.approx(2.0)
        assert whole["width"] / w == pytest.approx(3.5)


# --------------------------------------------------------------------------
# 6. Window title regex escaping
# --------------------------------------------------------------------------
class TestTitleEscaping:
    @pytest.mark.parametrize(
        "title",
        ["[admin] cmd", "report (final)", "C:\\path\\to", "a+b*c", "file.txt?"],
    )
    def test_metacharacters_do_not_break_the_pattern(self, title):
        import re

        pattern = f".*{re.escape(title)}.*"
        compiled = re.compile(pattern)
        assert compiled.search(f"window {title} - app")


# --------------------------------------------------------------------------
# 7. Gateway: timeouts and frame duplication
# --------------------------------------------------------------------------
class TestGateway:
    def test_per_action_timeouts(self):
        assert config.ws_timeout_for("get_ui_elements") > config.ws_timeout_for("mouse_action")
        assert config.ws_timeout_for("reset_override") < config.ws_timeout_for("mouse_action")
        assert config.ws_timeout_for("nonexistent") == config.WS_DEFAULT_TIMEOUT

    def test_stdio_encoding_is_utf8(self):
        config.configure_stdio_encoding()
        assert sys.stderr.encoding.lower().replace("-", "") == "utf8"
        "Русский \u2713 \u2500".encode(sys.stderr.encoding)  # must not raise

    def test_screenshot_tools_publish_no_output_schema(self):
        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {t.name: t for t in tools}
        image_tools = [
            "take_control", "get_screen_state", "mouse_action",
            "keyboard_action", "scroll_screen",
        ]
        for name in image_tools:
            schema = getattr(by_name[name], "outputSchema", None) or getattr(
                by_name[name], "output_schema", None
            )
            assert not schema, f"{name} publishes an output_schema -> frame duplication"

    def test_no_structured_content_duplication(self, monkeypatch):
        import mcp_server

        fake = base64.b64encode(b"\x9d" * 100_000).decode()

        async def fake_request(action, params):
            return {
                "status": "ok",
                "state": "AI_ACTIVE",
                "data": {
                    "screenshot": fake,
                    "scaled_width": 1280,
                    "scaled_height": 720,
                    "cursor": [1, 2],
                    "message": "probe",
                },
            }

        monkeypatch.setattr(mcp_server, "_send_daemon_request", fake_request)

        async def run():
            r = await mcp_server.mcp.call_tool("get_screen_state", {})
            return r

        result = asyncio.run(run())
        structured = getattr(result, "structuredContent", None) or getattr(
            result, "structured_content", None
        )
        assert structured is None, "frame is duplicated into structured_content"
        assert len(result.content) == 2
        assert type(result.content[0]).__name__ == "ImageContent"
# --------------------------------------------------------------------------
# USER_OVERRIDE auto-release after user stillness
# --------------------------------------------------------------------------
class TestIdleAutoResume:
    """A pause used to be a dead end in practice.

    Interrupting the agent took one physical mouse movement; resuming took a
    conversation turn, because policy forbade an unprompted reset_user_override. A
    live run by a second agent hit this: a single incidental movement blocked a Class
    B terminal write that never needed the cursor. The asymmetry bought no safety,
    since the interrupt stays armed either way.
    """

    def test_releases_once_the_human_is_still(self, monkeypatch):
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 10.0)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        assert stub.maybe_auto_resume() is True
        assert stub.state == AutopilotState.IDLE
        assert stub.transitions == [AutopilotState.IDLE]

    def test_stays_paused_while_input_is_recent(self, monkeypatch):
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 0.4)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        assert stub.maybe_auto_resume() is False
        assert stub.state == AutopilotState.USER_OVERRIDE
        assert stub.transitions == []

    def test_zero_threshold_restores_the_old_latched_behaviour(self, monkeypatch):
        monkeypatch.setattr(config, "IDLE_RESUME_SECONDS", 0.0)
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 9999.0)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        assert stub.maybe_auto_resume() is False
        assert stub.state == AutopilotState.USER_OVERRIDE

    def test_unavailable_idle_reading_keeps_the_pause(self, monkeypatch):
        """Failing closed: if stillness cannot be measured, do not resume."""
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: None)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        assert stub.maybe_auto_resume() is False
        assert stub.state == AutopilotState.USER_OVERRIDE

    @pytest.mark.parametrize("state", [AutopilotState.IDLE, AutopilotState.AI_ACTIVE])
    def test_other_states_are_untouched(self, monkeypatch, state):
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 9999.0)
        stub = StubController(state=state)
        assert stub.maybe_auto_resume() is False
        assert stub.state == state

    def test_drops_the_stale_cursor_position(self, monkeypatch):
        """reset_override clears it for a reason: a stale position makes the next
        physical movement compute a huge delta and re-trigger the pause instantly,
        so auto-release would bounce between states."""
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 10.0)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        stub._last_phys_x, stub._last_phys_y = 500, 500
        stub.maybe_auto_resume()
        assert stub._last_phys_x is None
        assert stub._last_phys_y is None

    def test_threshold_boundary_is_exclusive(self, monkeypatch):
        """Exactly at the threshold must not release; the human may be mid-motion."""
        monkeypatch.setattr(config, "IDLE_RESUME_SECONDS", 5.0)
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 4.999)
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        assert stub.maybe_auto_resume() is False
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 5.001)
        assert stub.maybe_auto_resume() is True


