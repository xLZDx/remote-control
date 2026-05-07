"""Tests for app.shared.protocol — framing, message types, JSON helpers."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

from app.shared import config, protocol
from app.shared.protocol import Message, MessageType


def test_header_struct_layout() -> None:
    msg = Message(MessageType.HELLO, b"abc")
    raw = msg.to_bytes()
    mtype, length = struct.unpack(">BI", raw[:5])
    assert mtype == int(MessageType.HELLO)
    assert length == 3
    assert raw[5:] == b"abc"


def test_oversize_payload_rejected() -> None:
    payload = b"\x00" * (config.MAX_FRAME_BYTES + 1)
    with pytest.raises(ValueError):
        Message(MessageType.VIDEO_FRAME, payload).to_bytes()


def test_json_roundtrip() -> None:
    msg = protocol.hello("my-laptop")
    assert msg.type is MessageType.HELLO
    data = msg.as_json()
    assert data["client_name"] == "my-laptop"
    assert data["version"] == config.PROTOCOL_VERSION


def test_video_frame_roundtrip() -> None:
    nal = b"\x00\x00\x00\x01\x67\x42\x00\x29payload"
    msg = Message.video_frame(timestamp_us=1234567, keyframe=True, nal=nal)
    ts, kf, body = msg.as_video_frame()
    assert ts == 1234567
    assert kf is True
    assert body == nal


def test_video_frame_non_keyframe() -> None:
    msg = Message.video_frame(0, False, b"x")
    _, kf, _ = msg.as_video_frame()
    assert kf is False


def test_as_video_frame_rejects_wrong_type() -> None:
    msg = protocol.ping()
    with pytest.raises(ValueError):
        msg.as_video_frame()


@pytest.mark.asyncio
async def test_read_write_roundtrip_over_pipe() -> None:
    """Run a tiny TCP loopback to verify read_message / write_message agree."""
    port_holder: dict[str, int] = {}

    async def _server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        msg = await protocol.read_message(reader)
        echoed = Message.json(msg.type, {"echo": msg.as_json()})
        await protocol.write_message(writer, echoed)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_server, host="127.0.0.1", port=0)
    port_holder["port"] = server.sockets[0].getsockname()[1]

    async def _client() -> dict:
        reader, writer = await asyncio.open_connection("127.0.0.1", port_holder["port"])
        await protocol.write_message(writer, protocol.hello("c1"))
        reply = await protocol.read_message(reader)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        return reply.as_json()

    async with server:
        result = await _client()

    assert result["echo"]["client_name"] == "c1"
