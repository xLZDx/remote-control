"""Phase 4 UI modules: import smoke tests + non-UI logic checks."""
from __future__ import annotations

import importlib

import pytest


# Skip the whole module if PyQt6 isn't installed.
pytest.importorskip("PyQt6")


def test_role_picker_imports() -> None:
    importlib.import_module("app.shared.role_picker")


def test_connect_dialog_imports() -> None:
    mod = importlib.import_module("app.client.connect_dialog")
    assert hasattr(mod, "ConnectDialog")
    assert hasattr(mod, "FingerprintConfirmDialog")
    assert hasattr(mod, "ConnectInputs")


def test_viewer_window_imports() -> None:
    mod = importlib.import_module("app.client.viewer_window")
    assert hasattr(mod, "ViewerWindow")
    assert hasattr(mod, "VideoCanvas")


def test_host_tray_imports() -> None:
    mod = importlib.import_module("app.host.tray")
    assert hasattr(mod, "HostTray")


def test_host_pin_window_imports() -> None:
    mod = importlib.import_module("app.host.ui_pin")
    assert hasattr(mod, "HostPinWindow")


def test_host_main_imports() -> None:
    mod = importlib.import_module("app.host.main")
    assert hasattr(mod, "HostApp")
    assert hasattr(mod, "run")


def test_client_main_imports() -> None:
    mod = importlib.import_module("app.client.main")
    assert hasattr(mod, "ClientApp")
    assert hasattr(mod, "run")


def test_video_canvas_widget_to_video_coords_letterbox() -> None:
    """Mapping from widget coordinates to source video coordinates."""
    # Construct without showing - QApplication needed.
    from PyQt6.QtWidgets import QApplication
    qt = QApplication.instance() or QApplication([])
    from app.client.viewer_window import VideoCanvas

    canvas = VideoCanvas()
    canvas.resize(1600, 900)               # 16:9 widget
    canvas.set_frame(b"\x00" * (1920 * 1080 * 3), 1920, 1080)   # 16:9 video, letterbox-free
    # Center of widget should map to center of video
    coords = canvas.widget_to_video_coords(800, 450)
    assert coords is not None
    cx, cy = coords
    assert 950 < cx < 970     # center of 1920 = 959
    assert 530 < cy < 550     # center of 1080 = 539


def test_video_canvas_with_letterbox() -> None:
    from PyQt6.QtWidgets import QApplication
    qt = QApplication.instance() or QApplication([])
    from app.client.viewer_window import VideoCanvas

    canvas = VideoCanvas()
    # Widget is 4:3 (taller than 16:9 video) => letterbox top/bottom
    canvas.resize(1200, 900)
    canvas.set_frame(b"\x00" * (1920 * 1080 * 3), 1920, 1080)
    # Top-left of the visible video region (not the widget origin)
    # video aspect 1.778; widget aspect 1.333 -> wider widget would not letterbox,
    # but here widget is taller than video so wider rule applies (target_w=widget_w, target_h=int(widget_w/ar_video))
    # With our impl: ar_widget=1.333, ar_video=1.778, ar_widget < ar_video -> target_w = 1200, target_h = 675
    # offset y = (900 - 675) // 2 = 112
    coords = canvas.widget_to_video_coords(0, 112)
    assert coords is not None
    assert coords[0] == 0
    assert 0 <= coords[1] <= 5
