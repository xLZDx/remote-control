"""
Client orchestrator: connect dialog -> TLS connect -> viewer window with
decoded video and input forwarding.

Threading: same model as host - asyncio worker thread + Qt main thread,
bridged via Qt signals and run_coroutine_threadsafe.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.client.connect_dialog import ConnectDialog, ConnectInputs, FingerprintConfirmDialog
from app.client.decoder import H264Decoder
from app.client.tcp_client import HostClient, AuthError, FingerprintMismatchError
from app.client.viewer_window import ViewerWindow
from app.shared import config, protocol
from app.shared.config import SavedConnection
from app.shared.logging_setup import humanize_socket_error, init_logging
from app.shared.protocol import Message, MessageType

logger = logging.getLogger(__name__)


class _AsyncioWorker(QObject):
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


class ClientApp(QObject):
    frame_ready = pyqtSignal(bytes, int, int)
    status_text = pyqtSignal(str)
    show_error = pyqtSignal(str)
    auto_disconnect = pyqtSignal()

    def __init__(self, qt: QApplication, cfg: config.AppConfig) -> None:
        super().__init__()
        self.qt = qt
        self.cfg = cfg
        self.client: HostClient | None = None
        self.decoder: H264Decoder | None = None
        self.window: ViewerWindow | None = None

        self.worker = _AsyncioWorker()
        self.worker.start()

        # Wire signals
        self.show_error.connect(self._show_error)
        self.auto_disconnect.connect(self._on_auto_disconnect)

    def run(self) -> int:
        # 1. Connect dialog (loop on retryable failures)
        last_inputs: ConnectInputs | None = None
        while True:
            dlg = ConnectDialog(self.cfg.client)
            if last_inputs is not None:
                dlg.address_edit.setText(last_inputs.address)
                dlg.port_spin.setValue(last_inputs.port)
                dlg.pin_edit.setFocus()
            if dlg.exec() != dlg.DialogCode.Accepted:
                return 0
            inputs = dlg.values()
            last_inputs = inputs
            if not inputs.address or not inputs.pin:
                QMessageBox.warning(None, "RemoteControl", "Address and PIN are required.")
                continue
            ok = self._do_connect(inputs)
            if ok:
                break

        # remember (last + saved)
        self.cfg.client.last_address = inputs.address
        self.cfg.client.last_port = inputs.port
        if inputs.save_name:
            self.cfg.client.upsert_saved(SavedConnection(
                name=inputs.save_name,
                address=inputs.address,
                port=inputs.port,
            ))
        try:
            config.save(self.cfg)
        except Exception:
            logger.exception("config save failed")

        # 2. Viewer window
        if self.client is None or self.client.host_info is None:
            return 1
        info = self.client.host_info
        self.window = ViewerWindow(host_name=info.name, monitors=info.monitors)
        self.window.input_event.connect(self._on_input_event)
        self.window.disconnect_requested.connect(self._on_disconnect_requested)
        self.window.monitor_change_requested.connect(self._on_monitor_change)
        self.frame_ready.connect(self.window.display_frame)
        self.status_text.connect(self.window.set_status)
        self.window.set_status(f"Connected to {info.name}")
        self.window.show()

        rc = self.qt.exec()
        self._teardown()
        return rc

    # ---------- connect flow ----------

    def _do_connect(self, inputs: ConnectInputs) -> bool:
        self.client = HostClient(inputs.address, inputs.port)
        self.decoder = H264Decoder()
        self.client.on_message = self._on_message  # noop until connected, but set early

        async def _confirm(host_key: str, fp: str) -> bool:
            future: asyncio.Future = asyncio.get_event_loop().create_future()

            def _ask() -> None:
                accepted = FingerprintConfirmDialog(host_key, fp).exec()
                future.get_loop().call_soon_threadsafe(
                    future.set_result,
                    accepted == FingerprintConfirmDialog.DialogCode.Accepted,
                )
            QTimer.singleShot(0, _ask)
            return await future

        async def _connect_async() -> Any:
            return await asyncio.wait_for(
                self.client.connect(pin=inputs.pin, confirm_new_fingerprint=_confirm),
                timeout=12.0,
            )

        logger.info("connect attempt: %s:%d", inputs.address, inputs.port)
        try:
            fut = self.worker.run_coro(_connect_async())
            fut.result(timeout=15)
            logger.info("connect success: %s:%d", inputs.address, inputs.port)
            return True
        except asyncio.TimeoutError:
            self._show_error(
                f"Timed out trying to reach {inputs.address}:{inputs.port} after 12 seconds.\n\n"
                "Likely causes:\n"
                "  - host PC is offline or has not started 'Share this PC'\n"
                "  - if connecting from a different network, the host's router is not\n"
                "    forwarding TCP/{port} to the host PC\n"
                "  - Windows Firewall on the host is blocking inbound on the port"
                .replace("{port}", str(inputs.port))
            )
            return False
        except FingerprintMismatchError as exc:
            self._show_error(
                "The host's certificate fingerprint changed.\n\n"
                "This could mean:\n"
                "  - the host re-installed RemoteControl\n"
                "  - someone is impersonating the host\n\n"
                "If you trust the new host, delete the saved fingerprint:\n"
                f"  {config.PINNED_CERTS_PATH}\n"
                f"and try again.\n\nDetails: {exc}"
            )
            return False
        except AuthError as exc:
            msg = str(exc) or "wrong PIN"
            self._show_error(f"Authentication failed: {msg}")
            return False
        except (ConnectionError, OSError) as exc:
            human = humanize_socket_error(exc)
            logger.warning("connect failed: %s (errno=%s)", exc, getattr(exc, "errno", None))
            self._show_error(f"Could not connect to {inputs.address}:{inputs.port}.\n\n{human}")
            return False
        except Exception as exc:
            logger.exception("connect failed (unhandled)")
            self._show_error(f"Connect failed: {exc.__class__.__name__}: {exc}\n\n"
                             f"Details written to %APPDATA%\\RemoteControl\\app.log")
            return False

    # ---------- runtime: messages from host ----------

    async def _on_message(self, msg: Message) -> None:
        if msg.type is MessageType.VIDEO_FRAME:
            self._on_video_frame(msg)
        elif msg.type is MessageType.VIDEO_CONFIG:
            data = msg.as_json()
            self.status_text.emit(
                f"Stream: {data.get('width')}x{data.get('height')} @ {data.get('fps')} fps"
            )
        elif msg.type is MessageType.AUTH_FAIL:
            self.show_error.emit(f"Auth failed: {msg.as_json().get('reason')}")
            self.auto_disconnect.emit()
        elif msg.type is MessageType.ERROR:
            self.show_error.emit(f"Host error: {msg.as_json().get('reason')}")

    def _on_video_frame(self, msg: Message) -> None:
        if self.decoder is None:
            return
        ts, _kf, nal = msg.as_video_frame()
        for df in self.decoder.decode(nal, ts):
            try:
                rgb = bytes(df.rgb.tobytes())
                self.frame_ready.emit(rgb, df.width, df.height)
            except Exception:
                logger.exception("emit frame failed")

    # ---------- runtime: outbound input ----------

    @pyqtSlot(dict)
    def _on_input_event(self, event: dict) -> None:
        if self.client is None or self.client.closed:
            return
        async def _send() -> None:
            await self.client.send(Message.json(MessageType.INPUT_EVENT, event))
        self.worker.run_coro(_send())

    @pyqtSlot(int)
    def _on_monitor_change(self, idx: int) -> None:
        if self.client is None:
            return
        async def _send() -> None:
            await self.client.send(Message.json(MessageType.SET_MONITOR, {"index": int(idx)}))
        self.worker.run_coro(_send())

    @pyqtSlot()
    def _on_disconnect_requested(self) -> None:
        async def _bye() -> None:
            if self.client is not None:
                await self.client.send(protocol.bye())
                await self.client.close()
        try:
            fut = self.worker.run_coro(_bye())
            fut.result(timeout=2)
        except Exception:
            pass
        self.qt.quit()

    @pyqtSlot()
    def _on_auto_disconnect(self) -> None:
        self._on_disconnect_requested()

    @pyqtSlot(str)
    def _show_error(self, text: str) -> None:
        QMessageBox.critical(None, "RemoteControl", text)

    def _teardown(self) -> None:
        try:
            if self.client is not None:
                fut = self.worker.run_coro(self.client.close())
                fut.result(timeout=2)
        except Exception:
            pass
        self.worker.stop()


def run() -> int:
    init_logging()
    qt = QApplication.instance() or QApplication(sys.argv)
    cfg = config.load()
    app = ClientApp(qt, cfg)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(run())
