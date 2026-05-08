"""
TLS client. Connects to a host, negotiates the handshake, then exposes a
stream-of-messages interface to the rest of the client app.

Cert pinning: on first connect to a host the user is asked to confirm the
server fingerprint (shown briefly to the user). On subsequent connects the
fingerprint must match the pinned value or the client refuses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.shared import config, crypto, protocol
from app.shared.hub_protocol import (
    hello_connect_via, read_hub_message, write_hub_message,
)
from app.shared.protocol import Message, MessageType

logger = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class FingerprintMismatchError(Exception):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"fingerprint mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


@dataclass
class HostInfo:
    name: str
    monitors: list[dict]
    fingerprint: str


# ---- pinning store ----

def load_pins(path: Path = config.PINNED_CERTS_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_pins(pins: dict[str, str], path: Path = config.PINNED_CERTS_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---- client ----

@dataclass
class ViaHub:
    """Parameters for connecting through the Hub broker."""
    hub_address: str
    hub_port: int
    laptop_name: str


class HostClient:
    def __init__(
        self,
        address: str,
        port: int = config.DEFAULT_PORT,
        client_name: str | None = None,
        via_hub: ViaHub | None = None,
    ) -> None:
        self.address = address
        self.port = port
        self.client_name = client_name or socket.gethostname() or "client"
        self.via_hub = via_hub

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.host_info: HostInfo | None = None

        self.on_message: Callable[[Message], Awaitable[None]] | None = None
        self._receive_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def send(self, msg: Message) -> bool:
        if self.writer is None or self._closed.is_set():
            return False
        async with self._send_lock:
            try:
                await protocol.write_message(self.writer, msg)
                return True
            except (ConnectionError, OSError):
                self._closed.set()
                return False

    async def connect(
        self,
        pin: str,
        confirm_new_fingerprint: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> HostInfo:
        """
        Connect, validate fingerprint, run handshake. Returns HostInfo on success.
        `confirm_new_fingerprint(host_key, fingerprint)` is awaited only when the
        target has no existing pin; if it returns False, the connection is aborted.

        In direct mode, the pinned cert is the host's. In via-Hub mode it's the
        Hub's (the laptop's host cert is not seen end-to-end in v1; see
        SECURITY.md).
        """
        if self.via_hub is not None:
            logger.info("connect: opening via Hub %s:%d -> %s",
                        self.via_hub.hub_address, self.via_hub.hub_port, self.via_hub.laptop_name)
            await self._open_via_hub(confirm_new_fingerprint)
        else:
            logger.info("connect: opening direct TCP/TLS to %s:%d", self.address, self.port)
            await self._open_direct(confirm_new_fingerprint)
        logger.info("connect: TCP/TLS+fingerprint OK; sending HELLO")

        # ---- application handshake (same code path for both transports) ----
        await self.send(protocol.hello(self.client_name))
        logger.info("connect: HELLO sent; waiting for HELLO_ACK")

        msg = await asyncio.wait_for(protocol.read_message(self.reader), config.HANDSHAKE_TIMEOUT_S)
        if msg.type is not MessageType.HELLO_ACK:
            await self.close()
            raise AuthError(f"expected HELLO_ACK, got {msg.type.name}")
        hello_ack = msg.as_json()
        logger.info("connect: got HELLO_ACK from host '%s'; waiting for AUTH_CHALLENGE",
                    hello_ack.get("host_name", "?"))

        msg = await asyncio.wait_for(protocol.read_message(self.reader), config.HANDSHAKE_TIMEOUT_S)
        if msg.type is not MessageType.AUTH_CHALLENGE:
            await self.close()
            raise AuthError(f"expected AUTH_CHALLENGE, got {msg.type.name}")
        chal = msg.as_json()
        salt = bytes.fromhex(chal["salt"])
        nonce = bytes.fromhex(chal["nonce"])
        proof = crypto.compute_proof(pin, salt, nonce)
        await self.send(protocol.auth_response(proof.hex()))
        logger.info("connect: AUTH_RESPONSE sent; waiting for AUTH_OK")

        msg = await asyncio.wait_for(protocol.read_message(self.reader), config.HANDSHAKE_TIMEOUT_S)
        if msg.type is MessageType.AUTH_FAIL:
            reason = msg.as_json().get("reason", "auth failed")
            await self.close()
            raise AuthError(reason)
        if msg.type is not MessageType.AUTH_OK:
            await self.close()
            raise AuthError(f"expected AUTH_OK, got {msg.type.name}")

        self.host_info = HostInfo(
            name=str(hello_ack.get("host_name") or "host"),
            monitors=list(hello_ack.get("monitors") or []),
            fingerprint=getattr(self, "_opened_fingerprint", ""),
        )
        self._receive_task = asyncio.create_task(self._recv_loop())
        target_label = (
            f"hub:{self.via_hub.hub_address}:{self.via_hub.hub_port} -> {self.via_hub.laptop_name}"
            if self.via_hub is not None
            else f"{self.address}:{self.port}"
        )
        logger.info("connected to %s (%s)", target_label, self.host_info.name)
        return self.host_info

    async def _recv_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self._closed.is_set():
                try:
                    msg = await protocol.read_message(self.reader)
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                if msg.type is MessageType.PING:
                    await self.send(protocol.pong())
                    continue
                if msg.type is MessageType.PONG:
                    continue
                if msg.type is MessageType.BYE:
                    break
                if self.on_message is not None:
                    try:
                        await self.on_message(msg)
                    except Exception:
                        logger.exception("on_message raised")
        finally:
            self._closed.set()

    async def close(self) -> None:
        self._closed.set()
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.writer = None

    # ---------- transport openers ----------

    async def _open_direct(
        self,
        confirm_new_fingerprint: Callable[[str, str], Awaitable[bool]] | None,
    ) -> None:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.address, self.port, ssl=ssl_ctx),
                timeout=config.TCP_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"TCP/TLS connect to {self.address}:{self.port} timed out after "
                f"{config.TCP_CONNECT_TIMEOUT_S:.0f}s"
            ) from exc
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise ConnectionError(f"connect to {self.address}:{self.port} failed: {exc}") from exc
        logger.info("connect: TCP+TLS established to %s:%d; verifying cert", self.address, self.port)
        host_key = f"{self.address}:{self.port}"
        await self._verify_fingerprint(host_key, confirm_new_fingerprint)

    async def _open_via_hub(
        self,
        confirm_new_fingerprint: Callable[[str, str], Awaitable[bool]] | None,
    ) -> None:
        assert self.via_hub is not None
        vh = self.via_hub
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(vh.hub_address, vh.hub_port, ssl=ssl_ctx),
                timeout=config.TCP_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"TCP/TLS connect to Hub {vh.hub_address}:{vh.hub_port} timed out after "
                f"{config.TCP_CONNECT_TIMEOUT_S:.0f}s"
            ) from exc
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise ConnectionError(
                f"connect to Hub {vh.hub_address}:{vh.hub_port} failed: {exc}"
            ) from exc

        host_key = f"hub:{vh.hub_address}:{vh.hub_port}"
        await self._verify_fingerprint(host_key, confirm_new_fingerprint)

        # Tell the Hub which laptop we want to reach
        try:
            await write_hub_message(self.writer, hello_connect_via(vh.laptop_name))
            ack = await read_hub_message(self.reader, timeout=10.0)
        except (ConnectionError, OSError, ValueError, asyncio.TimeoutError) as exc:
            await self.close()
            raise ConnectionError(f"Hub HELLO failed: {exc}") from exc

        if not ack.get("ok"):
            await self.close()
            raise AuthError(str(ack.get("reason") or "Hub rejected the connect_via"))
        # From here on, reader/writer is transparently relaying to the laptop's
        # plaintext loopback host listener.

    async def _verify_fingerprint(
        self,
        host_key: str,
        confirm_new_fingerprint: Callable[[str, str], Awaitable[bool]] | None,
    ) -> None:
        assert self.writer is not None
        ssl_obj: ssl.SSLObject | None = self.writer.get_extra_info("ssl_object")
        cert_der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
        if not cert_der:
            await self.close()
            raise ConnectionError("no peer cert")
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_der_x509_certificate(cert_der)
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        fp = crypto.cert_fingerprint(cert_pem)

        pins = load_pins()
        existing = pins.get(host_key)
        if existing:
            if existing != fp:
                await self.close()
                raise FingerprintMismatchError(existing, fp)
            logger.info("connect: cert fingerprint matches pinned for %s", host_key)
        else:
            logger.info("connect: NEW fingerprint for %s (%s); awaiting user confirmation",
                        host_key, fp[:23] + "...")
            ok = True
            if confirm_new_fingerprint is not None:
                ok = await confirm_new_fingerprint(host_key, fp)
            if not ok:
                await self.close()
                raise AuthError("user rejected new fingerprint")
            pins[host_key] = fp
            save_pins(pins)
            logger.info("connect: user accepted new fingerprint for %s", host_key)
        # Stash for HostInfo if direct mode; via-Hub will overwrite later.
        self._opened_fingerprint = fp
