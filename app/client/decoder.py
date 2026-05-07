"""
H.264 decoder using PyAV. Consumes packets from the wire, yields decoded
RGB ndarray frames suitable for QImage construction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

import av  # type: ignore

logger = logging.getLogger(__name__)

_TIME_BASE_US = Fraction(1, 1_000_000)


@dataclass
class DecodedFrame:
    timestamp_us: int
    width: int
    height: int
    rgb: object  # numpy.ndarray, shape (h,w,3), uint8


class H264Decoder:
    def __init__(self) -> None:
        self._ctx = av.CodecContext.create("h264", "r")

    def decode(self, packet_data: bytes, pts_us: int) -> Iterable[DecodedFrame]:
        try:
            packet = av.Packet(packet_data)
            packet.pts = pts_us
            packet.time_base = _TIME_BASE_US
            for frame in self._ctx.decode(packet):
                arr = frame.to_ndarray(format="rgb24")
                yield DecodedFrame(
                    timestamp_us=int(frame.pts) if frame.pts is not None else pts_us,
                    width=frame.width,
                    height=frame.height,
                    rgb=arr,
                )
        except av.error.InvalidDataError:
            logger.debug("invalid h264 packet (probably waiting for keyframe)")
        except Exception:
            logger.exception("decode error")

    def close(self) -> None:
        try:
            self._ctx.close()
        except Exception:
            pass
