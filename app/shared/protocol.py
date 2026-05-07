"""
Wire protocol for RemoteControl.

Frame layout on the wire (over TLS):

    +---------+----------+-----------------+
    | type:1  | len:4 BE | payload: <len>  |
    +---------+----------+-----------------+

`type` is a single byte from MessageType. `len` is a 32-bit big-endian length
of `payload`. Payload format depends on type:

- HELLO / HELLO_ACK / AUTH_CHALLENGE / AUTH_RESPONSE / AUTH_OK / AUTH_FAIL /
  ERROR / PING / PONG / VIDEO_CONFIG / INPUT_EVENT / CLIPBOARD / FILE_*:
    JSON-encoded UTF-8 dict.
- VIDEO_FRAME:
    8 bytes timestamp (uint64 BE microseconds) + 1 byte flags + raw H.264 NALu(s).
    flags bit 0 = keyframe.
"""
from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from . import config


class MessageType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    AUTH_CHALLENGE = 3
    AUTH_RESPONSE = 4
    AUTH_OK = 5
    AUTH_FAIL = 6
    ERROR = 7
    PING = 8
    PONG = 9
    VIDEO_CONFIG = 10
    VIDEO_FRAME = 11
    INPUT_EVENT = 12
    CLIPBOARD = 13
    FILE_OFFER = 14
    FILE_CHUNK = 15
    FILE_ACK = 16
    BYE = 17
    REKEY = 18
    SET_MONITOR = 19
    SET_VIEW_ONLY = 20


_HEADER = struct.Struct(">BI")
_VIDEO_HEADER = struct.Struct(">QB")


@dataclass
class Message:
    type: MessageType
    payload: bytes

    def to_bytes(self) -> bytes:
        if len(self.payload) > config.MAX_FRAME_BYTES:
            raise ValueError("payload exceeds MAX_FRAME_BYTES")
        return _HEADER.pack(int(self.type), len(self.payload)) + self.payload

    # ----- json helpers -----

    @classmethod
    def json(cls, mtype: MessageType, data: dict[str, Any]) -> "Message":
        return cls(mtype, json.dumps(data, separators=(",", ":")).encode("utf-8"))

    def as_json(self) -> dict[str, Any]:
        return json.loads(self.payload.decode("utf-8"))

    # ----- video helpers -----

    @classmethod
    def video_frame(cls, timestamp_us: int, keyframe: bool, nal: bytes) -> "Message":
        flags = 1 if keyframe else 0
        return cls(MessageType.VIDEO_FRAME, _VIDEO_HEADER.pack(timestamp_us, flags) + nal)

    def as_video_frame(self) -> tuple[int, bool, bytes]:
        if self.type is not MessageType.VIDEO_FRAME:
            raise ValueError("not a VIDEO_FRAME")
        ts, flags = _VIDEO_HEADER.unpack_from(self.payload, 0)
        return ts, bool(flags & 1), self.payload[_VIDEO_HEADER.size:]


async def read_message(reader: asyncio.StreamReader) -> Message:
    """Read one length-prefixed message. Raises asyncio.IncompleteReadError on EOF."""
    header = await reader.readexactly(_HEADER.size)
    mtype_int, length = _HEADER.unpack(header)
    if length > config.MAX_FRAME_BYTES:
        raise ValueError(f"message length {length} exceeds cap")
    payload = await reader.readexactly(length) if length else b""
    return Message(MessageType(mtype_int), payload)


async def write_message(writer: asyncio.StreamWriter, msg: Message) -> None:
    writer.write(msg.to_bytes())
    await writer.drain()


# ----- Convenience constructors -----

def hello(client_name: str) -> Message:
    return Message.json(MessageType.HELLO, {
        "version": config.PROTOCOL_VERSION,
        "client_name": client_name,
    })

def hello_ack(host_name: str, monitors: list[dict[str, Any]]) -> Message:
    return Message.json(MessageType.HELLO_ACK, {
        "version": config.PROTOCOL_VERSION,
        "host_name": host_name,
        "monitors": monitors,
    })

def auth_challenge(salt_hex: str, nonce_hex: str) -> Message:
    return Message.json(MessageType.AUTH_CHALLENGE, {"salt": salt_hex, "nonce": nonce_hex})

def auth_response(proof_hex: str) -> Message:
    return Message.json(MessageType.AUTH_RESPONSE, {"proof": proof_hex})

def auth_ok(view_only: bool = False) -> Message:
    return Message.json(MessageType.AUTH_OK, {"view_only": view_only})

def auth_fail(reason: str) -> Message:
    return Message.json(MessageType.AUTH_FAIL, {"reason": reason})

def error(reason: str) -> Message:
    return Message.json(MessageType.ERROR, {"reason": reason})

def ping() -> Message:
    return Message(MessageType.PING, b"")

def pong() -> Message:
    return Message(MessageType.PONG, b"")

def video_config(width: int, height: int, fps: int, codec: str = "h264") -> Message:
    return Message.json(MessageType.VIDEO_CONFIG, {
        "width": width, "height": height, "fps": fps, "codec": codec,
    })

def bye() -> Message:
    return Message(MessageType.BYE, b"")
