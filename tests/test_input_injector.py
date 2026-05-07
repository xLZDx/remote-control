"""
Tests for app.host.input_injector — patches SendInput so we can verify the
correct INPUT struct layout/flags without actually moving the cursor.

These tests run on any platform; the injector falls back to a stub on
non-Windows but we always pass a fake send_input_fn so we can introspect.
"""
from __future__ import annotations

from typing import Any

import ctypes

from app.host.input_injector import (
    InputInjector,
    INPUT_MOUSE, INPUT_KEYBOARD,
    MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_XDOWN, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL,
    KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY,
    XBUTTON1, XBUTTON2, WHEEL_DELTA,
    _INPUT,
)


class _FakeSendInput:
    """Captures every INPUT struct passed to SendInput for assertions."""
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, n: int, ptr, sz: int) -> int:
        # Materialize the INPUT struct from the pointer
        inp = ctypes.cast(ptr, ctypes.POINTER(_INPUT))[0]
        if inp.type == INPUT_MOUSE:
            self.calls.append({
                "type": "mouse",
                "dx": inp.mi.dx, "dy": inp.mi.dy,
                "mouseData": inp.mi.mouseData,
                "flags": inp.mi.dwFlags,
            })
        elif inp.type == INPUT_KEYBOARD:
            self.calls.append({
                "type": "key",
                "vk": inp.ki.wVk, "scan": inp.ki.wScan, "flags": inp.ki.dwFlags,
            })
        return n


def _injector() -> tuple[InputInjector, _FakeSendInput]:
    fake = _FakeSendInput()
    inj = InputInjector(send_input_fn=fake)
    # Force a known virtual screen (skip real GetSystemMetrics)
    inj._virtual_screen = lambda: (0, 0, 1920, 1080)  # type: ignore[assignment]
    return inj, fake


def test_mouse_move_absolute_normalizes_to_65535() -> None:
    inj, fake = _injector()
    assert inj.mouse_move(960, 540, absolute=True) is True
    assert len(fake.calls) == 1
    c = fake.calls[0]
    assert c["type"] == "mouse"
    assert c["flags"] == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
    # Halfway across 1919 -> ~32768
    assert 32700 < c["dx"] < 32800
    # Halfway down 1079 -> ~32768
    assert 32700 < c["dy"] < 32800


def test_mouse_move_relative() -> None:
    inj, fake = _injector()
    inj.mouse_move(5, -3, absolute=False)
    c = fake.calls[0]
    assert c["dx"] == 5 and c["dy"] == -3
    assert c["flags"] == MOUSEEVENTF_MOVE
    assert (c["flags"] & MOUSEEVENTF_ABSOLUTE) == 0


def test_mouse_left_down_up() -> None:
    inj, fake = _injector()
    inj.mouse_button("left", down=True)
    inj.mouse_button("left", down=False)
    assert fake.calls[0]["flags"] == MOUSEEVENTF_LEFTDOWN
    assert fake.calls[1]["flags"] == MOUSEEVENTF_LEFTUP


def test_mouse_x1_x2_set_xbutton_data() -> None:
    inj, fake = _injector()
    inj.mouse_button("x1", down=True)
    inj.mouse_button("x2", down=True)
    assert fake.calls[0]["mouseData"] == XBUTTON1
    assert fake.calls[0]["flags"] == MOUSEEVENTF_XDOWN
    assert fake.calls[1]["mouseData"] == XBUTTON2


def test_mouse_button_unknown_returns_false() -> None:
    inj, fake = _injector()
    assert inj.mouse_button("button42", down=True) is False
    assert fake.calls == []


def test_mouse_wheel_vertical() -> None:
    inj, fake = _injector()
    inj.mouse_wheel(0, WHEEL_DELTA)
    c = fake.calls[0]
    assert c["flags"] == MOUSEEVENTF_WHEEL
    assert c["mouseData"] == WHEEL_DELTA


def test_mouse_wheel_horizontal_negative() -> None:
    inj, fake = _injector()
    inj.mouse_wheel(-WHEEL_DELTA, 0)
    c = fake.calls[0]
    assert c["flags"] == MOUSEEVENTF_HWHEEL
    # negative deltas wrap as DWORD
    assert c["mouseData"] == ((-WHEEL_DELTA) & 0xFFFFFFFF)


def test_key_with_scancode_uses_scancode_flag() -> None:
    inj, fake = _injector()
    inj.key(vk=0x41, scan=30, down=True)            # 'A'
    c = fake.calls[0]
    assert c["scan"] == 30
    assert c["flags"] & KEYEVENTF_SCANCODE
    assert (c["flags"] & KEYEVENTF_KEYUP) == 0


def test_key_up_sets_keyup_flag() -> None:
    inj, fake = _injector()
    inj.key(vk=0x41, scan=30, down=False)
    c = fake.calls[0]
    assert c["flags"] & KEYEVENTF_KEYUP


def test_key_extended_flag() -> None:
    inj, fake = _injector()
    inj.key(vk=0x25, scan=0xE04B, down=True, extended=True)  # left arrow
    c = fake.calls[0]
    assert c["flags"] & KEYEVENTF_EXTENDEDKEY
    assert c["flags"] & KEYEVENTF_SCANCODE


def test_key_unicode_uses_unicode_flag() -> None:
    inj, fake = _injector()
    inj.key_unicode("A", down=True)
    c = fake.calls[0]
    assert c["flags"] & KEYEVENTF_UNICODE
    assert c["scan"] == ord("A")


def test_key_unicode_surrogate_pair_emits_two_events() -> None:
    inj, fake = _injector()
    # U+1F600 GRINNING FACE
    inj.key_unicode("\U0001F600", down=True)
    assert len(fake.calls) == 2
    for c in fake.calls:
        assert c["flags"] & KEYEVENTF_UNICODE


def test_dispatch_routes_mouse_move_abs() -> None:
    inj, fake = _injector()
    assert inj.dispatch({"kind": "mouse_move", "x": 100, "y": 50, "abs": True}) is True
    c = fake.calls[0]
    assert c["flags"] & MOUSEEVENTF_ABSOLUTE


def test_dispatch_routes_key_down() -> None:
    inj, fake = _injector()
    inj.dispatch({"kind": "key", "vk": 0x41, "scan": 30, "down": True})
    c = fake.calls[0]
    assert c["scan"] == 30
    assert (c["flags"] & KEYEVENTF_KEYUP) == 0


def test_dispatch_routes_key_unicode() -> None:
    inj, fake = _injector()
    inj.dispatch({"kind": "key_unicode", "char": "z", "down": True})
    c = fake.calls[0]
    assert c["flags"] & KEYEVENTF_UNICODE
    assert c["scan"] == ord("z")


def test_dispatch_unknown_kind_returns_false() -> None:
    inj, fake = _injector()
    assert inj.dispatch({"kind": "noop"}) is False
    assert fake.calls == []
