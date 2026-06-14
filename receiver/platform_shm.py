"""
CamNet – Platform-Abstracted Shared Memory Writer
receiver/platform_shm.py

Provides a unified interface for writing decoded frames into shared
memory regardless of the host platform:

  - **Windows**: Delegates to ``shm_writer.ShmFrameWriter`` (Win32 Named
    File Mapping + Named Mutex) for DirectShow filter consumption.
  - **Linux**: Delegates to ``v4l2_output.V4L2LoopbackWriter`` (V4L2
    loopback device + POSIX SHM mirror) for V4L2 consumer access.

The ``create_frame_writer()`` factory inspects ``sys.platform`` and
returns the appropriate implementation, so the rest of the receiver
codebase stays platform-agnostic.

Usage::

    from receiver.platform_shm import create_frame_writer

    writer = create_frame_writer(width=1920, height=1080, fps=60)
    writer.open()
    writer.write_frame(bgra_bytes, audio_bytes)
    writer.close()
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol (interface) that all platform writers must satisfy
# ---------------------------------------------------------------------------

@runtime_checkable
class FrameWriter(Protocol):
    """Minimal interface shared by ShmFrameWriter (Win) and V4L2LoopbackWriter (Linux)."""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write_frame(self, bgra_bytes: bytes, audio_bytes: bytes = b"") -> bool: ...

    @property
    def frame_index(self) -> int: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_frame_writer(
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    has_audio: bool = True,
    device: str = "",  # Linux: V4L2 device path; ignored on Windows
    enable_shm: bool = True,  # Linux: also mirror to POSIX SHM
) -> FrameWriter:
    """
    Create and return the platform-appropriate frame writer.

    Parameters
    ----------
    width, height, fps :
        Video frame dimensions and target frame rate.
    has_audio :
        Whether an audio stream accompanies the video.
    device :
        (Linux only) Path to the V4L2 loopback device, e.g.
        ``"/dev/video2"``.  If empty, the writer will attempt
        auto-detection via ``find_v4l2_loopback_device()``.
    enable_shm :
        (Linux only) Also write frames to a POSIX shared-memory
        segment so an external consumer can memory-map the data.

    Returns
    -------
    FrameWriter
        Either a :class:`ShmFrameWriter` (Windows) or a
        :class:`V4L2LoopbackWriter` (Linux).

    Raises
    ------
    RuntimeError
        If the current platform is not supported.
    """
    if sys.platform == "win32":
        from receiver.shm_writer import ShmFrameWriter
        return ShmFrameWriter(
            width=width,
            height=height,
            fps=fps,
            has_audio=has_audio,
        )

    elif sys.platform == "linux":
        from receiver.v4l2_output import V4L2LoopbackWriter, find_v4l2_loopback_device

        # Auto-detect V4L2 device if not specified
        v4l2_device = device
        if not v4l2_device:
            detected = find_v4l2_loopback_device()
            if detected:
                v4l2_device = detected
            else:
                v4l2_device = "/dev/video2"
                import warnings
                warnings.warn(
                    f"No CamNet V4L2 loopback device auto-detected. "
                    f"Falling back to {v4l2_device}. "
                    f"Load v4l2loopback with: "
                    f"sudo modprobe v4l2loopback video_nr=2 "
                    f"card_label='CamNet Virtual Camera' exclusive_caps=1",
                    RuntimeWarning,
                    stacklevel=2,
                )

        return V4L2LoopbackWriter(
            device=v4l2_device,
            width=width,
            height=height,
            fps=fps,
            has_audio=has_audio,
            enable_shm=enable_shm,
        )

    else:
        raise RuntimeError(
            f"Unsupported platform: {sys.platform}. "
            f"CamNet Receiver requires Windows or Linux."
        )


# ---------------------------------------------------------------------------
# Convenience: platform-aware SHM name accessor
# ---------------------------------------------------------------------------

def get_shm_name() -> str:
    """Return the platform-appropriate shared memory name.

    - Windows: ``Global\\CamNetFrame``
    - Linux:   ``/CamNetFrame``  (POSIX shm name)
    """
    if sys.platform == "win32":
        return "Global\\CamNetFrame"
    return "/CamNetFrame"


def get_mutex_name() -> str:
    """Return the platform-appropriate mutex / lock name.

    - Windows: ``Global\\CamNetMutex``
    - Linux:   ``/tmp/camnet_shm.lock``
    """
    if sys.platform == "win32":
        return "Global\\CamNetMutex"
    return "/tmp/camnet_shm.lock"
