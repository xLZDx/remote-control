"""
Host orchestrator: wires server + capture + encoder + broadcaster + UI.

Threading:
- Qt main thread runs the system tray icon and PIN window.
- A worker thread runs the asyncio event loop with the server, capture
  pump, and broadcaster.
- Cross-thread bridges are Qt signals (worker -> Qt) and
  asyncio.run_coroutine_threadsafe (Qt -> worker).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.host.capture import ScreenCapture
from app.host.encoder import H264Encoder
from app.host.frame_broadcaster import FrameBroadcaster
from app.host.input_injector import InputInjector
from app.host.server import HostServer, ClientSession
from app.host.tray import HostTray
from app.host.ui_pin import HostPinWindow
from app.shared import config, crypto, protocol
from app.shared.protocol import Message, MessageType

logger = logging.getLogger(__name__)


class _AsyncioWorker(QObject):
    """Owns the asyncio loop in a background thread."""
    sessions_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rc-asyncio", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def stop(self) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def run_coro(self, coro):
        if self.loop is None:
            raise RuntimeError("asyncio loop not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class HostApp(QObject):
    pin_changed = pyqtSignal(str)
    sessions_changed = pyqtSignal()

    def __init__(self, app: QApplication, cfg: config.AppConfig) -> None:
        super().__init__()
        self.qt_app = app
        self.cfg = cfg

        self._pin = cfg.host.static_pin or crypto.generate_pin(config.DEFAULT_PIN_LENGTH)
        self._fingerprint = ""

        self.injector = InputInjector()
        self.capture: ScreenCapture | None = None
        self.encoder: H264Encoder | None = None
        self.broadcaster: FrameBroadcaster | None = None
        self.server: HostServer | None = None
        self._capture_task: asyncio.Task | None = None

        self.worker = _AsyncioWorker()
        self.worker.start()

        # UI
        self.tray = HostTray(get_status_text=self._status_text)
        self.tray.show_window_requested.connect(self._show_window)
        self.tray.regenerate_pin_requested.connect(self.regenerate_pin)
        self.tray.quit_requested.connect(self.quit)
        self.tray.show()

        self.window = HostPinWindow(
            get_pin=lambda: self._pin,
            get_port=lambda: self.cfg.host.port,
            get_fingerprint=lambda: self._fingerprint,
            get_sessions=lambda: list(self.server.sessions) if self.server else [],
        )
        self.window.regenerate_pin_requested.connect(self.regenerate_pin)
        self.window.toggle_view_only_requested.connect(self._set_view_only)
        self.window.kick_session_requested.connect(self._kick_session)
        self.window.quit_requested.connect(self.quit)

        # Tray tooltip refresh
        self._tray_tip_timer = QTimer(self)
        self._tray_tip_timer.timeout.connect(self.tray.update_tooltip)
        self._tray_tip_timer.start(2000)

        # Boot async parts
        fut = self.worker.run_coro(self._async_start())
        try:
            fut.result(timeout=15)
        except Exception as exc:
            logger.exception("startup failed")
            QMessageBox.critical(None, "RemoteControl", f"Startup failed: {exc}")
            self.qt_app.quit()
            return

        self.window.show()

    # ---------- async setup ----------

    async def _async_start(self) -> None:
        # cert
        cert_path, _ = crypto.ensure_host_cert()
        try:
            self._fingerprint = crypto.cert_fingerprint(cert_path.read_bytes())
        except Exception:
            self._fingerprint = ""

        # capture
        self.capture = ScreenCapture(
            target_fps=self.cfg.host.fps,
            monitor_index=0,
        )
        await self.capture.start()
        mon = self.capture.monitors[0]
        # encoder for primary monitor
        self.encoder = H264Encoder(
            width=max(mon.width, 1280),
            height=max(mon.height, 720),
            fps=self.cfg.host.fps,
            bitrate_kbps=self.cfg.host.bitrate_kbps,
        )
        self.broadcaster = FrameBroadcaster(self.encoder)

        # server
        self.server = HostServer(
            get_pin=lambda: self._pin,
            port=self.cfg.host.port,
            bind_address=self.cfg.host.bind_address,
        )
        self.server.get_monitors = lambda: self.capture.monitors_as_dicts() if self.capture else []
        self.server.on_client_connect = self._on_client_connect
        self.server.on_client_disconnect = self._on_client_disconnect
        self.server.on_input_event = self._on_input_event

        await self.server.start()
        asyncio.create_task(self.server.serve_forever())

        # capture pump
        self._capture_task = asyncio.create_task(self._pump_capture())

    async def _pump_capture(self) -> None:
        if self.capture is None or self.broadcaster is None:
            return
        async for frame in self.capture.frames():
            if frame is None:
                break
            try:
                await self.broadcaster.feed_frame(frame.data, frame.timestamp_us)
            except Exception:
                logger.exception("broadcaster.feed_frame raised")

    # ---------- server callbacks (run in asyncio thread) ----------

    async def _on_client_connect(self, session: ClientSession) -> None:
        logger.info("connected: %s", session.peer)
        if self.broadcaster is None or self.encoder is None:
            return

        async def _send(packet) -> bool:
            msg = Message.video_frame(
                timestamp_us=packet.timestamp_us,
                keyframe=packet.keyframe,
                nal=packet.data,
            )
            return await session.send(msg)

        # Send video config first
        await session.send(protocol.video_config(self.encoder.width, self.encoder.height,
                                                self.encoder.fps, "h264"))
        await self.broadcaster.add_subscriber(session.session_id, _send)
        self.sessions_changed.emit()

    async def _on_client_disconnect(self, session: ClientSession) -> None:
        if self.broadcaster is not None:
            await self.broadcaster.remove_subscriber(session.session_id)
        self.sessions_changed.emit()
        # regenerate PIN if no sessions remain
        if config.PIN_REGENERATE_ON_DISCONNECT and self.cfg.host.static_pin is None:
            if self.server is not None and not self.server.sessions:
                self._pin = crypto.generate_pin(config.DEFAULT_PIN_LENGTH)
                self.pin_changed.emit(self._pin)

    async def _on_input_event(self, session: ClientSession, event: dict) -> None:
        if session.view_only:
            return
        try:
            self.injector.dispatch(event)
        except Exception:
            logger.exception("inject raised for event %s", event)

    # ---------- UI actions (run in Qt thread) ----------

    def regenerate_pin(self) -> None:
        self._pin = crypto.generate_pin(config.DEFAULT_PIN_LENGTH)
        self.pin_changed.emit(self._pin)
        self.window.refresh()

    def _set_view_only(self, sid: str, view_only: bool) -> None:
        async def _do() -> None:
            if self.server is None:
                return
            for s in self.server.sessions:
                if s.session_id == sid:
                    s.view_only = view_only
                    return
        self.worker.run_coro(_do())

    def _kick_session(self, sid: str) -> None:
        async def _do() -> None:
            if self.server is None:
                return
            for s in self.server.sessions:
                if s.session_id == sid:
                    s.closed.set()
                    if s.writer is not None:
                        try:
                            s.writer.close()
                        except Exception:
                            pass
                    return
        self.worker.run_coro(_do())

    def _show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _status_text(self) -> str:
        if self.server is None:
            return "RemoteControl - starting..."
        n = len(self.server.sessions)
        return f"RemoteControl - PIN {self._pin} - {n} viewer{'' if n == 1 else 's'}"

    def quit(self) -> None:
        async def _shutdown() -> None:
            if self._capture_task is not None:
                self._capture_task.cancel()
            if self.capture is not None:
                await self.capture.stop()
            if self.server is not None:
                await self.server.stop()
            if self.encoder is not None:
                self.encoder.close()
        try:
            fut = self.worker.run_coro(_shutdown())
            fut.result(timeout=5)
        except Exception:
            pass
        self.worker.stop()
        self.qt_app.quit()


def run() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s")
    qt = QApplication.instance() or QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)   # tray keeps app alive
    cfg = config.load()
    app = HostApp(qt, cfg)  # noqa: F841 - keep alive
    return qt.exec()


if __name__ == "__main__":
    raise SystemExit(run())
