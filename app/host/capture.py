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


def _parse_output_info(text: str) -> list[tuple[int, int, int, int]]:
    """Parse `dxcam.output_info()` text into (device_idx, output_idx, w, h)
    tuples. Format from dxcam:
        Device[0] Output[0]: Res:(2560, 1440) Rot:0 Primary:True
        Device[1] Output[0]: Res:(3840, 2160) Rot:0 Primary:False
    Returns [] on unrecognized format. We pull width/height straight from
    the text so we don't have to call dxcam.create() at enumeration time -
    that call hangs on some adapters (single-monitor laptops have been
    observed to freeze in cam.release()).
    """
    import re
    out: list[tuple[int, int, int, int]] = []
    pat = re.compile(r"Device\[(\d+)\]\s+Output\[(\d+)\]\s*:\s*Res:\((\d+)\s*,\s*(\d+)\)")
    for m in pat.finditer(text):
        try:
            out.append((int(m.group(1)), int(m.group(2)),
                        int(m.group(3)), int(m.group(4))))
        except ValueError:
            continue
    return out


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
        """Enumerate displays from `dxcam.output_info()` text only - no
        dxcam.create()/cam.release() calls here, because those have been
        observed to hang on certain adapters (single-monitor laptops in
        particular, where cam.release() freezes the process).

        The actual DXGI cam is created later in the capture worker thread,
        where a hang only kills the worker, not the whole startup.
        """
        self._monitors = []
        self._monitor_pairs: list[tuple[int, int]] = []
        if not _DXCAM_AVAILABLE:
            logger.warning("dxcam not available; using stub 1920x1080 monitor")
            self._monitors = [MonitorInfo(0, "Primary (stub)", 1920, 1080)]
            self._monitor_pairs = [(0, 0)]
            return

        try:
            output_info_text = _call_with_timeout(dxcam.output_info, timeout_s=5.0)
        except Exception as exc:
            logger.warning("dxcam.output_info failed/hung: %s; using fallback monitor", exc)
            self._monitors = [MonitorInfo(0, "Primary", 1920, 1080)]
            self._monitor_pairs = [(0, 0)]
            return
        logger.info("dxcam.output_info raw:\n%s", str(output_info_text)[:1000])

        parsed = _parse_output_info(str(output_info_text))
        if not parsed:
            logger.warning("could not parse output_info; using fallback (0,0) at 1920x1080")
            self._monitors = [MonitorInfo(0, "Primary", 1920, 1080)]
            self._monitor_pairs = [(0, 0)]
            return

        for idx, (dev, out, w, h) in enumerate(parsed):
            self._monitors.append(MonitorInfo(idx, f"Display {idx + 1}", w, h))
            self._monitor_pairs.append((dev, out))
            logger.info("monitor %d: device=%d output=%d %dx%d (from output_info)",
                        idx, dev, out, w, h)

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
        """Use the (device_idx, output_idx) pairs we already discovered during
        enumeration (no more blind probing of indices that may hang).
        """
        pairs = list(getattr(self, "_monitor_pairs", []))
        if not pairs:
            pairs = [(0, 0)]

        # Move the requested monitor_index to the front
        if 0 <= self.monitor_index < len(pairs):
            pairs = [pairs[self.monitor_index]] + [
                p for i, p in enumerate(pairs) if i != self.monitor_index
            ]

        last_exc: Exception | None = None
        for dev, out in pairs:
            try:
                cam = _call_with_timeout(
                    lambda d=dev, o=out: dxcam.create(
                        device_idx=d, output_idx=o, output_color="BGR"
                    ),
                    timeout_s=5.0,
                )
                if cam is not None:
                    logger.info("capture worker: dxcam.create OK (device=%d output=%d)", dev, out)
                    return cam
            except _TimeoutErr:
                logger.warning("capture worker: dxcam.create(device=%d output=%d) timed out 5s", dev, out)
                continue
            except Exception as exc:
                last_exc = exc
                logger.warning("capture worker: dxcam.create(device=%d output=%d) failed: %s",
                               dev, out, exc)
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
