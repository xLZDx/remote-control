"""
Encoder + decoder roundtrip tests. Uses synthesized BGR frames; no screen
capture required, so this runs cleanly in any environment with PyAV.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("av")

from app.host.encoder import H264Encoder
from app.client.decoder import H264Decoder


def _gen_frame(w: int, h: int, t: int) -> np.ndarray:
    """Cheap BGR test pattern that varies with t (so encoder can't optimize away)."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = (np.arange(w) + t) % 256       # B
    arr[:, :, 1] = (np.arange(h)[:, None] + t * 2) % 256  # G
    arr[:, :, 2] = ((np.arange(w) + np.arange(h)[:, None] + t * 3) % 256)  # R
    return arr


def test_encode_yields_packets() -> None:
    enc = H264Encoder(width=320, height=240, fps=30, bitrate_kbps=500, gop=10)
    total = 0
    for i in range(15):
        for _pkt in enc.encode(_gen_frame(320, 240, i), timestamp_us=i * 33_333):
            total += 1
    for _pkt in enc.flush():
        total += 1
    enc.close()
    assert total > 0


def test_first_packet_is_keyframe() -> None:
    enc = H264Encoder(width=160, height=120, fps=15, bitrate_kbps=200, gop=15)
    pkts: list = []
    for i in range(5):
        for pkt in enc.encode(_gen_frame(160, 120, i), timestamp_us=i * 66_666):
            pkts.append(pkt)
    pkts.extend(enc.flush())
    enc.close()
    assert pkts, "no packets emitted"
    assert pkts[0].keyframe is True


def test_request_keyframe_forces_idr() -> None:
    enc = H264Encoder(width=160, height=120, fps=15, bitrate_kbps=200, gop=120)
    # consume first keyframe
    saw_first_kf = False
    for i in range(20):
        for pkt in enc.encode(_gen_frame(160, 120, i), timestamp_us=i * 66_666):
            if pkt.keyframe and not saw_first_kf:
                saw_first_kf = True
    # now request another keyframe
    enc.request_keyframe()
    forced_kf_seen = False
    for i in range(20, 40):
        for pkt in enc.encode(_gen_frame(160, 120, i), timestamp_us=i * 66_666):
            if pkt.keyframe:
                forced_kf_seen = True
                break
        if forced_kf_seen:
            break
    enc.close()
    assert saw_first_kf
    assert forced_kf_seen


def test_decoder_roundtrip_dimensions() -> None:
    enc = H264Encoder(width=192, height=144, fps=30, bitrate_kbps=400, gop=15)
    dec = H264Decoder()
    decoded = 0
    for i in range(30):
        for pkt in enc.encode(_gen_frame(192, 144, i), timestamp_us=i * 33_333):
            for df in dec.decode(pkt.data, pkt.timestamp_us):
                assert df.width == 192
                assert df.height == 144
                assert df.rgb.shape == (144, 192, 3)
                decoded += 1
    for pkt in enc.flush():
        for df in dec.decode(pkt.data, pkt.timestamp_us):
            decoded += 1
    enc.close()
    dec.close()
    assert decoded >= 15  # tolerate codec startup latency
