"""
Hub broker server: TLS listener that accepts laptop registrations and routes
viewer connections through the registered laptops' persistent tunnels.

Two roles per incoming connection (selected by the HELLO message):

  - "register" (a laptop):
      Validate token. If valid, treat the rest of this socket as a
      MuxConnection (Hub = initiator side). Hub will open streams against
      it whenever a viewer asks to connect to this laptop.

  - "connect_via" (a viewer):
      Look up the named laptop in the active registrations. Open a stream
      on its mux. Pipe raw bytes between this socket and the stream until
      either side closes.

The Hub never inspects payload bytes - it's an opaque relay. The viewer's
TLS handshake to the laptop's host runs end-to-end inside the relayed
bytes (this assumes the viewer wraps the relayed connection in another
TLS layer; in v1 we instead trust the Hub - see SECURITY.md).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.hub.registry import HubRegistry
from app.shared import config, crypto
from app.shared.hub_protocol import (
    ack_err, ack_ok, read_hub_message, write_hub_message,
)
from app.shared.mux import MuxConnection, MuxStream

logger = logging.getLogger(__name__)


@dataclass
class ActiveRegistration:
    name: str
    peer: str
    mux: MuxConnection
    connected_iso: str = ""
    streams_opened: int = 0


class HubServer:
    def __init__(
        self,
        registry: HubRegistry | None = None,
        port: int = 7780,
        bind_address: str = "0.0.0.0",
        cert_path: Path | None = None,
        key_path: Path | None = None,
    ) -> None:
        self.registry = registry or HubRegistry()
        self.port = port
        self.bind_address = bind_address
        self._cert_path = cert_path or config.HOST_CERT_PATH
        self._key_path = key_path or config.HOST_KEY_PATH
        self._server: asyncio.base_events.Server | None = None
        self._active: dict[str, ActiveRegistration] = {}
        self._active_lock = asyncio.Lock()

    # ---------- public ----------

    @property
    def active_registrations(self) -> list[ActiveRegistration]:
        return list(self._active.values())

    def is_online(self, name: str) -> bool:
        return self.registry._key(name) in self._active

    async def start(self) -> None:
        # reuse the host's cert (same self-signed identity for the box)
        crypto.ensure_host_cert(self._cert_path, self._key_path)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(certfile=str(self._cert_path), keyfile=str(self._key_path))
        self._server = await asyncio.start_server(
            self._handle, host=self.bind_address, port=self.port, ssl=ssl_ctx,
        )
        logger.info("Hub broker listening on %s:%d", self.bind_address, self.port)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("server not started")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        async with self._active_lock:
            for ar in list(self._active.values()):
                await ar.mux.close()
            self._active.clear()

    # ---------- per-connection ----------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_tuple = writer.get_extra_info("peername") or ("?", 0)
        peer = f"{peer_tuple[0]}:{peer_tuple[1]}"
        try:
            try:
                msg = await read_hub_message(reader, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
                logger.warning("[%s] hub HELLO failed: %s", peer, exc)
                writer.close()
                return

            role = str(msg.get("role", ""))
            if role == "register":
                await self._handle_register(reader, writer, peer, msg)
            elif role == "connect_via":
                await self._handle_connect_via(reader, writer, peer, msg)
            else:
                await write_hub_message(writer, ack_err(f"unknown role: {role!r}"))
                writer.close()
        except Exception:
            logger.exception("[%s] hub handler crashed", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_register(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
        msg: dict,
    ) -> None:
        name = str(msg.get("name", "")).strip()
        token = str(msg.get("token", ""))
        if not name or not token:
            await write_hub_message(writer, ack_err("name and token required"))
            return
        if not self.registry.verify(name, token):
            await write_hub_message(writer, ack_err("bad token"))
            logger.warning("[%s] register rejected for %s: bad token", peer, name)
            return

        # Already-online dedupe: drop the older connection and accept the new one.
        async with self._active_lock:
            existing = self._active.pop(self.registry._key(name), None)
        if existing is not None:
            try:
                await existing.mux.close()
            except Exception:
                pass

        await write_hub_message(writer, ack_ok(name=name))
        # Hub becomes the mux initiator (it opens streams when viewers come in)
        mux = MuxConnection(reader, writer, is_initiator=True, keepalive_s=15.0)
        ar = ActiveRegistration(
            name=name, peer=peer, mux=mux,
            connected_iso=_iso_now(),
        )
        async with self._active_lock:
            self._active[self.registry._key(name)] = ar
        self.registry.mark_seen(name)
        logger.info("[%s] registered as %s", peer, name)

        try:
            await mux.run()
        finally:
            async with self._active_lock:
                if self._active.get(self.registry._key(name)) is ar:
                    self._active.pop(self.registry._key(name), None)
            logger.info("[%s] registration %s ended (streams_opened=%d)",
                        peer, name, ar.streams_opened)

    async def _handle_connect_via(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
        msg: dict,
    ) -> None:
        name = str(msg.get("name", "")).strip()
        if not name:
            await write_hub_message(writer, ack_err("name required"))
            return
        async with self._active_lock:
            ar = self._active.get(self.registry._key(name))
        if ar is None:
            # Either unknown name or laptop offline. Don't leak which.
            await write_hub_message(writer, ack_err(f"laptop '{name}' is not available"))
            logger.info("[%s] connect_via %s: not online", peer, name)
            return

        try:
            stream = await ar.mux.open_stream({"target": "host"})
        except (ConnectionError, OSError) as exc:
            await write_hub_message(writer, ack_err(f"laptop '{name}' tunnel dropped: {exc}"))
            return

        ar.streams_opened += 1
        await write_hub_message(writer, ack_ok(name=name))
        logger.info("[%s] connect_via %s: stream-id=%d", peer, name, stream.stream_id)

        # Now bidirectionally pipe raw bytes.
        await self._pipe_bidirectional(reader, writer, stream, peer=peer)

    async def _pipe_bidirectional(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        stream: MuxStream,
        peer: str = "?",
    ) -> None:
        async def _client_to_stream() -> None:
            try:
                while True:
                    chunk = await client_reader.read(64 * 1024)
                    if not chunk:
                        break
                    if not await stream.write(chunk):
                        break
            except (ConnectionError, OSError):
                pass
            finally:
                stream.close()

        async def _stream_to_client() -> None:
            try:
                while True:
                    try:
                        chunk = await stream.reader.read(64 * 1024)
                    except (ConnectionError, OSError):
                        break
                    if not chunk:
                        break
                    try:
                        client_writer.write(chunk)
                        await client_writer.drain()
                    except (ConnectionError, OSError):
                        break
            finally:
                try:
                    client_writer.close()
                except Exception:
                    pass

        await asyncio.gather(
            _client_to_stream(), _stream_to_client(),
            return_exceptions=True,
        )


def _iso_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
