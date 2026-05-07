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
from app.host.hub_settings import HubSettingsCallbacks, HubSettingsDialog
from app.host.input_injector import InputInjector
from app.host.registration import (
    HubRegistrar, HubRegistrationConfig, clear_registration,
    load_registration, save_registration,
)
from app.host.server import HostServer, ClientSession
from app.host.tray import HostTray
from app.host.ui_pin import HostPinWindow
from app.hub.registry import HubRegistry
from app.hub.server import HubServer
from app.shared import config, crypto, protocol
from app.shared.logging_setup import init_logging
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

        # Hub broker (only running on the dedicated-IP PC)
        self.hub_registry = HubRegistry()
        self.hub_server: HubServer | None = None
        self._hub_serve_task: asyncio.Task | None = None

        # Laptop registrar (only running on PCs that registered with a Hub)
        self.registrar: HubRegistrar | None = None
        self._loopback_started = False

        self.worker = _AsyncioWorker()
        self.worker.start()

        # UI
        self.tray = HostTray(get_status_text=self._status_text)
        self.tray.show_window_requested.connect(self._show_window)
        self.tray.regenerate_pin_requested.connect(self.regenerate_pin)
        self.tray.quit_requested.connect(self.quit)
        self.tray.hub_settings_requested.connect(self._show_hub_settings)
        self.tray.open_log_requested.connect(self._open_log_folder)
        self.tray.show()
        self._hub_dialog: HubSettingsDialog | None = None

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
            fut.result(timeout=20)
        except Exception as exc:
            logger.exception("startup failed")
            log_path = config.appdata_dir() / "app.log"
            QMessageBox.critical(
                None, "RemoteControl - startup failed",
                f"{exc.__class__.__name__}: {exc}\n\n"
                f"Common causes:\n"
                f"  - port {self.cfg.host.port} already in use (close other RemoteControl, "
                f"    change port in config.json)\n"
                f"  - dxcam couldn't initialize (multi-GPU laptop? try right-clicking the "
                f"    RemoteControl shortcut -> Run with graphics processor -> Integrated)\n"
                f"  - PyAV/FFmpeg DLLs not loaded (rebuild from source)\n\n"
                f"Full traceback in:\n  {log_path}",
            )
            self.qt_app.quit()
            return

        self.window.show()

    # ---------- async setup ----------

    async def _async_start(self) -> None:
        logger.info("host startup: cert + capture + encoder + server")

        # cert
        try:
            cert_path, _ = crypto.ensure_host_cert()
            self._fingerprint = crypto.cert_fingerprint(cert_path.read_bytes())
            logger.info("cert fingerprint: %s", self._fingerprint)
        except Exception as exc:
            logger.exception("cert generation failed")
            raise RuntimeError(f"could not generate TLS certificate: {exc}") from exc

        # capture
        try:
            self.capture = ScreenCapture(
                target_fps=self.cfg.host.fps,
                monitor_index=0,
            )
            await self.capture.start()
        except Exception as exc:
            logger.exception("capture init failed")
            raise RuntimeError(
                f"screen capture failed to initialize: {exc}\n\n"
                f"This usually means dxcam can't access the display adapter. "
                f"On laptops with switchable graphics (NVIDIA Optimus / AMD Switchable), "
                f"try Settings -> System -> Display -> Graphics, find RemoteControl, "
                f"and set it to use the integrated GPU."
            ) from exc

        mons = self.capture.monitors
        if not mons:
            raise RuntimeError("no monitors detected by dxcam")
        mon = mons[0]
        if mon.width <= 0 or mon.height <= 0:
            logger.warning("monitor reports 0x0 - using 1920x1080 fallback")
            cap_w, cap_h = 1920, 1080
        else:
            cap_w, cap_h = mon.width, mon.height
        # H.264 requires even dimensions
        cap_w -= cap_w % 2
        cap_h -= cap_h % 2
        logger.info("capture monitor: %s %dx%d", mon.name, cap_w, cap_h)

        try:
            self.encoder = H264Encoder(
                width=cap_w,
                height=cap_h,
                fps=self.cfg.host.fps,
                bitrate_kbps=self.cfg.host.bitrate_kbps,
            )
        except Exception as exc:
            logger.exception("encoder init failed")
            raise RuntimeError(
                f"H.264 encoder failed to initialize: {exc}\n\n"
                f"This usually means FFmpeg DLLs (libx264) aren't loadable. "
                f"Try reinstalling RemoteControl."
            ) from exc
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

        try:
            await self.server.start()
        except OSError as exc:
            logger.exception("server bind failed")
            raise RuntimeError(
                f"could not listen on port {self.cfg.host.port}: {exc}\n\n"
                f"Likely the port is already in use by another process."
            ) from exc
        asyncio.create_task(self.server.serve_forever())
        logger.info("server listening on %s:%d", self.cfg.host.bind_address, self.cfg.host.port)

        # capture pump
        self._capture_task = asyncio.create_task(self._pump_capture())
        logger.info("host startup complete")

        # Hub broker (if configured to run on this PC)
        if self.cfg.host.hub_enabled:
            await self._start_hub_server_async(self.cfg.host.hub_port)

        # Registrar (if a Hub registration is saved on this PC)
        existing_reg = load_registration()
        if existing_reg is not None and existing_reg.token and existing_reg.hub_address:
            await self._start_registrar_async(existing_reg)

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

    def _open_log_folder(self) -> None:
        import os
        import subprocess
        path = str(config.appdata_dir())
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            try:
                subprocess.Popen(["explorer", path])
            except Exception:
                logger.exception("could not open log folder")

    def _status_text(self) -> str:
        if self.server is None:
            return "RemoteControl - starting..."
        n = len(self.server.sessions)
        text = f"RemoteControl - PIN {self._pin} - {n} viewer{'' if n == 1 else 's'}"
        if self.hub_server is not None:
            text += f"  |  Hub: {len(self.hub_server.active_registrations)} laptops online"
        if self.registrar is not None:
            text += f"  |  Hub registrar: {self.registrar.status}"
        return text

    # ---------- Hub admin (Qt-thread entry points) ----------

    def _show_hub_settings(self) -> None:
        if self._hub_dialog is not None and self._hub_dialog.isVisible():
            self._hub_dialog.raise_()
            self._hub_dialog.activateWindow()
            return
        cb = HubSettingsCallbacks(
            list_registrations=self._hub_list_registrations,
            list_active=self._hub_list_active,
            add_registration=self._hub_add_registration,
            remove_registration=self._hub_remove_registration,
            set_hub_enabled=self._hub_set_enabled,
            is_hub_running=lambda: self.hub_server is not None,
            get_registration=lambda: load_registration(),
            save_registration=self._hub_save_registration,
            clear_registration=self._hub_clear_registration,
            registrar_status=self._hub_registrar_status,
        )
        self._hub_dialog = HubSettingsDialog(cb, self.cfg.host)
        self._hub_dialog.show()

    def _hub_list_registrations(self) -> list:
        return [self.hub_registry.get(n) for n in self.hub_registry.names() if self.hub_registry.get(n)]

    def _hub_list_active(self) -> list[str]:
        if self.hub_server is None:
            return []
        return [ar.name for ar in self.hub_server.active_registrations]

    def _hub_add_registration(self, name: str):
        return self.hub_registry.add(name)

    def _hub_remove_registration(self, name: str) -> bool:
        return self.hub_registry.remove(name)

    def _hub_set_enabled(self, enabled: bool, port: int) -> None:
        self.cfg.host.hub_enabled = bool(enabled)
        self.cfg.host.hub_port = int(port)
        try:
            config.save(self.cfg)
        except Exception:
            logger.exception("config save failed")
        if enabled:
            fut = self.worker.run_coro(self._start_hub_server_async(port))
            try:
                fut.result(timeout=10)
            except Exception as exc:
                QMessageBox.warning(None, "Hub", f"Could not start Hub: {exc}")
        else:
            fut = self.worker.run_coro(self._stop_hub_server_async())
            try:
                fut.result(timeout=5)
            except Exception:
                pass

    def _hub_save_registration(self, hub_addr: str, hub_port: int, name: str, token: str) -> None:
        existing = load_registration()
        new_reg = HubRegistrationConfig(
            hub_address=hub_addr,
            hub_port=hub_port,
            laptop_name=name,
            token=token,
            pinned_fingerprint=(existing.pinned_fingerprint if existing else ""),
        )
        # If hub_address or token changed, drop the pinned fingerprint
        if existing is None or existing.hub_address != hub_addr or existing.hub_port != hub_port:
            new_reg.pinned_fingerprint = ""
        save_registration(new_reg)
        # restart registrar
        fut = self.worker.run_coro(self._start_registrar_async(new_reg, restart=True))
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    def _hub_clear_registration(self) -> None:
        clear_registration()
        fut = self.worker.run_coro(self._stop_registrar_async())
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    def _hub_registrar_status(self) -> tuple[str, str]:
        if self.registrar is None:
            return ("idle", "")
        return (self.registrar.status, self.registrar.last_error)

    # ---------- Hub orchestration (asyncio thread) ----------

    async def _start_hub_server_async(self, port: int) -> None:
        await self._stop_hub_server_async()
        hub = HubServer(
            registry=self.hub_registry,
            port=port,
            bind_address=self.cfg.host.hub_bind_address,
        )
        await hub.start()
        self.hub_server = hub
        self._hub_serve_task = asyncio.create_task(hub.serve_forever())
        logger.info("Hub broker started on port %d", port)

    async def _stop_hub_server_async(self) -> None:
        if self._hub_serve_task is not None:
            self._hub_serve_task.cancel()
            try:
                await self._hub_serve_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hub_serve_task = None
        if self.hub_server is not None:
            await self.hub_server.stop()
            self.hub_server = None
            logger.info("Hub broker stopped")

    async def _start_registrar_async(
        self, reg: HubRegistrationConfig, restart: bool = False,
    ) -> None:
        if restart and self.registrar is not None:
            await self.registrar.stop()
            self.registrar = None
        # Make sure the loopback listener is up so Hub-bridged streams have somewhere to land.
        if self.server is not None and not self._loopback_started:
            try:
                await self.server.start_loopback(self.cfg.host.loopback_port)
                self._loopback_started = True
            except OSError as exc:
                logger.warning("could not start loopback listener on %d: %s",
                               self.cfg.host.loopback_port, exc)
        target_port = self.cfg.host.loopback_port if self._loopback_started else self.cfg.host.port
        self.registrar = HubRegistrar(
            cfg=reg, local_host_port=target_port,
            on_status_change=lambda s, e: logger.info("registrar: %s%s", s, f" ({e})" if e else ""),
        )
        self.registrar.start()
        logger.info("registrar started for %s (target laptop name: %s)",
                    f"{reg.hub_address}:{reg.hub_port}", reg.laptop_name)

    async def _stop_registrar_async(self) -> None:
        if self.registrar is not None:
            await self.registrar.stop()
            self.registrar = None
            logger.info("registrar stopped")

    def quit(self) -> None:
        async def _shutdown() -> None:
            if self._capture_task is not None:
                self._capture_task.cancel()
            if self.capture is not None:
                await self.capture.stop()
            if self.registrar is not None:
                await self.registrar.stop()
            await self._stop_hub_server_async()
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
    log_path = init_logging()
    logger.info("=== RemoteControl host starting ===")
    logger.info("log: %s", log_path)
    qt = QApplication.instance() or QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)   # tray keeps app alive
    cfg = config.load()
    app = HostApp(qt, cfg)  # noqa: F841 - keep alive
    return qt.exec()


if __name__ == "__main__":
    raise SystemExit(run())
