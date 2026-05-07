"""End-to-end via-Hub: real HostServer + Hub broker + laptop bridge + HostClient.

Verifies that a client routed through the Hub completes the same auth
handshake against a host listening on a plaintext loopback port.
"""
from __future__ import annotations

import asyncio
import ssl

import pytest

from app.client.tcp_client import HostClient, ViaHub, AuthError
from app.host.server import HostServer
from app.hub.registry import HubRegistry
from app.hub.server import HubServer
from app.shared import config, crypto
from app.shared.hub_protocol import (
    hello_register, read_hub_message, write_hub_message,
)
from app.shared.mux import MuxConnection


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    cert = tmp_path / "host_cert.pem"
    key = tmp_path / "host_key.pem"
    pins = tmp_path / "pinned_certs.json"
    monkeypatch.setattr(config, "HOST_CERT_PATH", cert)
    monkeypatch.setattr(config, "HOST_KEY_PATH", key)
    monkeypatch.setattr(config, "PINNED_CERTS_PATH", pins)
    return tmp_path


def _client_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _yes(_h, _fp) -> bool:
    return True


@pytest.mark.asyncio
async def test_via_hub_handshake_succeeds(isolated_paths) -> None:
    # 1. host server on loopback (plaintext)
    host = HostServer(
        get_pin=lambda: "482817",
        port=0,
        bind_address="127.0.0.1",
    )
    host.get_monitors = lambda: [{"index": 0, "name": "Test", "width": 1920, "height": 1080}]
    await host.start()              # public TLS listener on a random port
    loopback_port = await host.start_loopback(0)
    serve_host = asyncio.create_task(host.serve_forever())

    # 2. Hub broker
    hub_registry = HubRegistry(path=isolated_paths / "regs.json")
    hub = HubServer(
        registry=hub_registry,
        port=0,
        bind_address="127.0.0.1",
    )
    await hub.start()
    hub_port = hub._server.sockets[0].getsockname()[1]
    serve_hub = asyncio.create_task(hub.serve_forever())

    reg = hub_registry.add("laptop1")

    # 3. Fake laptop: register with Hub and bridge incoming streams to host loopback
    laptop_done = asyncio.Event()

    async def fake_laptop() -> None:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", hub_port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(writer, hello_register("laptop1", reg.token))
        ack = await read_hub_message(reader)
        assert ack.get("ok") is True
        mux = MuxConnection(reader, writer, is_initiator=False, keepalive_s=0)
        run = asyncio.create_task(mux.run())

        async def serve_one(stream) -> None:
            # Bridge to host loopback
            try:
                local_r, local_w = await asyncio.open_connection("127.0.0.1", loopback_port)
            except Exception:
                stream.close(error=True)
                return

            async def forward_a():
                try:
                    while True:
                        chunk = await stream.reader.read(64 * 1024)
                        if not chunk:
                            break
                        local_w.write(chunk)
                        await local_w.drain()
                finally:
                    local_w.close()

            async def forward_b():
                try:
                    while True:
                        chunk = await local_r.read(64 * 1024)
                        if not chunk:
                            break
                        if not await stream.write(chunk):
                            break
                finally:
                    stream.close()

            await asyncio.gather(forward_a(), forward_b(), return_exceptions=True)

        try:
            while True:
                stream = await mux.accept_stream()
                asyncio.create_task(serve_one(stream))
        except Exception:
            pass
        finally:
            run.cancel()
            laptop_done.set()

    laptop_task = asyncio.create_task(fake_laptop())
    # wait for laptop to register
    for _ in range(40):
        if hub.is_online("laptop1"):
            break
        await asyncio.sleep(0.05)
    assert hub.is_online("laptop1")

    # 4. HostClient via Hub
    try:
        client = HostClient(
            address="ignored",
            port=0,
            client_name="testclient",
            via_hub=ViaHub(
                hub_address="127.0.0.1",
                hub_port=hub_port,
                laptop_name="laptop1",
            ),
        )
        info = await asyncio.wait_for(
            client.connect(pin="482817", confirm_new_fingerprint=_yes),
            timeout=10.0,
        )
        assert info.monitors and info.monitors[0]["width"] == 1920
        assert info.fingerprint  # the Hub's, but populated
        await client.close()
    finally:
        laptop_task.cancel()
        serve_hub.cancel()
        serve_host.cancel()
        for t in (serve_hub, serve_host, laptop_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await hub.stop()
        await host.stop()


@pytest.mark.asyncio
async def test_via_hub_wrong_pin_rejected(isolated_paths) -> None:
    host = HostServer(get_pin=lambda: "482817", port=0, bind_address="127.0.0.1")
    host.get_monitors = lambda: [{"index": 0, "name": "Test", "width": 800, "height": 600}]
    await host.start()
    loopback_port = await host.start_loopback(0)
    serve_host = asyncio.create_task(host.serve_forever())

    hub_registry = HubRegistry(path=isolated_paths / "regs.json")
    hub = HubServer(registry=hub_registry, port=0, bind_address="127.0.0.1")
    await hub.start()
    hub_port = hub._server.sockets[0].getsockname()[1]
    serve_hub = asyncio.create_task(hub.serve_forever())
    reg = hub_registry.add("lap")

    async def fake_laptop() -> None:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", hub_port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(writer, hello_register("lap", reg.token))
        await read_hub_message(reader)
        mux = MuxConnection(reader, writer, is_initiator=False, keepalive_s=0)
        run = asyncio.create_task(mux.run())

        async def serve_one(stream) -> None:
            local_r, local_w = await asyncio.open_connection("127.0.0.1", loopback_port)
            async def fa():
                try:
                    while True:
                        c = await stream.reader.read(64 * 1024)
                        if not c: break
                        local_w.write(c); await local_w.drain()
                finally:
                    local_w.close()
            async def fb():
                try:
                    while True:
                        c = await local_r.read(64 * 1024)
                        if not c: break
                        if not await stream.write(c): break
                finally:
                    stream.close()
            await asyncio.gather(fa(), fb(), return_exceptions=True)

        try:
            while True:
                s = await mux.accept_stream()
                asyncio.create_task(serve_one(s))
        except Exception:
            pass
        finally:
            run.cancel()

    laptop_task = asyncio.create_task(fake_laptop())
    for _ in range(40):
        if hub.is_online("lap"):
            break
        await asyncio.sleep(0.05)

    try:
        client = HostClient(address="x", port=0, via_hub=ViaHub(
            hub_address="127.0.0.1", hub_port=hub_port, laptop_name="lap"
        ))
        with pytest.raises(AuthError):
            await asyncio.wait_for(
                client.connect(pin="000000", confirm_new_fingerprint=_yes),
                timeout=10.0,
            )
        await client.close()
    finally:
        laptop_task.cancel()
        serve_hub.cancel()
        serve_host.cancel()
        for t in (serve_hub, serve_host, laptop_task):
            try: await t
            except (asyncio.CancelledError, Exception): pass
        await hub.stop()
        await host.stop()
