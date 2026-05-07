"""
Viewer window: paints decoded video frames and captures input events.

Threading model:
- Qt main thread owns the window, painting, and input capture.
- A worker thread runs the asyncio HostClient (network) and decoder loop.
- Decoded frames cross thread boundary via Qt signal -> slot (Qt.QueuedConnection).
- Outgoing input events are pushed into a thread-safe queue that the worker
  drains and sends asynchronously.
"""
from __future__ import annotations

import logging
import queue
from typing import Any, Callable

from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QImage, QPainter, QResizeEvent, QPaintEvent, QMouseEvent, QKeyEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QToolBar,
    QWidget, QVBoxLayout,
)

from app.client import input_capture

logger = logging.getLogger(__name__)


class VideoCanvas(QWidget):
    """Inner widget that paints the latest decoded frame, scaled to fit."""

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QWidget().sizePolicy())  # default expanding-ish
        self.setMinimumSize(320, 240)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._video_w = 0
        self._video_h = 0

    def set_frame(self, rgb_bytes: bytes, w: int, h: int) -> None:
        # Construct QImage referencing the bytes; copy() to detach from buffer.
        img = QImage(rgb_bytes, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        self._image = img
        self._video_w = w
        self._video_h = h
        self.update()

    def widget_to_video_coords(self, x: int, y: int) -> tuple[int, int] | None:
        """Map a widget-local (x, y) to source video coordinates."""
        if self._video_w <= 0 or self._video_h <= 0:
            return None
        target = self._fit_rect()
        if target.width() <= 0 or target.height() <= 0:
            return None
        rel_x = (x - target.x()) / target.width()
        rel_y = (y - target.y()) / target.height()
        if not (0.0 <= rel_x <= 1.0 and 0.0 <= rel_y <= 1.0):
            return None
        return int(rel_x * (self._video_w - 1)), int(rel_y * (self._video_h - 1))

    def _fit_rect(self):
        from PyQt6.QtCore import QRect
        wr = self.width()
        hr = self.height()
        if self._video_w <= 0 or self._video_h <= 0:
            return QRect(0, 0, wr, hr)
        ar_video = self._video_w / self._video_h
        ar_widget = wr / max(hr, 1)
        if ar_widget > ar_video:
            # widget is wider -> letterbox left/right
            target_h = hr
            target_w = int(target_h * ar_video)
            x = (wr - target_w) // 2
            return QRect(x, 0, target_w, target_h)
        else:
            target_w = wr
            target_h = int(target_w / ar_video)
            y = (hr - target_h) // 2
            return QRect(0, y, target_w, target_h)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is not None:
            p.drawImage(self._fit_rect(), self._image)
        p.end()


class ViewerWindow(QMainWindow):
    """
    Main client window. Hosts the VideoCanvas and emits input-event dicts on
    the `input_event` signal for the network worker to ship over the wire.

    `disconnect_requested` is emitted when the user closes the window or
    clicks Disconnect.
    """
    input_event = pyqtSignal(dict)
    disconnect_requested = pyqtSignal()
    monitor_change_requested = pyqtSignal(int)

    def __init__(self, host_name: str = "host", monitors: list[dict] | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"RemoteControl - {host_name}")
        self.resize(1280, 720)

        # Toolbar
        tb = QToolBar()
        self.addToolBar(tb)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_requested)
        tb.addWidget(self.disconnect_btn)

        self.fullscreen_btn = QPushButton("Fullscreen")
        self.fullscreen_btn.setCheckable(True)
        self.fullscreen_btn.toggled.connect(self._toggle_fullscreen)
        tb.addWidget(self.fullscreen_btn)

        # Monitor selector (filled when monitors known)
        self._monitor_buttons: list[QPushButton] = []
        self._monitors_holder = QWidget()
        mh_layout = QHBoxLayout(self._monitors_holder)
        mh_layout.setContentsMargins(0, 0, 0, 0)
        tb.addWidget(self._monitors_holder)
        self.set_monitors(monitors or [])

        tb.addSeparator()
        self.send_input_chk_label = QLabel("Sending input")
        tb.addWidget(self.send_input_chk_label)

        # Canvas
        self.canvas = VideoCanvas()
        self.setCentralWidget(self.canvas)
        self.canvas.installEventFilter(self)
        self.canvas.setFocus()

        # Status
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._latency_ms = 0

    def set_monitors(self, monitors: list[dict]) -> None:
        layout = self._monitors_holder.layout()
        # clear
        while layout.count():
            it = layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        self._monitor_buttons.clear()
        if len(monitors) <= 1:
            return
        for mon in monitors:
            idx = int(mon.get("index", 0))
            name = str(mon.get("name", f"Display {idx + 1}"))
            btn = QPushButton(f"{name}")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, i=idx: self.monitor_change_requested.emit(i))
            layout.addWidget(btn)
            self._monitor_buttons.append(btn)
        if self._monitor_buttons:
            self._monitor_buttons[0].setChecked(True)

    def set_status(self, text: str) -> None:
        self.status.showMessage(text)

    @pyqtSlot(bytes, int, int)
    def display_frame(self, rgb_bytes: bytes, w: int, h: int) -> None:
        self.canvas.set_frame(rgb_bytes, w, h)

    def _toggle_fullscreen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    # ---------- input capture via event filter ----------

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self.canvas:
            t = event.type()
            try:
                from PyQt6.QtCore import QEvent
            except Exception:
                return super().eventFilter(obj, event)

            if t == QEvent.Type.MouseMove:
                self._on_mouse_move(event)
                return False
            if t == QEvent.Type.MouseButtonPress:
                self._on_mouse_button(event, True)
                return False
            if t == QEvent.Type.MouseButtonRelease:
                self._on_mouse_button(event, False)
                return False
            if t == QEvent.Type.Wheel:
                self._on_wheel(event)
                return False
            if t == QEvent.Type.KeyPress:
                self._on_key(event, True)
                return True   # eat -> don't trigger Qt shortcuts
            if t == QEvent.Type.KeyRelease:
                self._on_key(event, False)
                return True
        return super().eventFilter(obj, event)

    def _on_mouse_move(self, event: QMouseEvent) -> None:
        pos = event.position()
        coords = self.canvas.widget_to_video_coords(int(pos.x()), int(pos.y()))
        if coords is None:
            return
        self.input_event.emit(input_capture.map_mouse_move_abs(coords[0], coords[1]))

    def _on_mouse_button(self, event: QMouseEvent, is_press: bool) -> None:
        ev = input_capture.map_mouse_button(int(event.button().value), is_press)
        if ev is not None:
            self.input_event.emit(ev)

    def _on_wheel(self, event: QWheelEvent) -> None:
        ad = event.angleDelta()
        self.input_event.emit(input_capture.map_mouse_wheel(int(ad.x()), int(ad.y())))

    def _on_key(self, event: QKeyEvent, is_press: bool) -> None:
        ev = input_capture.map_keyevent(
            qt_key=int(event.key()),
            qt_native_scancode=int(event.nativeScanCode()),
            key_text=event.text(),
            is_press=is_press,
        )
        self.input_event.emit(ev)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.disconnect_requested.emit()
        super().closeEvent(event)


class FrameQueue:
    """Thread-safe queue between decoder thread and Qt main thread."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=4)

    def put_nowait(self, item: Any) -> None:
        try:
            self.q.put_nowait(item)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(item)
            except queue.Full:
                pass

    def get(self, timeout: float | None = None):
        return self.q.get(timeout=timeout)
