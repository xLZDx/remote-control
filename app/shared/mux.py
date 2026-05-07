"""
Stream multiplexing over a single asyncio Reader/Writer pair.

A MuxConnection runs over one TLS connection (typically the persistent
laptop -> Hub tunnel). Each side can open multiple logical streams; the Hub
uses this to relay client <-> laptop traffic.

Wire format per frame:

    +--------------+------+--------+--------+--------------+
    | stream_id:4  |type:1|flags:1 | len:4  | payload<len> |
    +--------------+------+--------+--------+--------------+

stream_id and len are unsigned 32-bit big-endian.

Types:
  0x01 OPEN     - sender opens a new stream. Payload = utf-8 JSON metadata
                  (optional; may be empty).
  0x02 DATA     - bytes for the named stream.
  0x03 CLOSE    - stream closed; flags bit 0 = error.
  0x04 PING     - keepalive (no stream).
  0x05 PONG     - reply to PING.

Stream-id allocation:
  - Initiator-side IDs are odd (1, 3, 5, ...).
  - Acceptor-side IDs are even (2, 4, 6, ...).
  - 0 is reserved for control frames (PING/PONG).

Public API:
  - MuxConnection(reader, writer, is_initiator)
  - await mux.run() -> drives the read loop until the connection drops
  - await mux.open_stream(metadata={...}) -> MuxStream
  - await mux.accept_stream() -> next inbound stream
  - mux.close()

Each MuxStream exposes:
  - .reader: asyncio.StreamReader (fed with DATA bytes by the mux loop)
  - async write(data: bytes) -> bool: ship DATA frame; returns False if closed
  - async drain(): TCP backpressure
  - close(): emit CLOSE frame
  - await wait_closed()
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)


class FrameType(IntEnum):
    OPEN = 0x01
    DATA = 0x02
    CLOSE = 0x03
    PING = 0x04
    PONG = 0x05


_HEADER = struct.Struct(">IBBI")   # stream_id, type, flags, len
MAX_PAYLOAD_BYTES = 1 * 1024 * 1024     # 1 MiB per frame; larger chunks split
_DEFAULT_READ_BUFFER = 4 * 1024 * 1024  # per-stream feed cap


# ---------- Stream object ----------

@dataclass
class MuxStream:
    """One logical stream inside a MuxConnection. Looks roughly like an asyncio
    StreamReader/Writer pair."""
    stream_id: int
    metadata: dict
    _conn: "MuxConnection"
    reader: asyncio.StreamReader = field(default_factory=lambda: asyncio.StreamReader(limit=_DEFAULT_READ_BUFFER))
    _closed_local: bool = False
    _closed_remote: bool = False
    _close_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def closed(self) -> bool:
        return self._closed_local and self._closed_remote

    async def write(self, data: bytes) -> bool:
        if self._closed_local or self._conn.closed:
            return False
        # Split larger-than-MAX into chunks.
        view = memoryview(data)
        i = 0
        while i < len(view):
            chunk = view[i:i + MAX_PAYLOAD_BYTES]
            ok = await self._conn._send_frame(FrameType.DATA, self.stream_id, 0, bytes(chunk))
            if not ok:
                return False
            i += len(chunk)
        return True

    async def drain(self) -> None:
        await self._conn._drain()

    def close(self, error: bool = False) -> None:
        if self._closed_local:
            return
        self._closed_local = True
        flags = 1 if error else 0
        # Fire-and-forget; we don't await here so close() stays sync-friendly.
        asyncio.create_task(self._conn._send_frame(FrameType.CLOSE, self.stream_id, flags, b""))
        self.reader.feed_eof()
        if self._closed_remote:
            self._close_event.set()

    async def wait_closed(self) -> None:
        await self._close_event.wait()

    # internal: called by MuxConnection on inbound CLOSE
    def _on_remote_close(self, error: bool) -> None:
        self._closed_remote = True
        self.reader.feed_eof()
        if self._closed_local:
            self._close_event.set()

    # internal
    def _on_data(self, payload: bytes) -> None:
        if not self._closed_remote:
            self.reader.feed_data(payload)


# ---------- Multiplexed connection ----------

class MuxConnection:
    """
    Multiplex many streams over a single (reader, writer) transport.

    Usage:
        mux = MuxConnection(reader, writer, is_initiator=True)
        run_task = asyncio.create_task(mux.run())
        stream = await mux.open_stream({"name": "client42"})
        await stream.write(b"hello")
        ...
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        is_initiator: bool,
        keepalive_s: float = 15.0,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.is_initiator = is_initiator
        self.keepalive_s = keepalive_s

        # Stream-id allocation: initiator picks odd, acceptor picks even.
        self._next_id = 1 if is_initiator else 2
        self._streams: dict[int, MuxStream] = {}
        self._streams_lock = asyncio.Lock()
        self._inbound: asyncio.Queue[MuxStream] = asyncio.Queue()
        self._send_lock = asyncio.Lock()

        self._closed = asyncio.Event()
        self._keepalive_task: asyncio.Task | None = None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ---------- public ----------

    async def run(self) -> None:
        """Drive the read loop until the underlying connection drops."""
        if self.keepalive_s > 0:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        try:
            while not self._closed.is_set():
                try:
                    header = await self.reader.readexactly(_HEADER.size)
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                stream_id, ftype_i, flags, length = _HEADER.unpack(header)
                if length > MAX_PAYLOAD_BYTES:
                    logger.warning("mux: oversize frame len=%d, dropping connection", length)
                    break
                payload = await self.reader.readexactly(length) if length else b""
                try:
                    ftype = FrameType(ftype_i)
                except ValueError:
                    logger.debug("mux: unknown type 0x%02x, ignoring", ftype_i)
                    continue
                await self._dispatch(stream_id, ftype, flags, payload)
        finally:
            await self.close()

    async def open_stream(self, metadata: dict | None = None) -> MuxStream:
        if self._closed.is_set():
            raise ConnectionError("mux closed")
        sid = self._allocate_id()
        meta = metadata or {}
        stream = MuxStream(stream_id=sid, metadata=meta, _conn=self)
        async with self._streams_lock:
            self._streams[sid] = stream
        await self._send_frame(
            FrameType.OPEN, sid, 0,
            json.dumps(meta, separators=(",", ":")).encode("utf-8"),
        )
        return stream

    async def accept_stream(self) -> MuxStream:
        return await self._inbound.get()

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        # Close all live streams
        async with self._streams_lock:
            streams = list(self._streams.values())
        for s in streams:
            s._on_remote_close(error=True)
            if not s._closed_local:
                s._closed_local = True
                s.reader.feed_eof()
        # Cancel keepalive
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        # Tear down transport
        try:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        except Exception:
            pass

    # ---------- internal ----------

    def _allocate_id(self) -> int:
        sid = self._next_id
        self._next_id += 2
        # Skip 0 (reserved for control frames; not reachable with our +2 ladder)
        return sid

    async def _send_frame(self, ftype: FrameType, sid: int, flags: int, payload: bytes) -> bool:
        if self._closed.is_set():
            return False
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds MAX_PAYLOAD_BYTES")
        header = _HEADER.pack(sid, int(ftype), flags & 0xFF, len(payload))
        async with self._send_lock:
            try:
                self.writer.write(header)
                if payload:
                    self.writer.write(payload)
                return True
            except (ConnectionError, OSError):
                self._closed.set()
                return False

    async def _drain(self) -> None:
        if self._closed.is_set():
            return
        try:
            await self.writer.drain()
        except (ConnectionError, OSError):
            self._closed.set()

    async def _dispatch(self, stream_id: int, ftype: FrameType, flags: int, payload: bytes) -> None:
        if ftype is FrameType.PING:
            await self._send_frame(FrameType.PONG, 0, 0, b"")
            return
        if ftype is FrameType.PONG:
            return
        if ftype is FrameType.OPEN:
            try:
                meta = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                meta = {}
            stream = MuxStream(stream_id=stream_id, metadata=meta, _conn=self)
            async with self._streams_lock:
                self._streams[stream_id] = stream
            await self._inbound.put(stream)
            return
        async with self._streams_lock:
            stream = self._streams.get(stream_id)
        if stream is None:
            # Unknown stream - send CLOSE so the other side cleans up.
            await self._send_frame(FrameType.CLOSE, stream_id, 1, b"")
            return
        if ftype is FrameType.DATA:
            stream._on_data(payload)
        elif ftype is FrameType.CLOSE:
            stream._on_remote_close(error=bool(flags & 1))
            async with self._streams_lock:
                if stream._closed_local:
                    self._streams.pop(stream_id, None)

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self.keepalive_s)
                if self._closed.is_set():
                    return
                ok = await self._send_frame(FrameType.PING, 0, 0, b"")
                if not ok:
                    return
        except asyncio.CancelledError:
            pass
