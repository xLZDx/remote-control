"""
Screen capture using dxcam (DXGI Desktop Duplication).

Runs the (sync) dxcam loop in a worker thread and pushes BGR ndarray frames
into an asyncio Queue. The encoder consumes that queue.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import dxcam  # type: ignore
    _DXCAM_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - import-time
    dxcam = None  # type: ignore[assignment]
    _DXCAM_AVAILABLE = False
    logger.warning("dxcam unavailable: %s (capture will be disabled)", _exc)


@dataclass
class MonitorInfo:
    index: int
    name: str
    width: int
    height: int


@dataclass
class Frame:
    """Raw captured frame. `data` is BGR uint8 ndarray, shape (h, w, 3)."""
    timestamp_us: int
    monitor_index: int
    width: int
    height: int
    data: object  # numpy.ndarray


class ScreenCapture:
    """
    Pulls frames from dxcam at a target FPS. Multiple monitor support.

    Usage:
        cap = ScreenCapture(target_fps=30, monitor_index=0)
        await cap.start()
        async for frame in cap.frames():
            ...
        await cap.stop()
    """

    def __init__(
        self,
        target_fps: int = 30,
        monitor_index: int = 0,
        queue_max: int = 4,
    ) -> None:
        self.target_fps = target_fps
        self.monitor_index = monitor_index
        self._queue: asyncio.Queue[Optional[Frame]] = asyncio.Queue(maxsize=queue_max)
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cam = None
        self._monitors: list[MonitorInfo] = []
        self._enumerate_monitors()

    # ---------- monitor enumeration ----------

    def _enumerate_monitors(self) -> None:
        self._monitors = []
        if not _DXCAM_AVAILABLE:
            self._monitors = [MonitorInfo(0, "Primary (stub)", 1920, 1080)]
            return
        try:
            output_info = dxcam.output_info()  # multi-line text describing each output
        except Exception as exc:  # pragma: no cover
            logger.warning("dxcam.output_info failed: %s", exc)
            self._monitors = [MonitorInfo(0, "Primary", 0, 0)]
            return
        # output_info returns a printable string; we just enumerate by trying camera creation
        idx = 0
        while True:
            try:
                cam = dxcam.create(output_idx=idx)
            except Exception:
                break
            if cam is None:
                break
            try:
                w, h = cam.width, cam.height
                self._monitors.append(MonitorInfo(idx, f"Display {idx + 1}", w, h))
            finally:
                try:
                    cam.release()
                except Exception:
                    pass
            idx += 1
            if idx > 8:  # sanity cap
                break
        if not self._monitors:
            self._monitors = [MonitorInfo(0, "Primary", 0, 0)]

    @property
    def monitors(self) -> list[MonitorInfo]:
        return list(self._monitors)

    def monitors_as_dicts(self) -> list[dict]:
        return [
            {"index": m.index, "name": m.name, "width": m.width, "height": m.height}
            for m in self._monitors
        ]

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="dxcam-capture", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop_evt.set()
        # wake any consumer
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue, None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._loop = None

    async def set_monitor(self, index: int) -> bool:
        if index >= len(self._monitors):
            return False
        if index == self.monitor_index:
            return True
        self.monitor_index = index
        # restart capture loop with new monitor
        await self.stop()
        await self.start()
        return True

    # ---------- frame iteration ----------

    async def frames(self):
        """Async generator of captured Frames until stopped."""
        while not self._stop_evt.is_set():
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    # ---------- worker thread ----------

    def _run(self) -> None:
        if not _DXCAM_AVAILABLE:
            logger.error("dxcam not available; capture thread exiting")
            return
        try:
            self._cam = dxcam.create(output_idx=self.monitor_index, output_color="BGR")
            if self._cam is None:
                logger.error("dxcam.create returned None for monitor %d", self.monitor_index)
                return
            self._cam.start(target_fps=self.target_fps, video_mode=True)
            interval = 1.0 / max(self.target_fps, 1)
            next_t = time.monotonic()
            while not self._stop_evt.is_set():
                arr = self._cam.get_latest_frame()
                if arr is None:
                    time.sleep(interval / 2)
                    continue
                ts = int(time.monotonic() * 1_000_000)
                h, w = arr.shape[:2]
                frame = Frame(
                    timestamp_us=ts, monitor_index=self.monitor_index,
                    width=w, height=h, data=arr,
                )
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._enqueue, frame)
                # pace
                next_t += interval
                sleep = next_t - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    next_t = time.monotonic()
        except Exception:
            logger.exception("capture thread error")
        finally:
            try:
                if self._cam is not None:
                    self._cam.stop()
                    self._cam.release()
            except Exception:
                pass
            self._cam = None

    def _enqueue(self, frame: Optional[Frame]) -> None:
        # Drop oldest if queue full (latency over completeness)
        if frame is None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
