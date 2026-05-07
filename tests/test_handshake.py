"""End-to-end handshake test: HostServer + HostClient over real TLS loopback."""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.host.server import HostServer
from app.client.tcp_client import HostClient, AuthError
from app.shared import config, crypto


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect cert / pin storage to a temp dir for each test."""
    cert = tmp_path / "host_cert.pem"
    key = tmp_path / "host_key.pem"
    pins = tmp_path / "pinned_certs.json"
    monkeypatch.setattr(config, "HOST_CERT_PATH", cert)
    monkeypatch.setattr(config, "HOST_KEY_PATH", key)
    monkeypatch.setattr(config, "PINNED_CERTS_PATH", pins)
    yield


async def _start_server(pin: str) -> tuple[HostServer, int]:
    server = HostServer(get_pin=lambda: pin, port=0, bind_address="127.0.0.1")
    server.get_monitors = lambda: [{"index": 0, "name": "Test", "width": 1920, "height": 1080}]
    await server.start()
    sock = server._server.sockets[0]
    port = sock.getsockname()[1]
    return server, port


async def _stop_server(server: HostServer, serve_task: asyncio.Task) -> None:
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass
    await server.stop()


@pytest.mark.asyncio
async def test_handshake_success() -> None:
    server, port = await _start_server("482817")
    serve = asyncio.create_task(server.serve_forever())
    try:
        client = HostClient("127.0.0.1", port, client_name="test-c")
        info = await client.connect(pin="482817", confirm_new_fingerprint=lambda h, fp: _yes())
        assert info.name
        assert len(info.monitors) == 1
        assert info.monitors[0]["width"] == 1920
        assert len(info.fingerprint.split(":")) == 32
        await client.close()
    finally:
        await _stop_server(server, serve)


@pytest.mark.asyncio
async def test_handshake_wrong_pin() -> None:
    server, port = await _start_server("482817")
    serve = asyncio.create_task(server.serve_forever())
    try:
        client = HostClient("127.0.0.1", port, client_name="test-c")
        with pytest.raises(AuthError):
            await client.connect(pin="000000", confirm_new_fingerprint=lambda h, fp: _yes())
        await client.close()
    finally:
        await _stop_server(server, serve)


@pytest.mark.asyncio
async def test_handshake_user_rejects_fingerprint() -> None:
    server, port = await _start_server("482817")
    serve = asyncio.create_task(server.serve_forever())
    try:
        client = HostClient("127.0.0.1", port, client_name="test-c")
        with pytest.raises(AuthError):
            await client.connect(pin="482817", confirm_new_fingerprint=lambda h, fp: _no())
        await client.close()
    finally:
        await _stop_server(server, serve)


@pytest.mark.asyncio
async def test_two_concurrent_clients() -> None:
    server, port = await _start_server("482817")
    serve = asyncio.create_task(server.serve_forever())
    try:
        c1 = HostClient("127.0.0.1", port, client_name="c1")
        c2 = HostClient("127.0.0.1", port, client_name="c2")
        await c1.connect(pin="482817", confirm_new_fingerprint=lambda h, fp: _yes())
        await c2.connect(pin="482817", confirm_new_fingerprint=lambda h, fp: _yes())
        # let the server register both sessions
        for _ in range(20):
            if len(server.sessions) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(server.sessions) == 2
        names = sorted(s.client_name for s in server.sessions)
        assert names == ["c1", "c2"]
        await c1.close()
        await c2.close()
    finally:
        await _stop_server(server, serve)


# ---- helpers ----

async def _yes() -> bool:
    return True

async def _no() -> bool:
    return False
