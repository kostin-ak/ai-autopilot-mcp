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
    _drop_stale_physical_position = ic.InputController._drop_stale_physical_position
    _send_win32_mouse_input = ic.InputController._send_win32_mouse_input
    # click now resolves button flags through the shipped path, so the stub must not
    # reimplement it. Re-wrapped in staticmethod: reading it off the class yields the
    # plain function, and assigning that to another class would make it an instance
    # method taking self. `_send_win32_key` is deliberately NOT rebound -- the stub has
    # its own recording version, and overwriting it broke hotkey and unicode typing.
    _button_flags = staticmethod(ic.InputController._button_flags)
    click = ic.InputController.click
    scroll = ic.InputController.scroll


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

    def test_high_volume_text_tools_publish_no_output_schema(self):
        """The doubling fix has to cover text, not only base64 frames.

        Phase 1.2 removed the duplication from five screenshot tools and the
        guarantee was recorded as holding for "four `-> str` tools where the
        duplication of a short string is harmless". The surface then grew to
        fifteen `-> str` tools, including four that return a tree, a console
        buffer or an OCR page. Those are the most frequently called tools in the
        set, and each was paying for its payload twice.
        """
        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {t.name: t for t in tools}
        for name in ["get_ui_elements", "get_snapshot", "read_terminal", "read_screen_text"]:
            tool = by_name[name]
            schema = getattr(tool, "outputSchema", None) or getattr(
                tool, "output_schema", None
            )
            assert not schema, f"{name} publishes an output_schema -> text duplication"

    def test_short_text_tools_keep_their_schema(self):
        """Short strings stay structured: the rule is payload size, not annotation."""
        import mcp_server

        tools = asyncio.run(mcp_server.mcp.list_tools())
        by_name = {t.name: t for t in tools}
        tool = by_name["get_hud_state"]
        schema = getattr(tool, "outputSchema", None) or getattr(
            tool, "output_schema", None
        )
        assert schema, "a short string should not lose its output_schema"

    def test_text_tool_call_emits_no_structured_content(self, monkeypatch):
        import mcp_server

        async def fake_request(action, params):
            return {
                "status": "ok",
                "state": "AI_ACTIVE",
                "data": {
                    "handle": 1,
                    "host": "conhost",
                    "resolution": "unique",
                    "row_count": 3,
                    "char_count": 11,
                    "read_ms": 1.0,
                    "selected_count": 3,
                    "mode": "tail",
                    "lines": ["one", "two", "three"],
                },
            }

        monkeypatch.setattr(mcp_server, "_send_daemon_request", fake_request)

        async def run():
            return await mcp_server.mcp.call_tool("read_terminal", {})

        result = asyncio.run(run())
        structured = getattr(result, "structuredContent", None) or getattr(
            result, "structured_content", None
        )
        assert structured is None, "terminal buffer is duplicated into structured_content"
        assert result.content, "the text payload must still reach the model"


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
        # AI_ACTIVE, not IDLE. Control was acquired explicitly by take_control, and an
        # incidental mouse twitch followed by stillness is nobody's decision to give it
        # up. IDLE also turned the HUD frame off while mouse and keyboard actions kept
        # working, because the gate is on USER_OVERRIDE alone -- so the one visible
        # signal about who has the machine said "nobody" while commands were accepted.
        assert stub.state == AutopilotState.AI_ACTIVE
        assert stub.transitions == [AutopilotState.AI_ACTIVE]

    def test_a_manual_reset_still_goes_to_idle(self):
        """The two releases are different acts and must not converge.

        A human pressing Ctrl+Alt+Space or calling reset_override is clearing the pause,
        not asking the agent to resume acting on their behalf. Re-acquiring control is
        take_control's job.
        """
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        stub.reset_override()
        assert stub.state == AutopilotState.IDLE
        assert stub.transitions == [AutopilotState.IDLE]

    def test_the_two_releases_land_in_different_states(self, monkeypatch):
        """Pinned together because they share a helper and a stale-position drop; if one
        is ever rerouted through the other, the distinction this project just paid for
        disappears silently."""
        monkeypatch.setattr(ic, "seconds_since_physical_input", lambda: 10.0)
        auto = StubController(state=AutopilotState.USER_OVERRIDE)
        auto.maybe_auto_resume()
        manual = StubController(state=AutopilotState.USER_OVERRIDE)
        manual.reset_override()
        assert auto.state == AutopilotState.AI_ACTIVE
        assert manual.state == AutopilotState.IDLE
        assert auto.state != manual.state

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


