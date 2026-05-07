"""
Hub control-channel protocol: a single length-prefixed JSON message at the
very start of every TLS connection, before any mux/relay traffic.

Wire format:

    | len:4 BE | utf-8 JSON |

Roles (in HELLO from the connecting peer):
    - "register"     -> "I am laptop X, my token is Y; keep this connection
                        open and route streams here."
    - "connect_via"  -> "Pipe me to laptop X."

The Hub responds with HELLO_ACK { ok: bool, [reason: str] }.

After a successful HELLO_ACK:
    - role=register: both sides start a MuxConnection on the same socket
      (Hub as initiator, laptop as acceptor).
    - role=connect_via: Hub allocates a stream on the named laptop's mux and
      pipes raw bytes between this socket and the stream until either side
      closes.
"""
from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

_MAX_HELLO_BYTES = 16 * 1024
_LEN = struct.Struct(">I")


async def read_hub_message(reader: asyncio.StreamReader, timeout: float = 10.0) -> dict[str, Any]:
    header = await asyncio.wait_for(reader.readexactly(_LEN.size), timeout=timeout)
    (length,) = _LEN.unpack(header)
    if length > _MAX_HELLO_BYTES:
        raise ValueError(f"hub message too large: {length} bytes")
    payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout) if length else b""
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"hub message not valid utf-8 JSON: {exc}") from exc


async def write_hub_message(writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    writer.write(_LEN.pack(len(payload)) + payload)
    await writer.drain()


def hello_register(name: str, token: str) -> dict[str, Any]:
    return {"role": "register", "name": name, "token": token}


def hello_connect_via(name: str) -> dict[str, Any]:
    return {"role": "connect_via", "name": name}


def ack_ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


def ack_err(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}
