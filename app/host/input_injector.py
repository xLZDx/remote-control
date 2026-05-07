"""
Windows input injection via SendInput.

Exposes a high-level InputInjector with mouse_move / mouse_button / mouse_wheel
/ key methods. Wire-format INPUT_EVENT messages are dispatched here from the
host server.

Wire format for INPUT_EVENT JSON (matches client capture):

    {"kind": "mouse_move",   "x": 1234, "y": 567, "abs": true}
    {"kind": "mouse_move",   "dx": 5,   "dy": -3, "abs": false}
    {"kind": "mouse_button", "button": "left"|"right"|"middle"|"x1"|"x2", "down": true}
    {"kind": "mouse_wheel",  "dx": 0, "dy": 120}    # 120 = one tick (WHEEL_DELTA)
    {"kind": "key",          "vk": 65, "scan": 30, "down": true, "extended": false}
    {"kind": "key_unicode",  "char": "A", "down": true}    # fallback for chars without VK
"""
from __future__ import annotations

import ctypes
import logging
import platform
from ctypes import wintypes
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------- Win32 SendInput plumbing ----------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

# MOUSEINPUT.dwFlags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

WHEEL_DELTA = 120

# KEYBDINPUT.dwFlags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# GetSystemMetrics indices
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    )


# ---------------- Public injector ----------------

# Buttons set with their down/up flag bits and extra mouseData.
_MOUSE_BUTTONS: dict[str, tuple[int, int, int]] = {
    # name -> (down_flag, up_flag, mouseData_value)
    "left":   (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "right":  (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "x1":     (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "x2":     (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


class InputInjector:
    """
    Calls SendInput on Windows. Falls back to a no-op on non-Windows (allows
    tests to import the module on any platform; real injection requires Win).
    """

    def __init__(self, send_input_fn: Callable | None = None) -> None:
        self._is_windows = platform.system() == "Windows"
        if send_input_fn is not None:
            self._SendInput = send_input_fn
        elif self._is_windows:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
            user32.SendInput.restype = wintypes.UINT
            self._SendInput = user32.SendInput
            self._user32 = user32
        else:
            self._SendInput = None
            self._user32 = None

    # ---------- mouse ----------

    def mouse_move(self, x: int, y: int, absolute: bool = True) -> bool:
        """
        Move the cursor. If absolute, x and y are pixel coordinates within the
        virtual screen; we normalize to 0..65535 with VIRTUALDESK semantics.
        """
        inp = _INPUT(type=INPUT_MOUSE)
        if absolute:
            vx, vy, vw, vh = self._virtual_screen()
            if vw <= 0 or vh <= 0:
                return False
            nx = int((x - vx) * 65535 / max(vw - 1, 1))
            ny = int((y - vy) * 65535 / max(vh - 1, 1))
            inp.mi = _MOUSEINPUT(
                dx=nx, dy=ny, mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0, dwExtraInfo=None,
            )
        else:
            inp.mi = _MOUSEINPUT(
                dx=int(x), dy=int(y), mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE,
                time=0, dwExtraInfo=None,
            )
        return self._send(inp)

    def mouse_button(self, button: str, down: bool) -> bool:
        spec = _MOUSE_BUTTONS.get(button)
        if spec is None:
            return False
        down_flag, up_flag, mdata = spec
        inp = _INPUT(type=INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(
            dx=0, dy=0, mouseData=mdata,
            dwFlags=(down_flag if down else up_flag),
            time=0, dwExtraInfo=None,
        )
        return self._send(inp)

    def mouse_wheel(self, dx: int, dy: int) -> bool:
        ok = True
        if dy:
            inp = _INPUT(type=INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(
                dx=0, dy=0, mouseData=int(dy) & 0xFFFFFFFF,
                dwFlags=MOUSEEVENTF_WHEEL,
                time=0, dwExtraInfo=None,
            )
            ok &= self._send(inp)
        if dx:
            inp = _INPUT(type=INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(
                dx=0, dy=0, mouseData=int(dx) & 0xFFFFFFFF,
                dwFlags=MOUSEEVENTF_HWHEEL,
                time=0, dwExtraInfo=None,
            )
            ok &= self._send(inp)
        return ok

    # ---------- keyboard ----------

    def key(self, vk: int, scan: int, down: bool, extended: bool = False) -> bool:
        flags = 0
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= KEYEVENTF_KEYUP
        # Use scancode when available (more reliable across keyboard layouts);
        # fall back to VK if no scancode.
        if scan:
            flags |= KEYEVENTF_SCANCODE
            wvk = 0
            wscan = scan & 0xFFFF
        else:
            wvk = vk & 0xFFFF
            wscan = 0
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(
            wVk=wvk, wScan=wscan, dwFlags=flags, time=0, dwExtraInfo=None,
        )
        return self._send(inp)

    def key_unicode(self, char: str, down: bool) -> bool:
        """Type a single Unicode character (works for non-ASCII)."""
        if not char:
            return False
        codepoint = ord(char[0])
        if codepoint > 0xFFFF:
            # surrogate pair
            cp = codepoint - 0x10000
            hi = 0xD800 + (cp >> 10)
            lo = 0xDC00 + (cp & 0x3FF)
            return self._send_unicode(hi, down) and self._send_unicode(lo, down)
        return self._send_unicode(codepoint, down)

    def _send_unicode(self, codepoint: int, down: bool) -> bool:
        flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(wVk=0, wScan=codepoint & 0xFFFF,
                             dwFlags=flags, time=0, dwExtraInfo=None)
        return self._send(inp)

    # ---------- helpers ----------

    def _send(self, inp: _INPUT) -> bool:
        if self._SendInput is None:
            return False
        try:
            n = self._SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
            return n == 1
        except Exception:
            logger.exception("SendInput failed")
            return False

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        if not self._is_windows or self._user32 is None:
            return (0, 0, 1920, 1080)
        gsm = self._user32.GetSystemMetrics
        gsm.argtypes = (ctypes.c_int,)
        gsm.restype = ctypes.c_int
        return (
            gsm(SM_XVIRTUALSCREEN),
            gsm(SM_YVIRTUALSCREEN),
            gsm(SM_CXVIRTUALSCREEN),
            gsm(SM_CYVIRTUALSCREEN),
        )

    # ---------- dispatch from wire-format JSON ----------

    def dispatch(self, event: dict) -> bool:
        kind = event.get("kind")
        if kind == "mouse_move":
            if event.get("abs"):
                return self.mouse_move(int(event.get("x", 0)), int(event.get("y", 0)), True)
            return self.mouse_move(int(event.get("dx", 0)), int(event.get("dy", 0)), False)
        if kind == "mouse_button":
            return self.mouse_button(str(event.get("button", "")), bool(event.get("down")))
        if kind == "mouse_wheel":
            return self.mouse_wheel(int(event.get("dx", 0)), int(event.get("dy", 0)))
        if kind == "key":
            return self.key(
                int(event.get("vk", 0)),
                int(event.get("scan", 0)),
                bool(event.get("down")),
                bool(event.get("extended", False)),
            )
        if kind == "key_unicode":
            return self.key_unicode(str(event.get("char", "")), bool(event.get("down")))
        logger.debug("unknown input event kind: %s", kind)
        return False
