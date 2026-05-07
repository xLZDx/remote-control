"""
End-to-end Hub routing tests:
- Register a fake laptop with a valid token
- Have a viewer connect through the Hub and exchange bytes
- Verify rejection on bad token
- Verify multiple concurrent viewers
"""
from __future__ import annotations

import asyncio
import ssl

import pytest

from app.hub.registry import HubRegistry
from app.hub.server import HubServer
from app.shared import config, crypto
from app.shared.hub_protocol import (
    hello_connect_via, hello_register, read_hub_message, write_hub_message,
)
from app.shared.mux import MuxConnection


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect cert + registry paths into a temp dir per test."""
    cert = tmp_path / "host_cert.pem"
    key = tmp_path / "host_key.pem"
    monkeypatch.setattr(config, "HOST_CERT_PATH", cert)
    monkeypatch.setattr(config, "HOST_KEY_PATH", key)
    return tmp_path


def _client_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _start_hub(tmp_path) -> tuple[HubServer, int, asyncio.Task]:
    registry = HubRegistry(path=tmp_path / "regs.json")
    hub = HubServer(
        registry=registry,
        port=0,
        bind_address="127.0.0.1",
        cert_path=tmp_path / "host_cert.pem",
        key_path=tmp_path / "host_key.pem",
    )
    await hub.start()
    port = hub._server.sockets[0].getsockname()[1]
    serve_task = asyncio.create_task(hub.serve_forever())
    return hub, port, serve_task


async def _stop(hub: HubServer, serve_task: asyncio.Task) -> None:
    serve_task.cancel()
    try:
        await serve_task
    except (asyncio.CancelledError, Exception):
        pass
    await hub.stop()


# ---------- tests ----------

@pytest.mark.asyncio
async def test_register_with_valid_token(isolated_paths) -> None:
    hub, port, serve = await _start_hub(isolated_paths)
    try:
        reg = hub.registry.add("laptop1")
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(writer, hello_register("laptop1", reg.token))
        ack = await asyncio.wait_for(read_hub_message(reader), timeout=2.0)
        assert ack.get("ok") is True
        # Registration should appear active soon
        for _ in range(20):
            if hub.is_online("laptop1"):
                break
            await asyncio.sleep(0.05)
        assert hub.is_online("laptop1")
        writer.close()
    finally:
        await _stop(hub, serve)


@pytest.mark.asyncio
async def test_register_with_bad_token_rejected(isolated_paths) -> None:
    hub, port, serve = await _start_hub(isolated_paths)
    try:
        hub.registry.add("laptop1")
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(writer, hello_register("laptop1", "wrong-token"))
        ack = await asyncio.wait_for(read_hub_message(reader), timeout=2.0)
        assert ack.get("ok") is False
        assert "token" in str(ack.get("reason", "")).lower()
        writer.close()
    finally:
        await _stop(hub, serve)


@pytest.mark.asyncio
async def test_connect_via_offline_laptop_is_unavailable(isolated_paths) -> None:
    hub, port, serve = await _start_hub(isolated_paths)
    try:
        hub.registry.add("laptop1")
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(writer, hello_connect_via("laptop1"))
        ack = await asyncio.wait_for(read_hub_message(reader), timeout=2.0)
        assert ack.get("ok") is False
        assert "not available" in str(ack.get("reason", "")).lower()
        writer.close()
    finally:
        await _stop(hub, serve)


@pytest.mark.asyncio
async def test_end_to_end_bytes_pipe(isolated_paths) -> None:
    """Full path: laptop registers, viewer connect_via, bytes flow both ways."""
    hub, port, serve = await _start_hub(isolated_paths)
    try:
        reg = hub.registry.add("laptop1")

        # ----- Fake laptop: connect, register, accept incoming streams,
        # echo bytes back with "ECHO:" prefix.
        async def fake_laptop() -> None:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=_client_ssl_ctx()
            )
            await write_hub_message(writer, hello_register("laptop1", reg.token))
            ack = await read_hub_message(reader)
            assert ack.get("ok") is True
            mux = MuxConnection(reader, writer, is_initiator=False, keepalive_s=0)
            run = asyncio.create_task(mux.run())

            async def _serve_one(stream) -> None:
                try:
                    chunk = await asyncio.wait_for(stream.reader.read(64 * 1024), timeout=2.0)
                    if chunk:
                        await stream.write(b"ECHO:" + chunk)
                finally:
                    stream.close()

            try:
                while True:
                    stream = await mux.accept_stream()
                    asyncio.create_task(_serve_one(stream))
            except Exception:
                pass
            finally:
                run.cancel()

        laptop_task = asyncio.create_task(fake_laptop())

        # Wait for laptop to register
        for _ in range(40):
            if hub.is_online("laptop1"):
                break
            await asyncio.sleep(0.05)
        assert hub.is_online("laptop1")

        # ----- Fake viewer: connect_via, send bytes, expect echo
        v_reader, v_writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=_client_ssl_ctx()
        )
        await write_hub_message(v_writer, hello_connect_via("laptop1"))
        ack = await asyncio.wait_for(read_hub_message(v_reader), timeout=2.0)
        assert ack.get("ok") is True
        v_writer.write(b"hello-laptop")
        await v_writer.drain()
        got = await asyncio.wait_for(v_reader.readexactly(len(b"ECHO:hello-laptop")), timeout=3.0)
        assert got == b"ECHO:hello-laptop"
        v_writer.close()

        laptop_task.cancel()
    finally:
        await _stop(hub, serve)


@pytest.mark.asyncio
async def test_two_concurrent_viewers_through_same_laptop(isolated_paths) -> None:
    hub, port, serve = await _start_hub(isolated_paths)
    try:
        reg = hub.registry.add("lap")

        async def fake_laptop() -> None:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=_client_ssl_ctx()
            )
            await write_hub_message(writer, hello_register("lap", reg.token))
            await read_hub_message(reader)
            mux = MuxConnection(reader, writer, is_initiator=False, keepalive_s=0)
            run = asyncio.create_task(mux.run())

            async def serve(stream) -> None:
                try:
                    chunk = await asyncio.wait_for(stream.reader.read(64 * 1024), timeout=3.0)
                    if chunk:
                        await stream.write(b"R:" + chunk)
                finally:
                    stream.close()

            try:
                while True:
                    stream = await mux.accept_stream()
                    asyncio.create_task(serve(stream))
            except Exception:
                pass
            finally:
                run.cancel()

        laptop = asyncio.create_task(fake_laptop())
        for _ in range(40):
            if hub.is_online("lap"):
                break
            await asyncio.sleep(0.05)

        async def viewer(payload: bytes) -> bytes:
            r, w = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ssl_ctx())
            await write_hub_message(w, hello_connect_via("lap"))
            ack = await read_hub_message(r)
            assert ack.get("ok") is True
            w.write(payload)
            await w.drain()
            got = await asyncio.wait_for(r.readexactly(len(payload) + 2), timeout=3.0)
            w.close()
            return got

        a, b = await asyncio.gather(viewer(b"AAA"), viewer(b"BBB"))
        assert a == b"R:AAA"
        assert b == b"R:BBB"
        laptop.cancel()
    finally:
        await _stop(hub, serve)


def test_registry_token_lifecycle(tmp_path) -> None:
    reg = HubRegistry(path=tmp_path / "regs.json")
    r1 = reg.add("laptop1")
    assert len(r1.token) == 32
    assert reg.verify("laptop1", r1.token) is True
    assert reg.verify("laptop1", "wrong") is False
    # Case-insensitive name match
    assert reg.verify("LAPTOP1", r1.token) is True

    # Re-add overwrites
    r2 = reg.add("laptop1")
    assert r2.token != r1.token
    assert reg.verify("laptop1", r1.token) is False
    assert reg.verify("laptop1", r2.token) is True

    # Persistence
    reg2 = HubRegistry(path=tmp_path / "regs.json")
    assert reg2.verify("laptop1", r2.token) is True

    # Remove
    assert reg2.remove("laptop1") is True
    assert reg2.verify("laptop1", r2.token) is False
    assert reg2.remove("laptop1") is False
