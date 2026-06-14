"""
CamNet – V4L2 Loopback Output (Linux)
receiver/v4l2_output.py

Writes decoded BGRA frames into a V4L2 loopback device so any Linux
application (OBS Studio, Chrome, VLC, etc.) can open it as a regular
video capture device — the Linux equivalent of the Windows DirectShow
virtual camera.

Requires:
  - v4l2loopback kernel module loaded (see linux_setup.sh)
  - Python v4l2 package (pyv4l2 or via ctypes fallback)

Shared memory layout (POSIX shm for IPC, same as Windows header)
--------------------
Offset  0 : uint32  magic          = 0xCAFECAFE
Offset  4 : uint32  width
Offset  8 : uint32  height
Offset 12 : uint32  fps
Offset 16 : uint64  frame_index    (monotonically increasing, 0-based)
Offset 24 : uint64  timestamp_ms   (ms since Unix epoch)
Offset 32 : uint32  audio_chunk_size  (bytes of audio that follow pixel data)
Offset 36 : uint32  flags          (bit 0 = has_audio)
Offset 40 : <width * height * 4>   raw BGRA pixels
Offset 40 + pixels: <audio_chunk_size>  PCM s16le stereo 48 kHz
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import mmap
import os
import struct
import sys
import time
from pathlib import Path
from typing import Optional

from loguru import logger

if sys.platform != "linux":
    raise ImportError("v4l2_output requires Linux.")

# ---------------------------------------------------------------------------
# V4L2 ioctl constants (from linux/videodev2.h)
# ---------------------------------------------------------------------------

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_S_FMT = 0xC0CC561D  # struct v4l2_format
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
VIDIOC_G_FMT = 0xC0CC5604

V4L2_BUF_TYPE_VIDEO_OUTPUT = 0x02
V4L2_FIELD_NONE = 0x01
V4L2_PIX_FMT_BGR32 = 0x32424752  # 'BGR2' — matches BGRA byte order
V4L2_CAP_VIDEO_OUTPUT = 0x00000002
V4L2_MEMORY_MMAP = 0x01

# ---------------------------------------------------------------------------
# V4L2 structures (ctypes)
# ---------------------------------------------------------------------------


class _v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class _v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class _v4l2_format_union(ctypes.Union):
    _fields_ = [("pix", _v4l2_pix_format), ("raw", ctypes.c_byte * 200)]


class _v4l2_format(ctypes.Structure):
    _anonymous_ = ("fmt",)
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("fmt", _v4l2_format_union),
    ]


class _v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class _v4l2_buffer(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", ctypes.c_ulonglong),
        ("timecode", ctypes.c_byte * 16),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("offset", ctypes.c_ulonglong),  # union: offset or userptr or planes
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# POSIX Shared Memory writer (mirrors Windows shm_writer layout)
# ---------------------------------------------------------------------------

MAGIC = 0xCAFECAFE
SHM_NAME = "/CamNetFrame"
LOCK_FILE = "/tmp/camnet_shm.lock"
AUDIO_BUF_SIZE = 192_000  # 1 s of 48 kHz stereo s16le

_HEADER_FMT = "<IIIIQQII"
_HEADER_STRUCT = struct.Struct(_HEADER_FMT)
_HEADER_SIZE = _HEADER_STRUCT.size  # 40 bytes


def _build_header(
    width: int,
    height: int,
    fps: int,
    frame_index: int,
    timestamp_ms: int,
    audio_chunk_size: int,
    flags: int,
) -> bytes:
    return _HEADER_STRUCT.pack(
        MAGIC, width, height, fps, frame_index, timestamp_ms,
        audio_chunk_size, flags,
    )


# ---------------------------------------------------------------------------
# V4L2LoopbackWriter
# ---------------------------------------------------------------------------


class V4L2LoopbackWriter:
    """
    Owns a V4L2 loopback device file descriptor and optionally a POSIX
    shared memory segment for IPC (so a companion C/Rust reader can also
    access frames).

    The primary output path writes BGRA frames directly to the V4L2 device
    via write() — the simplest and most reliable approach for v4l2loopback
    in output mode.

    The POSIX SHM mirror is maintained for any external consumer that
    prefers the memory-mapped approach (e.g., a custom V4L2 provider
    process).

    Usage::

        writer = V4L2LoopbackWriter(device="/dev/video2", width=1920, height=1080, fps=60)
        writer.open()
        writer.write_frame(bgra_bytes, audio_bytes)
        writer.close()
    """

    def __init__(
        self,
        device: str = "/dev/video2",
        width: int = 1920,
        height: int = 1080,
        fps: int = 60,
        has_audio: bool = True,
        enable_shm: bool = True,
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.has_audio = has_audio
        self.enable_shm = enable_shm

        self._pixel_bytes = width * height * 4
        self._total_shm_size = _HEADER_SIZE + self._pixel_bytes + AUDIO_BUF_SIZE

        self._fd: Optional[int] = None  # V4L2 device fd
        self._shm_fd: Optional[int] = None  # POSIX shm fd
        self._shm_map: Optional[mmap.mmap] = None  # mmap of POSIX shm
        self._lock_fd: Optional[int] = None  # lock file fd

        self._frame_index: int = 0

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "V4L2LoopbackWriter":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the V4L2 loopback device and optional POSIX shared memory."""
        # --- V4L2 device ---
        if not os.path.exists(self.device):
            raise FileNotFoundError(
                f"V4L2 device {self.device} not found. "
                "Load v4l2loopback: sudo modprobe v4l2loopback "
                "video_nr=2 card_label='CamNet Virtual Camera' "
                "exclusive_caps=1"
            )

        self._fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK)
        if self._fd < 0:
            raise OSError(f"Cannot open V4L2 device {self.device}")

        # Query capabilities
        cap = _v4l2_capability()
        fcntl.ioctl(self._fd, VIDIOC_QUERYCAP, cap)
        cap_flags = cap.capabilities
        if not (cap_flags & V4L2_CAP_VIDEO_OUTPUT):
            logger.warning(
                "V4L2 device {} does not report VIDEO_OUTPUT capability. "
                "Ensure v4l2loopback was loaded with exclusive_caps=1.",
                self.device,
            )

        # Set format
        fmt = _v4l2_format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
        fmt.pix.width = self.width
        fmt.pix.height = self.height
        fmt.pix.pixelformat = V4L2_PIX_FMT_BGR32
        fmt.pix.field = V4L2_FIELD_NONE
        fmt.pix.bytesperline = self.width * 4
        fmt.pix.sizeimage = self._pixel_bytes
        fmt.pix.colorspace = 1  # V4L2_COLORSPACE_SRGB

        try:
            fcntl.ioctl(self._fd, VIDIOC_S_FMT, fmt)
        except OSError as exc:
            logger.warning("VIDIOC_S_FMT failed (will try write() anyway): {}", exc)

        logger.info(
            "V4L2 device opened: {} ({}x{}@{}fps, pix_fmt=BGR32)",
            self.device, self.width, self.height, self.fps,
        )

        # --- POSIX shared memory (optional IPC mirror) ---
        if self.enable_shm:
            self._open_shm()

        # --- Lock file for SHM synchronization ---
        try:
            lock_dir = os.path.dirname(LOCK_FILE)
            if lock_dir and not os.path.exists(lock_dir):
                os.makedirs(lock_dir, exist_ok=True)
            self._lock_fd = os.open(
                LOCK_FILE,
                os.O_CREAT | os.O_RDWR,
                0o666,
            )
        except OSError as exc:
            logger.warning("Could not create lock file {}: {}", LOCK_FILE, exc)
            self._lock_fd = None

    def _open_shm(self) -> None:
        """Create and map a POSIX shared memory segment."""
        try:
            # Try to unlink existing segment first
            os.shm_unlink(SHM_NAME)
        except FileNotFoundError:
            pass
        except OSError:
            pass

        self._shm_fd = os.shm_open(
            SHM_NAME,
            os.O_CREAT | os.O_RDWR,
            0o666,
        )
        if self._shm_fd < 0:
            raise OSError(f"shm_open failed for {SHM_NAME}")

        os.ftruncate(self._shm_fd, self._total_shm_size)

        self._shm_map = mmap.mmap(
            self._shm_fd,
            self._total_shm_size,
            access=mmap.ACCESS_WRITE,
        )
        logger.info(
            "POSIX SHM opened: {} ({} bytes)",
            SHM_NAME, self._total_shm_size,
        )

    def close(self) -> None:
        """Release all resources."""
        # V4L2 device
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        # POSIX SHM
        if self._shm_map is not None:
            try:
                self._shm_map.close()
            except Exception:
                pass
            self._shm_map = None

        if self._shm_fd is not None:
            try:
                os.close(self._shm_fd)
            except OSError:
                pass
            self._shm_fd = None

            # Clean up SHM name
            try:
                os.shm_unlink(SHM_NAME)
            except OSError:
                pass

        # Lock file
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

        logger.info("V4L2LoopbackWriter closed.")

    # ------------------------------------------------------------------
    # Frame writing
    # ------------------------------------------------------------------

    def write_frame(
        self,
        bgra_bytes: bytes,
        audio_bytes: bytes = b"",
    ) -> bool:
        """
        Write a decoded BGRA frame to the V4L2 loopback device and
        optionally mirror it to POSIX shared memory.

        Returns True on success, False if the write would block.
        """
        if self._fd is None:
            raise RuntimeError("V4L2LoopbackWriter is not open. Call open() first.")

        if len(bgra_bytes) != self._pixel_bytes:
            raise ValueError(
                f"Expected {self._pixel_bytes} bytes of BGRA, got {len(bgra_bytes)}"
            )

        success = True

        # --- Write to V4L2 device ---
        try:
            os.write(self._fd, bgra_bytes)
        except BlockingIOError:
            # Device buffer full — skip frame
            logger.debug("V4L2 write blocked — skipping frame {}", self._frame_index)
            success = False
        except OSError as exc:
            logger.error("V4L2 write error: {}", exc)
            success = False

        # --- Mirror to POSIX SHM ---
        if self.enable_shm and self._shm_map is not None:
            self._write_shm(bgra_bytes, audio_bytes)

        if success:
            self._frame_index += 1

        return success

    def _write_shm(self, bgra_bytes: bytes, audio_bytes: bytes) -> None:
        """Write header + pixels + audio into the POSIX SHM segment."""
        # Acquire file lock (non-blocking)
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.debug("SHM lock busy — skipping SHM mirror for frame {}", self._frame_index)
                return

        try:
            ts_ms = int(time.time() * 1000)
            audio_sz = min(len(audio_bytes), AUDIO_BUF_SIZE)
            flags = 0x1 if self.has_audio and audio_sz > 0 else 0x0

            header = _build_header(
                width=self.width,
                height=self.height,
                fps=self.fps,
                frame_index=self._frame_index,
                timestamp_ms=ts_ms,
                audio_chunk_size=audio_sz,
                flags=flags,
            )

            # Write into mmap
            self._shm_map.seek(0)
            self._shm_map.write(header)
            self._shm_map.write(bgra_bytes)
            if audio_sz > 0:
                self._shm_map.write(audio_bytes[:audio_sz])
        finally:
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def shm_name(self) -> str:
        return SHM_NAME

    @property
    def lock_name(self) -> str:
        return LOCK_FILE


