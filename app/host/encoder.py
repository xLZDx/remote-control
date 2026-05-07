"""
H.264 encoder using PyAV. Wraps libx264 (or hardware encoder if FFmpeg
build provides one) with low-latency settings suited to remote desktop.

Output is one or more H.264 NAL units per input frame, suitable for shipping
straight over the wire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

import av  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class EncodedPacket:
    timestamp_us: int
    keyframe: bool
    data: bytes


# Resolve PictureType enum (location depends on PyAV version)
try:
    _PICT_I = av.video.frame.PictureType.I
    _PICT_NONE = av.video.frame.PictureType.NONE
except AttributeError:  # pragma: no cover
    from av.video.frame import PictureType as _PT
    _PICT_I = _PT.I
    _PICT_NONE = _PT.NONE


_TIME_BASE_US = Fraction(1, 1_000_000)


class H264Encoder:
    """
    Wraps a PyAV CodecContext for libx264. Frame in -> H.264 packets out.

    Forces a keyframe on `request_keyframe()` (e.g. when a new viewer joins).
    """

    def __init__(
        self,
        width: int,
        height: int,
        fps: int = 30,
        bitrate_kbps: int = 4000,
        gop: int = 60,
        preset: str = "ultrafast",
        tune: str = "zerolatency",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self.gop = gop

        self._ctx = av.CodecContext.create("libx264", "w")
        self._ctx.width = width
        self._ctx.height = height
        self._ctx.pix_fmt = "yuv420p"
        self._ctx.time_base = _TIME_BASE_US
        self._ctx.framerate = Fraction(fps, 1)
        self._ctx.bit_rate = bitrate_kbps * 1000
        self._ctx.gop_size = gop
        self._ctx.options = {
            "preset": preset,
            "tune": tune,
            "profile": "main",
            "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0",
        }
        self._ctx.open()
        self._frame_index = 0
        self._force_keyframe = True   # first frame is keyframe anyway, but be explicit

    def request_keyframe(self) -> None:
        self._force_keyframe = True

    def update_bitrate(self, kbps: int) -> None:
        kbps = max(200, kbps)
        self.bitrate_kbps = kbps
        # PyAV doesn't expose mid-stream bitrate change directly. We expose this
        # for callers that re-create the encoder; current ctx keeps its rate.
        # (full mid-stream bitrate change is a Phase-5 enhancement.)
        logger.debug("encoder bitrate update queued: %d kbps", kbps)

    def encode(self, bgr_frame, timestamp_us: int) -> Iterable[EncodedPacket]:
        """Encode one BGR ndarray. Yields zero or more EncodedPacket."""
        try:
            frame = av.VideoFrame.from_ndarray(bgr_frame, format="bgr24")
        except Exception:
            logger.exception("from_ndarray failed (shape mismatch?)")
            return
        frame.pts = timestamp_us
        frame.time_base = _TIME_BASE_US
        if self._force_keyframe:
            frame.pict_type = _PICT_I
            self._force_keyframe = False
        else:
            frame.pict_type = _PICT_NONE
        self._frame_index += 1

        for packet in self._ctx.encode(frame):
            yield EncodedPacket(
                timestamp_us=int(packet.pts) if packet.pts is not None else timestamp_us,
                keyframe=bool(packet.is_keyframe),
                data=bytes(packet),
            )

    def flush(self) -> Iterable[EncodedPacket]:
        for packet in self._ctx.encode(None):
            yield EncodedPacket(
                timestamp_us=int(packet.pts) if packet.pts is not None else 0,
                keyframe=bool(packet.is_keyframe),
                data=bytes(packet),
            )

    def close(self) -> None:
        try:
            list(self.flush())
        except Exception:
            pass
        try:
            self._ctx.close()
        except Exception:
            pass


def get_extradata(encoder: H264Encoder) -> bytes:
    """Return the SPS/PPS extradata (avcC or annex-b) used by some decoders."""
    try:
        return bytes(encoder._ctx.extradata or b"")
    except Exception:
        return b""
