"""
CamNet – Windows Named Shared Memory Frame Writer
receiver/shm_writer.py

Writes decoded BGRA frames (+ optional PCM audio) into a Win32 Named File
Mapping so the DirectShow filter (C++) can read them with zero-copy speed.

Shared memory layout
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

Dependencies: pywin32, loguru
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from typing import Optional

from loguru import logger

if sys.platform != "win32":
    raise ImportError("shm_writer requires Windows (win32 platform).")

import pywintypes           # type: ignore[import]
import win32api
import win32file             # type: ignore[import]
import win32con             # type: ignore[import]
import win32event           # type: ignore[import]
import win32security        # type: ignore[import]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC          = 0xCAFECAFE
SHM_NAME       = "Global\\CamNetFrame"
MUTEX_NAME     = "Global\\CamNetMutex"
MUTEX_TIMEOUT_MS = 100          # skip frame if we can't acquire in 100 ms
AUDIO_BUF_SIZE = 192_000        # 1 s of 48 kHz stereo s16le = 192 000 bytes

# Struct format for the 40-byte header (little-endian)
# Header layout (must match C++ CamNetShmHeader exactly):
#   0: uint32 magic
#   4: uint32 width
#   8: uint32 height
#  12: uint32 fps
#  16: uint64 frame_index
#  24: uint64 timestamp_ms
#  32: uint32 audio_chunk_size
#  36: uint32 flags
#  40: pixels …
_HEADER_FMT    = "<IIIIQQII"
_HEADER_STRUCT = struct.Struct(_HEADER_FMT)  # 40 bytes — matches C++ #pragma pack(push,1)
_HEADER_SIZE   = _HEADER_STRUCT.size         # 40


def _build_header(
    width: int,
    height: int,
    fps: int,
    frame_index: int,
    timestamp_ms: int,
    audio_chunk_size: int,
    flags: int,
) -> bytes:
    """Pack the 40-byte header (struct fields only, no trailing pad)."""
    return _HEADER_STRUCT.pack(
        MAGIC,
        width,
        height,
        fps,
        frame_index,
        timestamp_ms,
        audio_chunk_size,
        flags,
    )


# ---------------------------------------------------------------------------
# ShmFrameWriter
# ---------------------------------------------------------------------------

class ShmFrameWriter:
    """
    Owns a Win32 Named File Mapping (``Global\\CamNetFrame``) and a Named
    Mutex (``Global\\CamNetMutex``) so that the DirectShow reader can safely
    consume frames written by this writer.

    Usage::

        with ShmFrameWriter(width=1920, height=1080, fps=60) as w:
            w.write_frame(bgra_bytes, audio_bytes)
    """

    def __init__(
        self,
        width:     int  = 1920,
        height:    int  = 1080,
        fps:       int  = 60,
        has_audio: bool = True,
    ) -> None:
        self.width     = width
        self.height    = height
        self.fps       = fps
        self.has_audio = has_audio

        self._pixel_bytes  = width * height * 4
        self._total_size   = _HEADER_SIZE + self._pixel_bytes + AUDIO_BUF_SIZE

        self._hmap:  Optional[pywintypes.HANDLEType] = None  # File-mapping handle
        self._mutex: Optional[pywintypes.HANDLEType] = None  # Named mutex handle
        self._view:  Optional[int]                   = None  # MapViewOfFile pointer

        self._frame_index: int = 0

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ShmFrameWriter":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """
        Create (or open) the Win32 Named File Mapping and Named Mutex.
        Uses a NULL DACL security descriptor so both kernel-mode (driver)
        and user-mode processes can access the objects regardless of session.
        """
        sa = self._build_security_attributes()

        # ---- Named File Mapping (Shared Memory) ----
        # Use CreateFileMappingW with the "Global\" namespace so the
        # DirectShow filter running in a different session can open it.
        # Python's mmap.mmap(tagname=...) creates a *session-local* mapping
        # which the C++ filter (using OpenFileMappingW with "Global\...") cannot see.
        PAGE_READWRITE = 0x04
        FILE_MAP_WRITE = 0x02
        self._hmap = win32file.CreateFileMappingW(
            win32file.INVALID_HANDLE_VALUE,
            sa,
            PAGE_READWRITE,
            0,
            self._total_size,
            SHM_NAME,
        )
        if not self._hmap or self._hmap == win32file.INVALID_HANDLE_VALUE:
            raise OSError(f"CreateFileMappingW failed for '{SHM_NAME}'")

        self._view = win32file.MapViewOfFile(
            self._hmap,
            FILE_MAP_WRITE,
            0,
            0,
            self._total_size,
        )
        if not self._view:
            win32api.CloseHandle(self._hmap)
            self._hmap = None
            raise OSError("MapViewOfFile failed")

        logger.info("SHM opened: {} ({} bytes)", SHM_NAME, self._total_size)

        # ---- Named mutex ----
        self._mutex = win32event.CreateMutex(sa, False, MUTEX_NAME)
        if self._mutex is None:
            raise OSError("CreateMutex failed")
        logger.info("Mutex opened: {}", MUTEX_NAME)

    def close(self) -> None:
        """Release all handles."""
        if self._view is not None:
            try:
                win32file.UnmapViewOfFile(self._view)
            except Exception:
                pass
            self._view = None

        if self._hmap is not None:
            try:
                win32api.CloseHandle(self._hmap)
            except Exception:
                pass
            self._hmap = None

        if self._mutex is not None:
            try:
                win32api.CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None

        logger.info("ShmFrameWriter closed.")

    # ------------------------------------------------------------------
    # Frame writing
    # ------------------------------------------------------------------

    def write_frame(
        self,
        bgra_bytes: bytes,
        audio_bytes: bytes = b"",
    ) -> bool:
        """
        Acquire the named mutex, write the header + pixels + audio into the
        shared memory view, then release the mutex.

        Returns True on success, False if the mutex timed out (frame skipped).
        """
        if self._view is None or self._mutex is None:
            raise RuntimeError("ShmFrameWriter is not open. Call open() first.")

        if len(bgra_bytes) != self._pixel_bytes:
            raise ValueError(
                f"Expected {self._pixel_bytes} bytes of BGRA, got {len(bgra_bytes)}"
            )

        # ---- Acquire mutex ----
        rc = win32event.WaitForSingleObject(self._mutex, MUTEX_TIMEOUT_MS)
        if rc == win32event.WAIT_TIMEOUT:
            logger.warning("Mutex acquisition timed out — skipping frame {}", self._frame_index)
            return False
        if rc == win32event.WAIT_ABANDONED:
            logger.warning("Mutex was abandoned by previous owner — recovering")
        # rc == WAIT_OBJECT_0 or WAIT_ABANDONED → we own the mutex

        try:
            self._do_write(bgra_bytes, audio_bytes)
        finally:
            win32event.ReleaseMutex(self._mutex)

        self._frame_index += 1
        return True

    def _do_write(self, bgra_bytes: bytes, audio_bytes: bytes) -> None:
        """Perform the actual memory write (must be called while mutex is held)."""
        ts_ms = int(time.time() * 1000)
        audio_sz = min(len(audio_bytes), AUDIO_BUF_SIZE)
        flags = 0x1 if self.has_audio and audio_sz > 0 else 0x0

        header = _build_header(
            width            = self.width,
            height           = self.height,
            fps              = self.fps,
            frame_index      = self._frame_index,
            timestamp_ms     = ts_ms,
            audio_chunk_size = audio_sz,
            flags            = flags,
        )

        # Write header + pixels + audio into the mapped memory view.
        # self._view is a ctypes pointer from MapViewOfFile.
        buf = (ctypes.c_char * self._total_size).from_address(self._view)
        offset = 0
        ctypes.memmove(ctypes.addressof(buf) + offset, header, len(header))
        offset += len(header)
        ctypes.memmove(ctypes.addressof(buf) + offset, bgra_bytes, len(bgra_bytes))
        offset += len(bgra_bytes)
        if audio_sz > 0:
            ctypes.memmove(ctypes.addressof(buf) + offset, audio_bytes, audio_sz)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_security_attributes() -> pywintypes.SECURITYATTRIBUTESType:
        """
        Build a SECURITY_ATTRIBUTES with a NULL DACL (grants all access)
        so Global\\ objects are accessible across integrity levels.
        """
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(True, None, False)  # NULL DACL
        sa = pywintypes.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = sd
        return sa


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import random

    WIDTH, HEIGHT, FPS = 1920, 1080, 60
    FRAME_BYTES = WIDTH * HEIGHT * 4

    logger.info("CamNet SHM writer self-test: writing 100 frames…")

    with ShmFrameWriter(width=WIDTH, height=HEIGHT, fps=FPS) as writer:
        for i in range(100):
            # Alternating color pattern for easy visual verification
            colour = (i % 3)
            if colour == 0:
                pixel = b"\xff\x00\x00\xff"  # blue  (BGRA)
            elif colour == 1:
                pixel = b"\x00\xff\x00\xff"  # green (BGRA)
            else:
                pixel = b"\x00\x00\xff\xff"  # red   (BGRA)

            frame  = pixel * (FRAME_BYTES // 4)
            audio  = os.urandom(4096)

            ok = writer.write_frame(frame, audio)
            status = "OK" if ok else "SKIPPED"
            logger.info(f"Frame {i:03d}: {status}  ts={int(time.time()*1000)}")
            time.sleep(1 / FPS)

    logger.info("Self-test complete.")