# ---------------------------------------------------------------------------
# Device auto-detection helper
# ---------------------------------------------------------------------------


def find_v4l2_loopback_device(label: str = "CamNet") -> Optional[str]:
    """
    Scan /sys/class/video4linux/ for a v4l2loopback device whose
    card label contains the given string.

    Returns the device path (e.g. '/dev/video2') or None.
    """
    v4l_dir = Path("/sys/class/video4linux")
    if not v4l_dir.exists():
        return None

    for entry in sorted(v4l_dir.iterdir()):
        name_file = entry / "name"
        if name_file.exists():
            try:
                card_label = name_file.read_text().strip()
            except OSError:
                continue
            if label.lower() in card_label.lower():
                dev_path = f"/dev/{entry.name}"
                if os.path.exists(dev_path):
                    logger.info(
                        "Found V4L2 loopback device: {} (label: '{}')",
                        dev_path, card_label,
                    )
                    return dev_path

    return None


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    WIDTH, HEIGHT, FPS = 1920, 1080, 60
    FRAME_BYTES = WIDTH * HEIGHT * 4

    logger.info("CamNet V4L2 output self-test: writing 100 frames…")

    # Auto-detect or use /dev/video2
    device = find_v4l2_loopback_device() or "/dev/video2"
    logger.info("Using device: {}", device)

    with V4L2LoopbackWriter(device=device, width=WIDTH, height=HEIGHT, fps=FPS) as writer:
        for i in range(100):
            # Alternating color pattern for easy visual verification
            colour = (i % 3)
            if colour == 0:
                pixel = b"\xff\x00\x00\xff"  # blue  (BGRA)
            elif colour == 1:
                pixel = b"\x00\xff\x00\xff"  # green (BGRA)
            else:
                pixel = b"\x00\x00\xff\xff"  # red   (BGRA)

            frame = pixel * (FRAME_BYTES // 4)
            audio = os.urandom(4096)

            ok = writer.write_frame(frame, audio)
            status = "OK" if ok else "SKIPPED"
            logger.info(f"Frame {i:03d}: {status}  ts={int(time.time()*1000)}")
            time.sleep(1 / FPS)

    logger.info("Self-test complete.")
