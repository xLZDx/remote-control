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

class HostClient:
    def __init__(
        self,
        address: str,
        port: int = config.DEFAULT_PORT,
        client_name: str | None = None,
    ) -> None:
        self.address = address
        self.port = port
        self.client_name = client_name or socket.gethostname() or "client"

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
        host has no existing pin; if it returns False, the connection is aborted.
        """
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE     # we do our own pinning
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        try:
            self.reader, self.writer = await asyncio.open_connection(
                self.address, self.port, ssl=ssl_ctx
            )
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            raise ConnectionError(f"connect to {self.address}:{self.port} failed: {exc}") from exc

        # cert fingerprint check
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

        host_key = f"{self.address}:{self.port}"
        pins = load_pins()
        existing = pins.get(host_key)
        if existing:
            if existing != fp:
                await self.close()
                raise FingerprintMismatchError(existing, fp)
        else:
            ok = True
            if confirm_new_fingerprint is not None:
                ok = await confirm_new_fingerprint(host_key, fp)
            if not ok:
                await self.close()
                raise AuthError("user rejected new fingerprint")
            pins[host_key] = fp
            save_pins(pins)

        # ---- application handshake ----
        await self.send(protocol.hello(self.client_name))

        msg = await asyncio.wait_for(protocol.read_message(self.reader), config.HANDSHAKE_TIMEOUT_S)
        if msg.type is not MessageType.HELLO_ACK:
            await self.close()
            raise AuthError(f"expected HELLO_ACK, got {msg.type.name}")
        hello_ack = msg.as_json()

        msg = await asyncio.wait_for(protocol.read_message(self.reader), config.HANDSHAKE_TIMEOUT_S)
        if msg.type is not MessageType.AUTH_CHALLENGE:
            await self.close()
            raise AuthError(f"expected AUTH_CHALLENGE, got {msg.type.name}")
        chal = msg.as_json()
        salt = bytes.fromhex(chal["salt"])
        nonce = bytes.fromhex(chal["nonce"])
        proof = crypto.compute_proof(pin, salt, nonce)
        await self.send(protocol.auth_response(proof.hex()))

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
            fingerprint=fp,
        )
        self._receive_task = asyncio.create_task(self._recv_loop())
        logger.info("connected to %s (%s)", host_key, self.host_info.name)
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
