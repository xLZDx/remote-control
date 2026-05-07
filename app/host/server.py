"""
TLS server for the host. Accepts multiple concurrent clients, each going
through a HELLO -> AUTH_CHALLENGE -> AUTH_RESPONSE -> AUTH_OK handshake,
then receiving video and sending input events on the same connection.

The server is transport-only: the host orchestrator wires it to the capture
pipeline and the input injector via callbacks/methods.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.shared import config, crypto, protocol
from app.shared.protocol import Message, MessageType

logger = logging.getLogger(__name__)


@dataclass
class ClientSession:
    session_id: str
    peer: str                          # "ip:port" of remote
    client_name: str
    view_only: bool = False
    writer: asyncio.StreamWriter | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, msg: Message) -> bool:
        """Send one message. Returns False on broken pipe."""
        if self.writer is None or self.closed.is_set():
            return False
        async with self.send_lock:
            try:
                await protocol.write_message(self.writer, msg)
                return True
            except (ConnectionError, OSError, asyncio.TimeoutError):
                self.closed.set()
                return False


# Callback signatures
InputHandler = Callable[[ClientSession, dict], Awaitable[None]]
ConnectHandler = Callable[[ClientSession], Awaitable[None]]
DisconnectHandler = Callable[[ClientSession], Awaitable[None]]
ClipboardHandler = Callable[[ClientSession, str], Awaitable[None]]


class HostServer:
    def __init__(
        self,
        get_pin: Callable[[], str],
        host_name: str | None = None,
        port: int = config.DEFAULT_PORT,
        bind_address: str = "0.0.0.0",
    ) -> None:
        self.get_pin = get_pin
        self.host_name = host_name or socket.gethostname() or "host"
        self.port = port
        self.bind_address = bind_address

        self.on_client_connect: ConnectHandler | None = None
        self.on_client_disconnect: DisconnectHandler | None = None
        self.on_input_event: InputHandler | None = None
        self.on_clipboard: ClipboardHandler | None = None
        self.get_monitors: Callable[[], list[dict]] | None = None

        self._sessions: dict[str, ClientSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._server: asyncio.base_events.Server | None = None
        self._loopback_server: asyncio.base_events.Server | None = None
        self._ssl_ctx: ssl.SSLContext | None = None

    # ---------- public ----------

    @property
    def sessions(self) -> list[ClientSession]:
        return list(self._sessions.values())

    async def broadcast(self, msg: Message) -> int:
        """Send `msg` to all connected, authenticated clients. Returns delivery count."""
        sent = 0
        for session in list(self._sessions.values()):
            if await session.send(msg):
                sent += 1
        return sent

    async def start(self) -> None:
        cert_path, key_path = crypto.ensure_host_cert()
        self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_address,
            port=self.port,
            ssl=self._ssl_ctx,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("HostServer listening on %s", addrs)

    async def start_loopback(self, loopback_port: int) -> int:
        """
        Start a PLAINTEXT TCP listener on 127.0.0.1 only. Used by the Hub
        registration bridge so Hub-relayed clients can speak the host
        protocol without an inner TLS wrap (v1 trade-off; the Hub is in
        the trust path - see SECURITY.md).

        Returns the bound port (useful when caller passed 0 for auto).
        """
        srv = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=loopback_port,
        )
        self._loopback_server = srv
        addrs = ", ".join(str(s.getsockname()) for s in srv.sockets or [])
        logger.info("HostServer loopback listener on %s", addrs)
        return srv.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        if not self._server:
            raise RuntimeError("server not started")
        tasks = [asyncio.create_task(self._server.serve_forever())]
        if self._loopback_server is not None:
            tasks.append(asyncio.create_task(self._loopback_server.serve_forever()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def stop(self) -> None:
        for srv_attr in ("_server", "_loopback_server"):
            srv = getattr(self, srv_attr)
            if srv is not None:
                srv.close()
                try:
                    await srv.wait_closed()
                except Exception:
                    pass
                setattr(self, srv_attr, None)
        # close sessions
        for s in list(self._sessions.values()):
            await self._close_session(s)

    # ---------- per-client ----------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_tuple = writer.get_extra_info("peername") or ("?", 0)
        peer = f"{peer_tuple[0]}:{peer_tuple[1]}"
        session = ClientSession(
            session_id=uuid.uuid4().hex[:12],
            peer=peer,
            client_name="?",
            writer=writer,
        )
        try:
            await asyncio.wait_for(self._do_handshake(session, reader), config.HANDSHAKE_TIMEOUT_S)
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError) as exc:
            logger.warning("[%s] handshake failed: %s", peer, exc)
            await self._close_session(session)
            return
        except Exception:
            logger.exception("[%s] handshake error", peer)
            await self._close_session(session)
            return

        async with self._sessions_lock:
            self._sessions[session.session_id] = session

        if self.on_client_connect:
            try:
                await self.on_client_connect(session)
            except Exception:
                logger.exception("on_client_connect raised")

        try:
            await self._run_session_loop(session, reader)
        finally:
            async with self._sessions_lock:
                self._sessions.pop(session.session_id, None)
            if self.on_client_disconnect:
                try:
                    await self.on_client_disconnect(session)
                except Exception:
                    logger.exception("on_client_disconnect raised")
            await self._close_session(session)

    async def _do_handshake(
        self, session: ClientSession, reader: asyncio.StreamReader
    ) -> None:
        # 1) HELLO from client
        msg = await protocol.read_message(reader)
        if msg.type is not MessageType.HELLO:
            raise ValueError(f"expected HELLO, got {msg.type.name}")
        hello = msg.as_json()
        if hello.get("version") != config.PROTOCOL_VERSION:
            await session.send(protocol.error(f"protocol version mismatch (need {config.PROTOCOL_VERSION})"))
            raise ValueError("protocol version mismatch")
        session.client_name = str(hello.get("client_name") or "client")[:64]

        # 2) HELLO_ACK with monitor info
        monitors = self.get_monitors() if self.get_monitors else []
        await session.send(protocol.hello_ack(self.host_name, monitors))

        # 3) AUTH_CHALLENGE
        salt, nonce = crypto.make_challenge()
        await session.send(protocol.auth_challenge(salt.hex(), nonce.hex()))

        # 4) AUTH_RESPONSE
        msg = await protocol.read_message(reader)
        if msg.type is not MessageType.AUTH_RESPONSE:
            raise ValueError(f"expected AUTH_RESPONSE, got {msg.type.name}")
        proof_hex = msg.as_json().get("proof", "")
        try:
            proof = bytes.fromhex(proof_hex)
        except ValueError as exc:
            raise ValueError("malformed proof") from exc

        pin = self.get_pin()
        if not crypto.verify_proof(pin, salt, nonce, proof):
            await session.send(protocol.auth_fail("bad PIN"))
            raise ValueError(f"auth failed for {session.peer}")

        # 5) AUTH_OK
        await session.send(protocol.auth_ok(view_only=session.view_only))
        logger.info("[%s] authenticated as %s (session=%s)", session.peer, session.client_name, session.session_id)

    async def _run_session_loop(
        self, session: ClientSession, reader: asyncio.StreamReader
    ) -> None:
        while not session.closed.is_set():
            try:
                msg = await asyncio.wait_for(protocol.read_message(reader), config.IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                if not await session.send(protocol.ping()):
                    return
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return

            await self._dispatch(session, msg)

    async def _dispatch(self, session: ClientSession, msg: Message) -> None:
        t = msg.type
        if t is MessageType.PING:
            await session.send(protocol.pong())
        elif t is MessageType.PONG:
            pass
        elif t is MessageType.BYE:
            session.closed.set()
        elif t is MessageType.INPUT_EVENT:
            if session.view_only or self.on_input_event is None:
                return
            try:
                await self.on_input_event(session, msg.as_json())
            except Exception:
                logger.exception("on_input_event raised")
        elif t is MessageType.CLIPBOARD:
            if self.on_clipboard is None:
                return
            text = str(msg.as_json().get("text", ""))
            try:
                await self.on_clipboard(session, text)
            except Exception:
                logger.exception("on_clipboard raised")
        elif t is MessageType.SET_VIEW_ONLY:
            session.view_only = bool(msg.as_json().get("view_only", False))
        else:
            logger.debug("[%s] ignoring %s", session.peer, t.name)

    async def _close_session(self, session: ClientSession) -> None:
        session.closed.set()
        if session.writer is not None:
            try:
                session.writer.close()
                await session.writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            session.writer = None
