"""Tests for the fixes that came out of the live Blender session.

The session found three classes of defect: unicode text that never reaches
custom-drawn UIs, punctuation hotkeys that resolve to the wrong virtual key, and
frames too small to read dense UI. Each test names the live symptom it pins down.
"""

import threading

import pytest
from PIL import Image

import config
import vision_engine
from input_controller import InputController
from vision_engine import validate_crop


class TestHotkeyPunctuation:
    """A single-character token used to resolve through ord(upper).

    That is correct for A-Z and 0-9 only: "." became 0x2E, which is VK_DELETE, so a
    request for a decimal point pressed Delete. In the Blender session this made
    fractional values untypable in every number field.
    """

    def test_period_is_oem_period_not_delete(self):
        assert InputController._resolve_hotkey(".") == [0xBE]

    def test_comma_is_oem_comma(self):
        assert InputController._resolve_hotkey(",") == [0xBC]

    def test_minus_is_oem_minus(self):
        assert InputController._resolve_hotkey("-") == [0xBD]

    def test_named_punctuation_tokens(self):
        assert InputController._resolve_hotkey("period") == [0xBE]
        assert InputController._resolve_hotkey("slash") == [0xBF]
        assert InputController._resolve_hotkey("backslash") == [0xDC]

    def test_numpad_tokens(self):
        assert InputController._resolve_hotkey("numpad_period") == [0x6E]
        assert InputController._resolve_hotkey("numpad_plus") == [0x6B]

    def test_punctuation_combines_with_modifiers(self):
        assert InputController._resolve_hotkey("ctrl+.") == [0x11, 0xBE]

    def test_letters_and_digits_keep_their_codes(self):
        assert InputController._resolve_hotkey("a") == [0x41]
        assert InputController._resolve_hotkey("0") == [0x30]

    def test_non_ascii_letter_is_an_error_not_a_garbage_key(self):
        # ord("Я") is 0x42F, a virtual key that does not exist. Sending it was worse
        # than refusing: the target received an arbitrary keypress.
        with pytest.raises(ValueError):
            InputController._resolve_hotkey("я")


class TestTextModes:
    def _bare(self):
        # Bypasses __init__ on purpose: constructing a real controller installs
        # low-level hooks, which a unit test must not do.
        ctrl = object.__new__(InputController)
        ctrl._state = config.AutopilotState.IDLE
        ctrl._state_lock = threading.Lock()
        return ctrl

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            self._bare().type_text("abc", mode="telepathy")

    def test_vkeys_routes_to_vkey_path(self, monkeypatch):
        ctrl = self._bare()
        sent = []
        monkeypatch.setattr(ctrl, "_send_vkey_text", sent.append)
        monkeypatch.setattr(
            ctrl, "_send_unicode_text",
            lambda _t: pytest.fail("unicode path must not run in vkeys mode"),
        )
        ctrl.type_text("abc", mode="vkeys")
        assert sent == ["abc"]

    def test_unicode_mode_routes_to_unicode_path(self, monkeypatch):
        ctrl = self._bare()
        sent = []
        monkeypatch.setattr(ctrl, "_send_unicode_text", sent.append)
        monkeypatch.setattr(
            ctrl, "_send_vkey_text",
            lambda _t: pytest.fail("vkeys path must not run in unicode mode"),
        )
        ctrl.type_text("abc")
        assert sent == ["abc"]

    def test_unmappable_char_falls_back_to_unicode(self, monkeypatch):
        ctrl = self._bare()
        unicode_chars = []
        monkeypatch.setattr(ctrl, "_send_unicode_text", unicode_chars.append)
        monkeypatch.setattr(ctrl, "_char_to_vkey", lambda _ch, _hkl: (None, 0))
        ctrl._send_vkey_text("Ж")
        assert unicode_chars == ["Ж"]

    def test_vkeys_sends_plain_keydowns_not_scancode_events(self, monkeypatch):
        # Measured live: Blender's GHOST text fields accept an ordinary virtual-key
        # keydown and reject the same key carrying KEYEVENTF_SCANCODE. The vkeys path
        # must therefore send exactly the hotkey event shape.
        ctrl = self._bare()
        keys = []
        monkeypatch.setattr(
            ctrl, "_send_win32_key",
            lambda vk, key_up=False: keys.append((vk, key_up)))
        monkeypatch.setattr(ctrl, "_char_to_vkey", lambda _ch, _hkl: (0x43, 0))
        monkeypatch.setattr(ctrl, "_foreground_keyboard_layout", lambda: 0)
        ctrl._send_vkey_text("c")
        assert keys == [(0x43, False), (0x43, True)]

    def test_vkeys_holds_shift_for_uppercase(self, monkeypatch):
        ctrl = self._bare()
        keys = []
        monkeypatch.setattr(
            ctrl, "_send_win32_key",
            lambda vk, key_up=False: keys.append((vk, key_up)))
        monkeypatch.setattr(ctrl, "_char_to_vkey", lambda _ch, _hkl: (0x43, 0x01))
        monkeypatch.setattr(ctrl, "_foreground_keyboard_layout", lambda: 0)
        ctrl._send_vkey_text("C")
        assert keys == [(0x10, False), (0x43, False), (0x43, True), (0x10, True)]

    def test_char_to_vkey_marshals_the_character_not_a_pointer(self):
        # Regression: a bare str argument made ctypes pass a pointer, every character
        # came back unmappable, and the whole string fell through to the unicode path.
        # "1" exists in every layout, so the assertion holds on RU and EN alike.
        import ctypes

        ctrl = object.__new__(InputController)
        user32 = ctypes.windll.user32
        hkl = user32.GetKeyboardLayout(
            user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None))
        vk, mods = ctrl._char_to_vkey("1", hkl)
        assert vk == 0x31
        assert mods == 0


