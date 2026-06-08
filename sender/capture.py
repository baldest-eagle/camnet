"""
sender/capture.py — Camera capture module for CamNet Sender.

Handles OpenCV and GStreamer backends with 1080p60 target, BGRA output.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import cv2
import numpy as np
from loguru import logger


@dataclass
class CaptureConfig:
    device_index: int = 0
    width: int = 1920
    height: int = 1080
    fps: int = 60
    pixel_format: str = "BGRA"        # Output format
    use_gstreamer: bool = False        # Prefer GStreamer pipeline if True
    backend: int = cv2.CAP_ANY        # OpenCV backend override


@dataclass
class FrameStats:
    frames_captured: int = 0
    frames_dropped: int = 0
    actual_fps: float = 0.0
    last_frame_ts: float = field(default_factory=time.time)
    _fps_window: list[float] = field(default_factory=list, repr=False)

    def record(self) -> None:
        now = time.time()
        self._fps_window.append(now)
        # Keep only 2-second window
        cutoff = now - 2.0
        self._fps_window = [t for t in self._fps_window if t > cutoff]
        self.actual_fps = len(self._fps_window) / 2.0
        self.last_frame_ts = now
        self.frames_captured += 1


class CameraCapture:
    """
    Captures frames from a local camera device.

    Supports OpenCV (CAP_DSHOW on Windows for low-latency) and a
    GStreamer pipeline fallback for hardware-accelerated capture.

    Usage:
        cap = CameraCapture(CaptureConfig(device_index=0, fps=60))
        with cap:
            while True:
                frame = cap.read_frame()  # raw BGRA bytes or None
    """

    def __init__(self, config: CaptureConfig | None = None) -> None:
        self.config = config or CaptureConfig()
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()
        self._running = False
        self._latest_frame: Optional[np.ndarray] = None
        self._capture_thread: Optional[threading.Thread] = None
        self.stats = FrameStats()
        self._on_frame_callbacks: list[Callable[[bytes], None]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open(self) -> bool:
        """Open the camera. Returns True on success."""
        cap = self._try_open()
        if cap is None:
            logger.error(
                "Failed to open camera device {}.", self.config.device_index
            )
            return False
        self._cap = cap
        self._configure_capture()
        logger.info(
            "Camera opened: device={} backend={} target={}x{}@{}fps",
            self.config.device_index,
            self._cap.getBackendName(),
            self.config.width,
            self.config.height,
            self.config.fps,
        )
        return True

    def start(self) -> None:
        """Start the background capture thread."""
        if not self._cap:
            raise RuntimeError("Camera not opened. Call open() first.")
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="CamNetCapture",
            daemon=True,
        )
        self._capture_thread.start()
        logger.info("Capture thread started.")

    def stop(self) -> None:
        """Stop the capture thread and release the camera."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3.0)
        self._release()
        logger.info("Capture stopped. Stats: {}", self.stats)

    def read_frame(self) -> Optional[bytes]:
        """
        Returns the latest captured frame as raw BGRA bytes, or None if
        no frame is available yet.
        """
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.tobytes()

    def on_frame(self, callback: Callable[[bytes], None]) -> None:
        """Register a callback to be invoked for every new frame (BGRA bytes)."""
        self._on_frame_callbacks.append(callback)

    @property
    def frame_size(self) -> int:
        """Expected size in bytes of one raw BGRA frame."""
        return self.config.width * self.config.height * 4

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "CameraCapture":
        self.open()
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_open(self) -> Optional[cv2.VideoCapture]:
        """Try DirectShow first (Windows), then any backend."""
        backends_to_try: list[int] = []

        if self.config.use_gstreamer:
            # GStreamer pipeline for hardware-accelerated capture
            pipeline = self._build_gstreamer_pipeline()
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                logger.info("Using GStreamer pipeline: {}", pipeline)
                return cap
            logger.warning("GStreamer pipeline failed, falling back to OpenCV.")

        # Windows: DirectShow gives sub-frame latency via DSHOW backend
        import sys
        if sys.platform == "win32":
            backends_to_try = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        else:
            backends_to_try = [cv2.CAP_V4L2, cv2.CAP_ANY]

        if self.config.backend != cv2.CAP_ANY:
            backends_to_try = [self.config.backend] + backends_to_try

        for backend in backends_to_try:
            cap = cv2.VideoCapture(self.config.device_index, backend)
            if cap.isOpened():
                logger.debug("Opened camera with backend: {}", backend)
                return cap

        return None

    def _configure_capture(self) -> None:
        """Set resolution, FPS, and buffer size on the VideoCapture object."""
        c = self._cap
        c.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        c.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        c.set(cv2.CAP_PROP_FPS, self.config.fps)
        # Minimize internal buffer to reduce latency
        c.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(c.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(c.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = c.get(cv2.CAP_PROP_FPS)

        if actual_fps < self.config.fps - 5:
            logger.warning(
                "Camera reports only {:.0f}fps (requested {}). Falling back.",
                actual_fps,
                self.config.fps,
            )
            self.config.fps = int(actual_fps) or 30

        if actual_w != self.config.width or actual_h != self.config.height:
            logger.warning(
                "Camera resolution mismatch: got {}x{}, requested {}x{}",
                actual_w, actual_h,
                self.config.width, self.config.height,
            )
            self.config.width = actual_w
            self.config.height = actual_h

    def _build_gstreamer_pipeline(self) -> str:
        """Build a GStreamer pipeline string for hardware-accelerated capture."""
        cfg = self.config
        return (
            f"ksvideosrc device-index={cfg.device_index} ! "
            f"video/x-raw,width={cfg.width},height={cfg.height},"
            f"framerate={cfg.fps}/1 ! "
            f"videoconvert ! "
            f"video/x-raw,format=BGRA ! "
            f"appsink drop=true max-buffers=1"
        )

    def _capture_loop(self) -> None:
        """Background thread: continuously reads frames from the camera."""
        cap = self._cap
        interval = 1.0 / self.config.fps
        frame_deadline = time.perf_counter()

        while self._running:
            ret, frame = cap.read()  # BGR uint8 ndarray

            if not ret:
                logger.warning("Camera read failed, retrying...")
                self.stats.frames_dropped += 1
                time.sleep(0.01)
                continue

            # Convert BGR → BGRA (add alpha channel)
            bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)

            # Resize if camera returned unexpected dimensions
            if bgra.shape[1] != self.config.width or bgra.shape[0] != self.config.height:
                bgra = cv2.resize(
                    bgra,
                    (self.config.width, self.config.height),
                    interpolation=cv2.INTER_LINEAR,
                )

            with self._lock:
                self._latest_frame = bgra

            self.stats.record()

            # Invoke registered callbacks
            if self._on_frame_callbacks:
                raw = bgra.tobytes()
                for cb in self._on_frame_callbacks:
                    try:
                        cb(raw)
                    except Exception as exc:
                        logger.error("Frame callback error: {}", exc)

            # Pace to target FPS (drift-corrected)
            frame_deadline += interval
            sleep_time = frame_deadline - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # We're behind; reset deadline to avoid spiral
                frame_deadline = time.perf_counter()

    def _release(self) -> None:
        """Release the VideoCapture resource."""
        if self._cap:
            self._cap.release()
            self._cap = None


# ---------------------------------------------------------------------------
# Quick test / demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logger.info("Opening camera device 0, target 1080p @ 60fps ...")
    config = CaptureConfig(device_index=0, width=1920, height=1080, fps=60)
    cap = CameraCapture(config)

    if not cap.open():
        logger.error("Could not open camera.")
        sys.exit(1)

    cap.start()
    try:
        for _ in range(300):  # ~5 seconds at 60fps
            frame = cap.read_frame()
            if frame:
                logger.debug(
                    "Frame: {} bytes | FPS: {:.1f}",
                    len(frame),
                    cap.stats.actual_fps,
                )
            time.sleep(1.0 / 60)
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop()
