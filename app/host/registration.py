"""
Laptop-side Hub registration: hold a persistent outbound TLS connection to
the Hub. When the Hub opens streams against this connection, bridge each
stream to a fresh local TCP connection to 127.0.0.1:<host_port>.

Auto-reconnect with exponential backoff on drop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from pathlib import Path

from app.shared import config, crypto
from app.shared.hub_protocol import (
    hello_register, read_hub_message, write_hub_message,
)
from app.shared.mux import MuxConnection, MuxStream

logger = logging.getLogger(__name__)


@dataclass
class HubRegistrationConfig:
    hub_address: str
    hub_port: int
    laptop_name: str
    token: str
    pinned_fingerprint: str = ""    # set after first successful connect

    def to_dict(self) -> dict:
        return {
            "hub_address": self.hub_address,
            "hub_port": self.hub_port,
            "laptop_name": self.laptop_name,
            "token": self.token,
            "pinned_fingerprint": self.pinned_fingerprint,
        }


def _registration_path() -> Path:
    return config.appdata_dir() / "hub_registration.json"


def load_registration() -> HubRegistrationConfig | None:
    path = _registration_path()
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return HubRegistrationConfig(
            hub_address=str(d.get("hub_address", "")),
            hub_port=int(d.get("hub_port", 7780)),
            laptop_name=str(d.get("laptop_name", "")),
            token=str(d.get("token", "")),
            pinned_fingerprint=str(d.get("pinned_fingerprint", "")),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_registration(reg: HubRegistrationConfig) -> None:
    path = _registration_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_registration() -> None:
    path = _registration_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------- runtime ----------

class HubRegistrar:
    """
    Maintains the laptop's persistent connection to the Hub. Public state:
        .status: one of "idle", "connecting", "online", "rejected", "error"
        .last_error: short string, only meaningful in error/rejected states
    """

    def __init__(
        self,
        cfg: HubRegistrationConfig,
        local_host_port: int,
        on_status_change=None,
    ) -> None:
        self.cfg = cfg
        self.local_host_port = local_host_port
        self.on_status_change = on_status_change
        self.status: str = "idle"
        self.last_error: str = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------- internal ----------

    def _set_status(self, status: str, error: str = "") -> None:
        self.status = status
        self.last_error = error
        if self.on_status_change is not None:
            try:
                self.on_status_change(status, error)
            except Exception:
                logger.exception("on_status_change raised")

    async def _run_forever(self) -> None:
        backoff = 2.0
        max_backoff = 60.0
        while not self._stop.is_set():
            self._set_status("connecting")
            try:
                await self._one_session()
            except asyncio.CancelledError:
                return
            except _RejectedError as exc:
                self._set_status("rejected", str(exc))
                logger.warning("Hub rejected registration: %s", exc)
                # Don't loop quickly when rejected - wait longer
                await asyncio.sleep(min(60.0, backoff * 4))
            except Exception as exc:
                logger.exception("Hub session error")
                self._set_status("error", str(exc))
            else:
                # Clean disconnect; reset backoff
                backoff = 2.0
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, max_backoff)

    async def _one_session(self) -> None:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.cfg.hub_address, self.cfg.hub_port, ssl=ssl_ctx,
                ),
                timeout=10.0,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            raise RuntimeError(f"could not reach Hub: {exc}") from exc

        # Cert pinning
        ssl_obj = writer.get_extra_info("ssl_object")
        cert_der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
        if not cert_der:
            writer.close()
            raise RuntimeError("no peer cert from Hub")
        from cryptography import x509 as _x509
        from cryptography.hazmat.primitives import serialization as _serial
        cert = _x509.load_der_x509_certificate(cert_der)
        fp = crypto.cert_fingerprint(cert.public_bytes(_serial.Encoding.PEM))
        if self.cfg.pinned_fingerprint and self.cfg.pinned_fingerprint != fp:
            writer.close()
            raise _RejectedError(
                f"Hub cert fingerprint changed (expected {self.cfg.pinned_fingerprint[:23]}..., "
                f"got {fp[:23]}...). If this is intentional, re-register."
            )
        if not self.cfg.pinned_fingerprint:
            self.cfg.pinned_fingerprint = fp
            try:
                save_registration(self.cfg)
            except Exception:
                logger.exception("could not persist Hub fingerprint")

        # HELLO/HELLO_ACK
        await write_hub_message(writer, hello_register(self.cfg.laptop_name, self.cfg.token))
        try:
            ack = await read_hub_message(reader, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            writer.close()
            raise RuntimeError(f"no Hub HELLO_ACK: {exc}") from exc
        if not ack.get("ok"):
            writer.close()
            raise _RejectedError(str(ack.get("reason") or "rejected"))

        self._set_status("online")
        logger.info("registered with Hub as %s", self.cfg.laptop_name)

        mux = MuxConnection(reader, writer, is_initiator=False, keepalive_s=15.0)
        run_task = asyncio.create_task(mux.run())
        accept_task = asyncio.create_task(self._accept_loop(mux))

        done, pending = await asyncio.wait(
            {run_task, accept_task, asyncio.create_task(self._stop_waiter())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await mux.close()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _stop_waiter(self) -> None:
        await self._stop.wait()

    async def _accept_loop(self, mux: MuxConnection) -> None:
        while not self._stop.is_set():
            try:
                stream = await mux.accept_stream()
            except (ConnectionError, OSError):
                return
            asyncio.create_task(self._bridge_to_local_host(stream))

    async def _bridge_to_local_host(self, stream: MuxStream) -> None:
        target = "127.0.0.1"
        port = self.local_host_port
        try:
            local_reader, local_writer = await asyncio.wait_for(
                asyncio.open_connection(target, port), timeout=5.0,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            logger.warning("local host %s:%d unavailable for incoming stream: %s",
                           target, port, exc)
            stream.close(error=True)
            return

        async def _stream_to_local() -> None:
            try:
                while True:
                    try:
                        chunk = await stream.reader.read(64 * 1024)
                    except (ConnectionError, OSError):
                        break
                    if not chunk:
                        break
                    try:
                        local_writer.write(chunk)
                        await local_writer.drain()
                    except (ConnectionError, OSError):
                        break
            finally:
                try:
                    local_writer.close()
                except Exception:
                    pass

        async def _local_to_stream() -> None:
            try:
                while True:
                    try:
                        chunk = await local_reader.read(64 * 1024)
                    except (ConnectionError, OSError):
                        break
                    if not chunk:
                        break
                    if not await stream.write(chunk):
                        break
            finally:
                stream.close()

        await asyncio.gather(
            _stream_to_local(), _local_to_stream(),
            return_exceptions=True,
        )


class _RejectedError(Exception):
    """Raised when the Hub explicitly rejects a registration. Long-backoff."""