# --------------------------------------------------------------------------
# 8. Silent acceptance in the input layer
# --------------------------------------------------------------------------
class TestInputValueRejection:
    """A malformed mouse request used to be quietly turned into a valid one.

    click() fell through to a left click for any unrecognized button, and scroll()
    treated any direction other than "up" as down and let a negative amount invert
    the result. The MCP gateway's Literal annotations shield the shipped tool
    surface, but the daemon forwards params verbatim, so a direct WebSocket call
    asking for button "back" received a left click and a success status. A left
    click is the destructive one, which makes the fallback worse than a failure.
    """

    def test_unknown_button_raises_and_sends_nothing(self, captured_sendinput):
        stub = StubController()
        with pytest.raises(ValueError, match="Неизвестная кнопка"):
            stub.click("back")
        assert captured_sendinput == [], "no mouse event may be sent for a bad button"

    def test_error_names_the_allowed_buttons(self, captured_sendinput):
        stub = StubController()
        with pytest.raises(ValueError) as excinfo:
            stub.click("forward")
        message = str(excinfo.value)
        for allowed in ("left", "right", "middle"):
            assert allowed in message

    @pytest.mark.parametrize("button", ["left", "right", "middle"])
    def test_known_buttons_still_send_down_and_up(self, captured_sendinput, button):
        stub = StubController()
        stub.click(button)
        assert len(captured_sendinput) == 2, "one down, one up"

    def test_unknown_direction_raises_and_sends_nothing(self, captured_sendinput):
        """Not "left": horizontal scrolling is supported now, and this test used to use
        it as the invalid example, which is exactly how a widened contract turns an old
        assertion into a false failure."""
        stub = StubController()
        with pytest.raises(ValueError, match="Неизвестное направление"):
            stub.scroll("sideways", 3)
        assert captured_sendinput == []

    def test_the_direction_set_and_the_handler_agree(self):
        """Four names are accepted, and the handler must have a branch for each. A name in
        the set with no branch would scroll the wrong axis rather than fail."""
        assert {"up", "down", "left", "right"} == ic.SCROLL_DIRECTIONS
        import inspect

        src = inspect.getsource(ic.InputController.scroll)
        assert "MOUSEEVENTF_HWHEEL" in src, "horizontal has no wheel flag of its own"

    def test_horizontal_uses_the_horizontal_wheel_flag(self):
        """Intercepted at the sender rather than at SendInput, so the assertion is about
        the flag chosen and not about how the capture fixture happens to record a struct."""
        stub = StubController()
        sent = []
        stub._send_win32_mouse_input = lambda flags, dx=0, dy=0, data=0: sent.append(
            (flags, data))
        stub.scroll("right", 2)
        assert sent == [(ic.MOUSEEVENTF_HWHEEL, 240)], sent
        stub.scroll("left", 2)
        assert sent[-1] == (ic.MOUSEEVENTF_HWHEEL, -240)

    def test_vertical_uses_the_vertical_wheel_flag(self):
        stub = StubController()
        sent = []
        stub._send_win32_mouse_input = lambda flags, dx=0, dy=0, data=0: sent.append(
            (flags, data))
        stub.scroll("up", 2)
        assert sent == [(ic.MOUSEEVENTF_WHEEL, 240)], sent
        stub.scroll("down", 2)
        assert sent[-1] == (ic.MOUSEEVENTF_WHEEL, -240)

    def test_the_two_wheel_flags_are_distinct(self):
        """If they were equal, a horizontal scroll would silently be a vertical one and
        every assertion above would pass anyway."""
        assert ic.MOUSEEVENTF_HWHEEL != ic.MOUSEEVENTF_WHEEL
        assert ic.MOUSEEVENTF_HWHEEL == 0x01000
        assert ic.MOUSEEVENTF_WHEEL == 0x0800

    def test_negative_amount_is_rejected(self, captured_sendinput):
        """amount=-3 with direction="up" used to scroll down: the sign inverted it."""
        stub = StubController()
        with pytest.raises(ValueError, match="отрицательным"):
            stub.scroll("up", -3)
        assert captured_sendinput == []

    def test_zero_amount_is_a_noop_not_an_error(self, captured_sendinput):
        stub = StubController()
        stub.scroll("down", 0)
        assert captured_sendinput == []

    @pytest.mark.parametrize("direction,expected", [("up", 360), ("down", -360)])
    def test_direction_still_selects_the_wheel_sign(
        self, captured_sendinput, direction, expected
    ):
        stub = StubController()
        stub.scroll(direction, 3)
        assert len(captured_sendinput) == 1
        # mouseData is a DWORD, so a negative wheel delta arrives wrapped. Comparing
        # against the wrapped value keeps the intent readable while matching what the
        # struct actually holds.
        assert captured_sendinput[0].union.mi.mouseData == expected & 0xFFFFFFFF

    def test_validation_happens_under_override_too(self):
        """The override check comes first, so a pause reports a pause, not a typo."""
        stub = StubController(state=AutopilotState.USER_OVERRIDE)
        with pytest.raises(ic.InterruptedByUserError):
            stub.click("back")


class TestWaitTransportTimeout:
    """The transport deadline for a waiting tool must outlive the tool's own.

    wait_terminal's own default deadline is 30 s and the gateway's default
    transport timeout was also 30 s, so the two raced. When the transport won, a
    caller got "daemon did not answer" instead of the structured "did not arrive,
    and here is what the buffer actually contained" -- strictly less information
    at the moment it matters most.
    """

    def test_transport_exceeds_the_polling_deadline(self):
        for deadline in (0.0, 3.0, 20.0, 30.0, 600.0):
            assert config.ws_timeout_for_wait(deadline) > deadline

    def test_margin_is_applied_uniformly(self):
        for deadline in (1.0, 45.0):
            assert (
                config.ws_timeout_for_wait(deadline) - deadline
                == config.WS_WAIT_MARGIN_SEC
            )

    def test_negative_deadline_does_not_shrink_the_transport_timeout(self):
        assert config.ws_timeout_for_wait(-5.0) == config.WS_WAIT_MARGIN_SEC
