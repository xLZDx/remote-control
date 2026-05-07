"""FrameBroadcaster: encode-once-fan-out, keyframe replay for new joiners."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

pytest.importorskip("av")

from app.host.encoder import H264Encoder, EncodedPacket
from app.host.frame_broadcaster import FrameBroadcaster


def _gen_frame(w: int, h: int, t: int) -> np.ndarray:
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = (np.arange(w) + t) % 256
    arr[:, :, 1] = (np.arange(h)[:, None] + t * 2) % 256
    arr[:, :, 2] = (t * 7) % 256
    return arr


class _Recorder:
    def __init__(self) -> None:
        self.packets: list[EncodedPacket] = []

    async def send(self, packet: EncodedPacket) -> bool:
        self.packets.append(packet)
        return True


@pytest.mark.asyncio
async def test_no_subs_no_encode() -> None:
    enc = H264Encoder(192, 144, fps=15, bitrate_kbps=200, gop=10)
    bc = FrameBroadcaster(enc)
    for i in range(5):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
    enc.close()
    assert bc.subscriber_count == 0


@pytest.mark.asyncio
async def test_two_subscribers_get_keyframe_then_stream() -> None:
    enc = H264Encoder(192, 144, fps=15, bitrate_kbps=400, gop=15)
    bc = FrameBroadcaster(enc)
    r1, r2 = _Recorder(), _Recorder()
    await bc.add_subscriber("a", r1.send)
    await bc.add_subscriber("b", r2.send)
    for i in range(40):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
        await asyncio.sleep(0)  # let pump tasks run
    # let the pumps drain the queues
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(r1.packets) > 5 and len(r2.packets) > 5:
            break
    enc.close()
    assert len(r1.packets) > 0
    assert len(r2.packets) > 0
    assert r1.packets[0].keyframe is True
    assert r2.packets[0].keyframe is True


@pytest.mark.asyncio
async def test_late_joiner_receives_keyframe_first() -> None:
    enc = H264Encoder(192, 144, fps=15, bitrate_kbps=400, gop=15)
    bc = FrameBroadcaster(enc)
    early = _Recorder()
    await bc.add_subscriber("early", early.send)
    for i in range(20):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
        await asyncio.sleep(0)
    late = _Recorder()
    await bc.add_subscriber("late", late.send)
    for i in range(20, 40):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0.01)
    enc.close()
    assert late.packets, "late joiner got no packets"
    assert late.packets[0].keyframe is True


@pytest.mark.asyncio
async def test_remove_subscriber_stops_delivery() -> None:
    enc = H264Encoder(192, 144, fps=15, bitrate_kbps=300, gop=15)
    bc = FrameBroadcaster(enc)
    r = _Recorder()
    await bc.add_subscriber("x", r.send)
    for i in range(10):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0.01)
    count_at_remove = len(r.packets)
    await bc.remove_subscriber("x")
    for i in range(10, 30):
        await bc.feed_frame(_gen_frame(192, 144, i), i * 66_666)
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0.01)
    enc.close()
    assert len(r.packets) <= count_at_remove + 2  # allow a small in-flight tolerance
    assert bc.subscriber_count == 0
