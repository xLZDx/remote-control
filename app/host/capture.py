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


class _TimeoutErr(Exception):
    pass


def _call_with_timeout(fn, timeout_s: float):
    """Run `fn` in a thread with a hard timeout. Raises _TimeoutErr on timeout.
    Used to defang dxcam calls that may hang indefinitely on some adapters."""
    result: list = [None]
    error: list = [None]

    def _worker() -> None:
        try:
            result[0] = fn()
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_worker, name="dxcam-probe", daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise _TimeoutErr(f"call did not return in {timeout_s}s")
    if error[0] is not None:
        raise error[0]
    return result[0]


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
        logger.info("ScreenCapture.__init__: enumerating monitors (target_fps=%d, monitor_index=%d)",
                    target_fps, monitor_index)
        self._enumerate_monitors()
        logger.info("ScreenCapture.__init__: enumeration done, %d monitor(s) detected", len(self._monitors))

    # ---------- monitor enumeration ----------

    def _enumerate_monitors(self) -> None:
        """Enumerate displays. dxcam.create() can hang on some Optimus laptops;
        each probe is wrapped with a 5-second timeout so a single bad adapter
        doesn't block startup forever."""
        self._monitors = []
        if not _DXCAM_AVAILABLE:
            logger.warning("dxcam not available; using stub 1920x1080 monitor")
            self._monitors = [MonitorInfo(0, "Primary (stub)", 1920, 1080)]
            return
        try:
            output_info = _call_with_timeout(dxcam.output_info, timeout_s=5.0)
            logger.debug("dxcam.output_info ok:\n%s", str(output_info)[:500])
        except Exception as exc:
            logger.warning("dxcam.output_info failed/hung: %s; falling back to single Primary stub", exc)
            self._monitors = [MonitorInfo(0, "Primary", 0, 0)]
            return
        idx = 0
        while True:
            logger.debug("probing dxcam output_idx=%d", idx)
            cam = None
            try:
                cam = _call_with_timeout(lambda: dxcam.create(output_idx=idx), timeout_s=5.0)
            except _TimeoutErr:
                logger.warning("dxcam.create(output_idx=%d) timed out after 5s; skipping", idx)
                break
            except Exception as exc:
                logger.debug("dxcam.create(output_idx=%d) failed: %s", idx, exc)
                break
            if cam is None:
                break
            try:
                w, h = cam.width, cam.height
                self._monitors.append(MonitorInfo(idx, f"Display {idx + 1}", w, h))
                logger.info("monitor %d: Display %d %dx%d", idx, idx + 1, w, h)
            finally:
                try:
                    cam.release()
                except Exception:
                    pass
            idx += 1
            if idx > 8:
                break
        if not self._monitors:
            logger.warning("no usable monitors enumerated; using Primary stub")
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
            logger.info("capture worker: creating dxcam (with GPU fallback grid, 5s/probe timeout)")
            self._cam = self._create_camera_with_fallback()
            if self._cam is None:
                logger.error("dxcam.create failed for all GPUs / outputs")
                return
            logger.info("capture worker: dxcam created (output_idx=%d), starting acquisition",
                        self.monitor_index)
            self._cam.start(target_fps=self.target_fps, video_mode=True)
            logger.info("capture worker: dxcam.start ok (target_fps=%d)", self.target_fps)
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

    def _create_camera_with_fallback(self):
        """Try (device_idx, output_idx) combinations until one succeeds.

        On laptops with hybrid graphics (NVIDIA Optimus / AMD Switchable),
        the primary display might be attached to a different DXGI adapter
        than dxcam's default. We brute-force a small grid.
        """
        # First the user-requested output on the default device (matches old behavior)
        attempts = [(None, self.monitor_index)]
        # Then enumerate small grid
        for d in range(0, 4):
            for o in range(0, 4):
                if (d, o) not in attempts and (None, o) not in attempts:
                    attempts.append((d, o))

        last_exc: Exception | None = None
        for device_idx, output_idx in attempts:
            try:
                if device_idx is None:
                    cam = dxcam.create(output_idx=output_idx, output_color="BGR")
                else:
                    cam = dxcam.create(device_idx=device_idx, output_idx=output_idx, output_color="BGR")
                if cam is not None:
                    if device_idx is not None or output_idx != self.monitor_index:
                        logger.warning(
                            "dxcam fell back to device_idx=%s output_idx=%d "
                            "(requested output_idx=%d)",
                            device_idx, output_idx, self.monitor_index,
                        )
                    self.monitor_index = output_idx
                    return cam
            except Exception as exc:
                last_exc = exc
                logger.debug("dxcam.create(device_idx=%s, output_idx=%d) failed: %s",
                             device_idx, output_idx, exc)
                continue
        if last_exc is not None:
            logger.error("dxcam.create exhausted all attempts; last error: %s", last_exc)
        return None

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