class TestCropValidation:
    def test_valid_crop_is_returned(self):
        assert validate_crop([10, 20, 200, 150], 1280, 720) == (10, 20, 200, 150)

    def test_out_of_frame_crop_raises(self):
        with pytest.raises(ValueError):
            validate_crop([0, 0, 1281, 720], 1280, 720)

    def test_degenerate_crop_raises(self):
        # An empty crop encodes to an empty image, which reads as a blank screen
        # rather than a bad request.
        with pytest.raises(ValueError):
            validate_crop([100, 100, 100, 200], 1280, 720)

    def test_non_integer_crop_raises(self):
        with pytest.raises(ValueError):
            validate_crop([0.5, 0, 100, 100], 1280, 720)

    def test_wrong_shape_raises(self):
        with pytest.raises(ValueError):
            validate_crop([0, 0, 100], 1280, 720)


class TestRegionDiff:
    def test_identical_regions_score_zero(self):
        img = Image.new("RGB", (40, 40), (10, 20, 30))
        assert vision_engine.VisionEngine.region_diff_score(img, img.copy()) == 0.0

    def test_changed_region_scores_above_zero(self):
        a = Image.new("RGB", (40, 40), (0, 0, 0))
        b = Image.new("RGB", (40, 40), (255, 255, 255))
        assert vision_engine.VisionEngine.region_diff_score(a, b) > 200

    def test_masked_cursor_does_not_count_as_change(self):
        # The cursor always moves to the click target; without masking, every click
        # would report "changed" for the pointer alone.
        a = Image.new("RGB", (100, 100), (0, 0, 0))
        b = a.copy()
        from PIL import ImageDraw

        ImageDraw.Draw(b).rectangle([10, 10, 30, 30], fill=(255, 255, 255))
        assert vision_engine.VisionEngine.region_diff_score(
            a, b, mask_points=[(20, 20)], mask_radius=24) == 0.0

    def test_size_mismatch_is_not_a_change_signal(self):
        a = Image.new("RGB", (40, 40))
        b = Image.new("RGB", (41, 40))
        assert vision_engine.VisionEngine.region_diff_score(a, b) == -1.0
