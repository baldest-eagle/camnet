"""
CamNet – Shared Memory correctness tests
tests/test_shm.py

Run with:  pytest tests/test_shm.py -v

All tests are skipped automatically on non-Windows platforms.
"""

from __future__ import annotations

import mmap
import struct
import sys
import threading
import time
from typing import Generator

import pytest

# Skip the entire module on non-Windows
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Shared memory tests require Windows (win32)",
)

# ---------------------------------------------------------------------------
# Bring in the module under test.  If pywin32 is missing the tests will be
# skipped gracefully instead of erroring at collection time.
# ---------------------------------------------------------------------------
try:
    from receiver.shm_writer import (
        MAGIC,
        MUTEX_NAME,
        SHM_NAME,
        AUDIO_BUF_SIZE,
        ShmFrameWriter,
        _HEADER_SIZE,
    )
    _HAS_SHM = True
except ImportError as _imp_err:
    _HAS_SHM = False
    _imp_err_msg = str(_imp_err)

_shm_available = pytest.mark.skipif(
    not _HAS_SHM,
    reason=f"shm_writer unavailable: {_imp_err_msg if not _HAS_SHM else ''}",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WIDTH, HEIGHT, FPS = 1920, 1080, 60
PIXEL_BYTES = WIDTH * HEIGHT * 4
BLANK_FRAME = b"\x00" * PIXEL_BYTES
WHITE_FRAME = b"\xff" * PIXEL_BYTES


def _make_writer(**kw) -> ShmFrameWriter:
    return ShmFrameWriter(width=WIDTH, height=HEIGHT, fps=FPS, **kw)


def _read_shm_raw(writer: ShmFrameWriter) -> bytes:
    """Read the full SHM view content into a bytes object."""
    view: mmap.mmap = writer._view  # type: ignore[assignment]
    view.seek(0)
    total = _HEADER_SIZE + PIXEL_BYTES + AUDIO_BUF_SIZE
    return view.read(total)


def _parse_header(raw: bytes) -> dict:
    """Unpack the 40-byte SHM header fields."""
    (magic, width, height, fps,
     frame_index, timestamp_ms,
     audio_chunk_size, flags) = struct.unpack_from("<IIIIQQii", raw, 0)
    return {
        "magic":           magic,
        "width":           width,
        "height":          height,
        "fps":             fps,
        "frame_index":     frame_index,
        "timestamp_ms":    timestamp_ms,
        "audio_chunk_size": audio_chunk_size,
        "flags":           flags,
    }


# ---------------------------------------------------------------------------
# Test 1: open / close lifecycle
# ---------------------------------------------------------------------------

@_shm_available
def test_shm_open_close() -> None:
    """ShmFrameWriter must open and close without raising."""
    writer = _make_writer()
    writer.open()
    try:
        assert writer._hmap  is not None, "File-mapping handle must be set after open()"
        assert writer._mutex is not None, "Mutex handle must be set after open()"
        assert writer._view  is not None, "mmap view must be set after open()"
    finally:
        writer.close()

    assert writer._hmap  is None, "File-mapping handle must be None after close()"
    assert writer._mutex is None, "Mutex handle must be None after close()"
    assert writer._view  is None, "mmap view must be None after close()"


# ---------------------------------------------------------------------------
# Test 2: header fields correctness
# ---------------------------------------------------------------------------

@_shm_available
def test_frame_write_header() -> None:
    """Write a frame; verify magic, width, height, fps, frame_index from SHM."""
    with _make_writer() as writer:
        writer.write_frame(BLANK_FRAME)
        raw = _read_shm_raw(writer)
        hdr = _parse_header(raw)

    assert hdr["magic"]       == MAGIC,  f"magic mismatch: {hdr['magic']:#010x}"
    assert hdr["width"]       == WIDTH,  f"width mismatch: {hdr['width']}"
    assert hdr["height"]      == HEIGHT, f"height mismatch: {hdr['height']}"
    assert hdr["fps"]         == FPS,    f"fps mismatch: {hdr['fps']}"
    assert hdr["frame_index"] == 0,      "first frame must have frame_index 0"
    assert hdr["timestamp_ms"] > 0,      "timestamp_ms must be positive"


# ---------------------------------------------------------------------------
# Test 3: pixel data integrity
# ---------------------------------------------------------------------------

@_shm_available
def test_frame_write_pixels() -> None:
    """Write a known all-0xFF pattern (white frame) and verify pixel data."""
    with _make_writer() as writer:
        writer.write_frame(WHITE_FRAME)
        raw = _read_shm_raw(writer)

    pixel_section = raw[_HEADER_SIZE: _HEADER_SIZE + PIXEL_BYTES]
    assert pixel_section == WHITE_FRAME, (
        "Pixel data read back from SHM does not match the written white frame"
    )


# ---------------------------------------------------------------------------
# Test 4: frame_index monotonically increments
# ---------------------------------------------------------------------------

@_shm_available
def test_frame_index_increments() -> None:
    """Write 5 frames; verify frame_index goes 0 → 4."""
    indices: list[int] = []
    with _make_writer() as writer:
        for _ in range(5):
            writer.write_frame(BLANK_FRAME)
            raw = _read_shm_raw(writer)
            hdr = _parse_header(raw)
            indices.append(hdr["frame_index"])

    assert indices == list(range(5)), (
        f"frame_index sequence was {indices}, expected [0, 1, 2, 3, 4]"
    )


# ---------------------------------------------------------------------------
# Test 5: audio chunk written correctly
# ---------------------------------------------------------------------------

@_shm_available
def test_audio_chunk_written() -> None:
    """Write a frame with audio; verify audio_chunk_size header and raw audio bytes."""
    audio_payload = b"\xAB\xCD" * 2048   # 4096 bytes of known pattern
    audio_size    = len(audio_payload)

    with _make_writer(has_audio=True) as writer:
        writer.write_frame(BLANK_FRAME, audio_payload)
        raw = _read_shm_raw(writer)

    hdr = _parse_header(raw)
    assert hdr["audio_chunk_size"] == audio_size, (
        f"audio_chunk_size header is {hdr['audio_chunk_size']}, expected {audio_size}"
    )
    assert hdr["flags"] & 0x1, "has_audio flag (bit 0) must be set"

    audio_section = raw[_HEADER_SIZE + PIXEL_BYTES: _HEADER_SIZE + PIXEL_BYTES + audio_size]
    assert audio_section == audio_payload, "Audio bytes read from SHM do not match written data"


# ---------------------------------------------------------------------------
# Test 6: concurrent read / write – no data corruption
# ---------------------------------------------------------------------------

@_shm_available
def test_mutex_synchronization() -> None:
    """
    Spin a reader thread that continuously reads the SHM while the writer
    pumps frames.  Verify that the magic number is always intact (a torn
    write would produce garbage in that field).
    """
    NUM_FRAMES   = 50
    READER_POLLS = 200

    errors: list[str] = []

    with _make_writer() as writer:

        def reader() -> None:
            view: mmap.mmap = writer._view  # type: ignore[assignment]
            for _ in range(READER_POLLS):
                view.seek(0)
                raw = view.read(4)
                if len(raw) == 4:
                    magic_read = struct.unpack_from("<I", raw, 0)[0]
                    # Before first frame is written, value may be 0
                    if magic_read not in (0, MAGIC):
                        errors.append(
                            f"Corrupt magic: {magic_read:#010x}"
                        )
                time.sleep(0.001)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        for _ in range(NUM_FRAMES):
            writer.write_frame(BLANK_FRAME)
            time.sleep(0.001)

        t.join(timeout=5.0)

    assert not errors, "Data corruption detected under concurrent access:\n" + "\n".join(errors)
