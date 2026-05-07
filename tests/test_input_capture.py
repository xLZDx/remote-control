"""Pure-function tests for Qt -> wire-format input mapping."""
from __future__ import annotations

from app.client.input_capture import (
    map_keyevent, map_mouse_button, map_mouse_move_abs, map_mouse_wheel,
    qt_key_to_vk, qt_button_name,
    VK_RETURN, VK_ESCAPE, VK_LEFT, VK_F1, VK_SHIFT, VK_CONTROL,
    EXTENDED_VKS,
)


def test_qt_key_to_vk_letters_and_digits() -> None:
    assert qt_key_to_vk(ord("A")) == ord("A")
    assert qt_key_to_vk(ord("Z")) == ord("Z")
    assert qt_key_to_vk(ord("0")) == ord("0")
    assert qt_key_to_vk(ord("9")) == ord("9")


def test_qt_key_to_vk_specials() -> None:
    assert qt_key_to_vk(0x01000000) == VK_ESCAPE
    assert qt_key_to_vk(0x01000004) == VK_RETURN
    assert qt_key_to_vk(0x01000012) == VK_LEFT
    assert qt_key_to_vk(0x01000020) == VK_SHIFT
    assert qt_key_to_vk(0x01000021) == VK_CONTROL


def test_qt_key_to_vk_function_keys() -> None:
    assert qt_key_to_vk(0x01000030) == VK_F1
    assert qt_key_to_vk(0x0100003B) == VK_F1 + 11   # F12


def test_qt_key_to_vk_unknown_returns_none() -> None:
    assert qt_key_to_vk(0xDEADBEEF) is None


def test_map_keyevent_ascii_letter() -> None:
    ev = map_keyevent(qt_key=ord("A"), qt_native_scancode=30, key_text="a", is_press=True)
    assert ev["kind"] == "key"
    assert ev["vk"] == ord("A")
    assert ev["scan"] == 30
    assert ev["down"] is True
    assert ev["extended"] is False


def test_map_keyevent_arrow_is_extended() -> None:
    ev = map_keyevent(qt_key=0x01000012, qt_native_scancode=0xE04B, key_text="", is_press=True)
    assert ev["vk"] == VK_LEFT
    assert ev["extended"] is True


def test_map_keyevent_release() -> None:
    ev = map_keyevent(qt_key=ord("A"), qt_native_scancode=30, key_text="", is_press=False)
    assert ev["down"] is False


def test_map_keyevent_unicode_fallback() -> None:
    """Non-mapped key with printable text should fall back to key_unicode."""
    ev = map_keyevent(qt_key=0xDEAD, qt_native_scancode=0, key_text="é", is_press=True)
    assert ev["kind"] == "key_unicode"
    assert ev["char"] == "é"


def test_map_keyevent_unmapped_no_text_returns_zero_vk() -> None:
    ev = map_keyevent(qt_key=0xDEAD, qt_native_scancode=42, key_text="", is_press=True)
    assert ev["kind"] == "key"
    assert ev["vk"] == 0
    assert ev["scan"] == 42


def test_qt_button_name() -> None:
    assert qt_button_name(0x01) == "left"
    assert qt_button_name(0x02) == "right"
    assert qt_button_name(0x04) == "middle"
    assert qt_button_name(0x08) == "x1"
    assert qt_button_name(0x10) == "x2"
    assert qt_button_name(0xFF) is None


def test_map_mouse_button() -> None:
    assert map_mouse_button(0x01, True) == {"kind": "mouse_button", "button": "left", "down": True}
    assert map_mouse_button(0x02, False) == {"kind": "mouse_button", "button": "right", "down": False}
    assert map_mouse_button(0xFF, True) is None


def test_map_mouse_move_abs() -> None:
    assert map_mouse_move_abs(100, 200) == {"kind": "mouse_move", "x": 100, "y": 200, "abs": True}


def test_map_mouse_wheel() -> None:
    assert map_mouse_wheel(0, 120) == {"kind": "mouse_wheel", "dx": 0, "dy": 120}


def test_extended_vks_includes_arrows_and_navigation() -> None:
    assert VK_LEFT in EXTENDED_VKS
    assert 0x26 in EXTENDED_VKS  # VK_UP
    assert 0x27 in EXTENDED_VKS  # VK_RIGHT
    assert 0x28 in EXTENDED_VKS  # VK_DOWN
    assert 0x24 in EXTENDED_VKS  # VK_HOME
    assert 0x23 in EXTENDED_VKS  # VK_END

