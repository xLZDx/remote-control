"""
FrameBroadcaster: encode-once, fan-out to N viewers.

The broadcaster owns the encoder. New viewers receive the most recent
keyframe before any subsequent frames, so they have a valid decoder context.

Each viewer registers a `Subscriber` (an async send callable). The
broadcaster pushes each encoded packet to every subscriber. Slow subscribers
are dropped if their queue overflows beyond a threshold.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.host.encoder import H264Encoder, EncodedPacket

logger = logging.getLogger(__name__)

SendFn = Callable[[EncodedPacket], Awaitable[bool]]


@dataclass
class Subscriber:
    sid: str
    send: SendFn
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    needs_keyframe: bool = True
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    backpressure_drops: int = 0


class FrameBroadcaster:
    """
    Subscribers register, then the host calls `feed_frame(bgr, ts)` which
    encodes once and ships to all subscribers.
    """

    def __init__(self, encoder: H264Encoder) -> None:
        self.encoder = encoder
        self._subs: dict[str, Subscriber] = {}
        self._sub_lock = asyncio.Lock()
        self._last_keyframe: EncodedPacket | None = None
        self._delivered = 0
        self._encoded = 0
        self._last_stats_t = time.monotonic()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    async def add_subscriber(self, sid: str, send: SendFn) -> Subscriber:
        sub = Subscriber(sid=sid, send=send)
        async with self._sub_lock:
            self._subs[sid] = sub
        # New subscriber needs a keyframe
        self.encoder.request_keyframe()
        # Replay last keyframe immediately so they aren't waiting for the next GOP boundary
        if self._last_keyframe is not None:
            await sub.send(self._last_keyframe)
            sub.needs_keyframe = False
        asyncio.create_task(self._sub_pump(sub))
        return sub

    async def remove_subscriber(self, sid: str) -> None:
        async with self._sub_lock:
            sub = self._subs.pop(sid, None)
        if sub is not None:
            sub.closed.set()
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def feed_frame(self, bgr_frame, timestamp_us: int) -> None:
        if not self._subs:
            return  # don't burn CPU encoding to no one
        for packet in self.encoder.encode(bgr_frame, timestamp_us):
            self._encoded += 1
            if packet.keyframe:
                self._last_keyframe = packet
            await self._dispatch(packet)
        self._maybe_log_stats()

    async def _dispatch(self, packet: EncodedPacket) -> None:
        for sub in list(self._subs.values()):
            if sub.closed.is_set():
                continue
            if sub.needs_keyframe and not packet.keyframe:
                continue
            sub.needs_keyframe = False
            try:
                sub.queue.put_nowait(packet)
            except asyncio.QueueFull:
                # backpressure: drop frames; force a keyframe for this subscriber
                sub.backpressure_drops += 1
                sub.needs_keyframe = True
                self.encoder.request_keyframe()
                if sub.backpressure_drops % 50 == 0:
                    logger.warning(
                        "subscriber %s dropped %d frames (backpressure)",
                        sub.sid, sub.backpressure_drops,
                    )

    async def _sub_pump(self, sub: Subscriber) -> None:
        try:
            while not sub.closed.is_set():
                packet = await sub.queue.get()
                if packet is None:
                    return
                ok = await sub.send(packet)
                if ok:
                    self._delivered += 1
                else:
                    sub.closed.set()
                    return
        except Exception:
            logger.exception("sub_pump error for %s", sub.sid)

    def _maybe_log_stats(self) -> None:
        now = time.monotonic()
        if now - self._last_stats_t < 5.0:
            return
        dt = now - self._last_stats_t
        logger.info(
            "broadcast: %.1f enc/s -> %.1f deliv/s across %d subs",
            self._encoded / dt, self._delivered / dt, len(self._subs),
        )
        self._last_stats_t = now
        self._encoded = 0
        self._delivered = 0
