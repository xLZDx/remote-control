"""
Map Qt input events to wire-format INPUT_EVENT JSON dicts.

The viewer window installs a Qt event filter that calls these helpers and
ships the dicts to the host via the TLS connection.

Key mapping covers ASCII letters/digits, function keys, arrows, modifiers,
and most special keys. For keys not in the table we fall back to
`key_unicode` so non-Latin layouts still work.
"""
from __future__ import annotations

from typing import Any, Optional

# Windows VK codes for everything we care about. Source: Microsoft docs.
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21         # PageUp
VK_NEXT = 0x22          # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_PRINT = 0x2A
VK_SNAPSHOT = 0x2C
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D
VK_NUMPAD0 = 0x60
VK_F1 = 0x70
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_DIVIDE = 0x6F

# Map Qt.Key.* (as integer constants) -> Windows VK.
# We accept either Qt.Key enum or its numeric value because the viewer code
# typically receives `event.key()` which returns int in PyQt6.

# Qt enum integer constants (PyQt6 uses these values; stable since Qt5):
_QT_KEY_TABLE: dict[int, int] = {
    0x01000000: VK_ESCAPE,       # Qt.Key_Escape
    0x01000001: VK_TAB,          # Qt.Key_Tab
    0x01000002: VK_TAB,          # Qt.Key_Backtab -> Tab (Shift handled separately)
    0x01000003: VK_BACK,         # Qt.Key_Backspace
    0x01000004: VK_RETURN,       # Qt.Key_Return
    0x01000005: VK_RETURN,       # Qt.Key_Enter (numpad)
    0x01000006: VK_INSERT,       # Qt.Key_Insert
    0x01000007: VK_DELETE,       # Qt.Key_Delete
    0x01000008: VK_PAUSE,        # Qt.Key_Pause
    0x01000009: VK_PRINT,        # Qt.Key_Print
    0x0100000B: VK_SNAPSHOT,     # Qt.Key_SysReq
    0x01000010: VK_HOME,         # Qt.Key_Home
    0x01000011: VK_END,          # Qt.Key_End
    0x01000012: VK_LEFT,         # Qt.Key_Left
    0x01000013: VK_UP,           # Qt.Key_Up
    0x01000014: VK_RIGHT,        # Qt.Key_Right
    0x01000015: VK_DOWN,         # Qt.Key_Down
    0x01000016: VK_PRIOR,        # Qt.Key_PageUp
    0x01000017: VK_NEXT,         # Qt.Key_PageDown
    0x01000020: VK_SHIFT,        # Qt.Key_Shift
    0x01000021: VK_CONTROL,      # Qt.Key_Control
    0x01000022: VK_LWIN,         # Qt.Key_Meta -> Windows key
    0x01000023: VK_MENU,         # Qt.Key_Alt
    0x01000024: VK_CAPITAL,      # Qt.Key_CapsLock
    0x01000025: VK_NUMLOCK,      # Qt.Key_NumLock
    0x01000026: VK_SCROLL,       # Qt.Key_ScrollLock
}
# F1..F24
for _i in range(24):
    _QT_KEY_TABLE[0x01000030 + _i] = VK_F1 + _i
# Space
_QT_KEY_TABLE[0x20] = VK_SPACE
# Digits 0..9
for _d in range(10):
    _QT_KEY_TABLE[ord("0") + _d] = ord("0") + _d
# Letters A..Z (Qt reports uppercase regardless of shift state)
for _c in range(26):
    _QT_KEY_TABLE[ord("A") + _c] = ord("A") + _c

# Set of VK codes that need the EXTENDED flag in scancode events.
EXTENDED_VKS = {
    VK_RETURN,   # actually only the numpad enter; we ship extended=True for Enter on the right side via Qt modifier
    VK_INSERT, VK_DELETE, VK_HOME, VK_END, VK_PRIOR, VK_NEXT,
    VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN,
    VK_NUMLOCK, VK_DIVIDE,
    VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN, VK_APPS,
}


def qt_key_to_vk(qt_key: int) -> Optional[int]:
    return _QT_KEY_TABLE.get(int(qt_key))


def map_keyevent(qt_key: int, qt_native_scancode: int, key_text: str, is_press: bool) -> dict[str, Any]:
    """
    Build a wire-format INPUT_EVENT for a Qt key press/release.

    qt_key: int (Qt.Key value)
    qt_native_scancode: int (Qt's nativeScanCode())
    key_text: the QKeyEvent.text() string (for Unicode fallback)
    is_press: True for keydown, False for keyup
    """
    vk = qt_key_to_vk(qt_key)
    if vk is None:
        # No VK mapping - fall back to Unicode injection if printable
        if key_text and not key_text.isspace():
            return {"kind": "key_unicode", "char": key_text[0], "down": is_press}
        # Nothing usable
        return {"kind": "key", "vk": 0, "scan": int(qt_native_scancode or 0), "down": is_press}

    return {
        "kind": "key",
        "vk": vk,
        "scan": int(qt_native_scancode or 0),
        "down": is_press,
        "extended": vk in EXTENDED_VKS,
    }


# Qt.MouseButton -> our string name. Numeric values per Qt5/6 stable enum.
_QT_BUTTONS: dict[int, str] = {
    0x00000001: "left",
    0x00000002: "right",
    0x00000004: "middle",
    0x00000008: "x1",     # Qt.BackButton
    0x00000010: "x2",     # Qt.ForwardButton
}


def qt_button_name(qt_button: int) -> Optional[str]:
    return _QT_BUTTONS.get(int(qt_button))


def map_mouse_button(qt_button: int, is_press: bool) -> Optional[dict[str, Any]]:
    name = qt_button_name(qt_button)
    if name is None:
        return None
    return {"kind": "mouse_button", "button": name, "down": is_press}


def map_mouse_move_abs(host_x: int, host_y: int) -> dict[str, Any]:
    return {"kind": "mouse_move", "x": int(host_x), "y": int(host_y), "abs": True}


def map_mouse_wheel(dx: int, dy: int) -> dict[str, Any]:
    return {"kind": "mouse_wheel", "dx": int(dx), "dy": int(dy)}
