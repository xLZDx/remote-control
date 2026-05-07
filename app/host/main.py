"""
Host entry point. Starts the TLS server, generates a session PIN, prints it,
and serves until interrupted. Phase-1: connection-only; Phase-2 wires the
encoder/broadcaster, Phase-3 wires the input injector, Phase-4 wires the UI.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.host.server import HostServer, ClientSession
from app.shared import config, crypto

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    )


class HostApp:
    def __init__(self, cfg: config.AppConfig) -> None:
        self.cfg = cfg
        self.pin = cfg.host.static_pin or crypto.generate_pin(config.DEFAULT_PIN_LENGTH)
        self.server = HostServer(
            get_pin=lambda: self.pin,
            port=cfg.host.port,
            bind_address=cfg.host.bind_address,
        )
        self.server.on_client_connect = self._on_connect
        self.server.on_client_disconnect = self._on_disconnect
        self.server.get_monitors = self._monitor_list

    def _monitor_list(self) -> list[dict]:
        # Filled in by Phase 2 (capture module). Stub keeps protocol working.
        return [{"index": 0, "name": "Primary", "width": 0, "height": 0}]

    async def _on_connect(self, session: ClientSession) -> None:
        logger.info("client connected: %s as %s [%s]", session.peer, session.client_name, session.session_id)

    async def _on_disconnect(self, session: ClientSession) -> None:
        logger.info("client disconnected: %s [%s]", session.peer, session.session_id)
        if config.PIN_REGENERATE_ON_DISCONNECT and self.cfg.host.static_pin is None:
            # Only regenerate when no peers remain
            if not self.server.sessions:
                self.pin = crypto.generate_pin(config.DEFAULT_PIN_LENGTH)
                logger.info("PIN regenerated: %s", self.pin)

    async def run(self) -> None:
        crypto.ensure_host_cert()
        await self.server.start()
        logger.info("PIN: %s   port: %d", self.pin, self.cfg.host.port)
        # graceful shutdown
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: stop.set_result(None) if not stop.done() else None)
            except NotImplementedError:
                pass  # Windows
        serve = asyncio.create_task(self.server.serve_forever())
        try:
            await stop
        except KeyboardInterrupt:
            pass
        finally:
            serve.cancel()
            await self.server.stop()


def run() -> int:
    _setup_logging()
    cfg = config.load()
    app = HostApp(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
