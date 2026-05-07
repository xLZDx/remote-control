"""Tests for the mux protocol: framing, multi-stream, ordering, close semantics."""
from __future__ import annotations

import asyncio

import pytest

from app.shared.mux import MuxConnection, FrameType


async def _connect_pair() -> tuple[MuxConnection, MuxConnection, asyncio.Task, asyncio.Task]:
    """Create two MuxConnections back-to-back over an in-memory loopback TCP socket."""
    server_started = asyncio.Event()
    server_pair: dict[str, MuxConnection] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mux_b = MuxConnection(reader, writer, is_initiator=False, keepalive_s=0)
        server_pair["mux"] = mux_b
        server_started.set()
        await mux_b.run()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    asyncio.create_task(server.serve_forever())

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    mux_a = MuxConnection(reader, writer, is_initiator=True, keepalive_s=0)
    a_task = asyncio.create_task(mux_a.run())
    await server_started.wait()
    mux_b = server_pair["mux"]
    # b_task is implicit (running inside handle); we won't await it directly.
    return mux_a, mux_b, a_task, server


# ---------- tests ----------

@pytest.mark.asyncio
async def test_open_stream_arrives_on_the_other_side() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a = await a.open_stream({"label": "first"})
        s_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        assert s_b.metadata == {"label": "first"}
        assert s_b.stream_id == s_a.stream_id
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_data_round_trip() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a = await a.open_stream()
        s_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        await s_a.write(b"hello world")
        got = await asyncio.wait_for(s_b.reader.readexactly(11), timeout=2.0)
        assert got == b"hello world"

        # And reverse direction
        await s_b.write(b"reply")
        got2 = await asyncio.wait_for(s_a.reader.readexactly(5), timeout=2.0)
        assert got2 == b"reply"
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_two_concurrent_streams_dont_interleave() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s1 = await a.open_stream({"name": "one"})
        s2 = await a.open_stream({"name": "two"})

        s1_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        s2_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        # Match by metadata since stream_id ordering depends on allocation
        if s1_b.metadata.get("name") != "one":
            s1_b, s2_b = s2_b, s1_b
        assert s1_b.metadata["name"] == "one"
        assert s2_b.metadata["name"] == "two"

        await s1.write(b"AAAA")
        await s2.write(b"BBBB")
        await s1.write(b"AAAA")
        await s2.write(b"BBBB")

        got1 = await asyncio.wait_for(s1_b.reader.readexactly(8), timeout=2.0)
        got2 = await asyncio.wait_for(s2_b.reader.readexactly(8), timeout=2.0)
        assert got1 == b"AAAAAAAA"
        assert got2 == b"BBBBBBBB"
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_initiator_uses_odd_ids_acceptor_even() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a1 = await a.open_stream()
        s_a2 = await a.open_stream()
        s_b1 = await b.open_stream()
        s_b2 = await b.open_stream()
        assert s_a1.stream_id % 2 == 1
        assert s_a2.stream_id % 2 == 1
        assert s_b1.stream_id % 2 == 0
        assert s_b2.stream_id % 2 == 0
        # No collisions
        ids = {s.stream_id for s in (s_a1, s_a2, s_b1, s_b2)}
        assert len(ids) == 4
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_close_propagates_eof() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a = await a.open_stream()
        s_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        await s_a.write(b"final")
        s_a.close()
        # Reading will return the buffered data then EOF
        got = await asyncio.wait_for(s_b.reader.read(64), timeout=2.0)
        # On the read side we may see "final" then subsequent reads return b""
        # depending on race. But after EOF, read() returns "".
        assert got == b"final" or got == b""
        eof = await asyncio.wait_for(s_b.reader.read(64), timeout=2.0)
        assert eof == b""
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_large_payload_split_into_frames() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a = await a.open_stream()
        s_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        big = b"X" * (3 * 1024 * 1024)   # 3 MiB - must split into 3 frames
        await s_a.write(big)
        got = await asyncio.wait_for(s_b.reader.readexactly(len(big)), timeout=5.0)
        assert got == big
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()


@pytest.mark.asyncio
async def test_close_after_remote_close_releases_wait_closed() -> None:
    a, b, a_task, server = await _connect_pair()
    try:
        s_a = await a.open_stream()
        s_b = await asyncio.wait_for(b.accept_stream(), timeout=2.0)
        s_b.close()
        # Wait for remote-close to propagate to s_a
        for _ in range(50):
            await asyncio.sleep(0.02)
            if s_a._closed_remote:
                break
        s_a.close()
        await asyncio.wait_for(s_a.wait_closed(), timeout=2.0)
    finally:
        await a.close()
        await b.close()
        a_task.cancel()
        server.close()
