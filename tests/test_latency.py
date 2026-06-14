"""
CamNet – End-to-End Latency Benchmark & Dropout Simulation
tests/test_latency.py

Run with:  pytest tests/test_latency.py -v -s

All tests are skipped automatically on non-Windows platforms.

Benchmark output is printed to stdout; pass ``-s`` to pytest to see it.
"""

from __future__ import annotations

import queue
import statistics
import sys
import threading
import time
from typing import Generator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Skip the entire module on non-Windows
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Latency tests require Windows (win32)",
)

# ---------------------------------------------------------------------------
# Import modules under test
# ---------------------------------------------------------------------------
try:
    from receiver.shm_writer import (
        ShmFrameWriter,
        _HEADER_SIZE,
        AUDIO_BUF_SIZE,
        MAGIC,
    )
    _HAS_SHM = True
except ImportError as _e:
    _HAS_SHM = False
    _shm_import_err = str(_e)

_shm_available = pytest.mark.skipif(
    not _HAS_SHM,
    reason=f"shm_writer unavailable: {_shm_import_err if not _HAS_SHM else ''}",  # type: ignore[name-defined]
)

try:
    from receiver.ingest import StreamIngestor, _RECONNECT_DELAYS
    _HAS_INGEST = True
except ImportError as _e2:
    _HAS_INGEST = False
    _ingest_import_err = str(_e2)

_ingest_available = pytest.mark.skipif(
    not _HAS_INGEST,
    reason=f"ingest unavailable: {_ingest_import_err if not _HAS_INGEST else ''}",  # type: ignore[name-defined]
)

# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

WIDTH, HEIGHT, FPS = 1920, 1080, 60
PIXEL_BYTES = WIDTH * HEIGHT * 4
BLANK_FRAME = b"\x00" * PIXEL_BYTES
AUDIO_CHUNK = b"\xAB\xCD" * 2048   # 4 096 bytes


def _make_writer(**kw) -> ShmFrameWriter:
    return ShmFrameWriter(width=WIDTH, height=HEIGHT, fps=FPS, **kw)


# ---------------------------------------------------------------------------
# Test 1 – single frame write latency
# ---------------------------------------------------------------------------

@_shm_available
def test_shm_write_latency() -> None:
    """Time to write one frame must be < 5 ms."""
    with _make_writer() as w:
        # Warm-up: one write before measuring so OS page-faults are absorbed
        w.write_frame(BLANK_FRAME)

        t0 = time.perf_counter()
        w.write_frame(BLANK_FRAME)
        elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 5.0, (
        f"Single frame write took {elapsed_ms:.3f} ms — expected < 5 ms"
    )


# ---------------------------------------------------------------------------
# Test 2 – pipeline throughput
# ---------------------------------------------------------------------------

@_shm_available
def test_frame_pipeline_throughput() -> None:
    """
    Write 60 frames sequentially; measured throughput must be >= 55 fps
    (within 10 % of target).
    """
    N = 60
    with _make_writer() as w:
        # Warm-up
        w.write_frame(BLANK_FRAME)

        t0 = time.perf_counter()
        for _ in range(N):
            w.write_frame(BLANK_FRAME)
        elapsed = time.perf_counter() - t0

    achieved_fps = N / elapsed
    assert achieved_fps >= 55.0, (
        f"Throughput {achieved_fps:.1f} fps is below the 55 fps floor "
        f"(elapsed={elapsed:.3f}s for {N} frames)"
    )
    print(f"\n[throughput] {N} frames in {elapsed*1000:.1f} ms → {achieved_fps:.1f} fps")


# ---------------------------------------------------------------------------
# Test 3 – dropout / reconnect simulation
# ---------------------------------------------------------------------------

@_ingest_available
def test_dropout() -> None:
    """
    Mock StreamIngestor to simulate 5 good frames → ConnectionError → resume.
    Verify reconnect_count == 1 and frames resume flowing.
    """
    # We test the reconnect *logic* directly without launching FFmpeg.
    # Strategy: subclass StreamIngestor, override _launch to install a
    # fake video-reader that feeds controlled data/errors.

    call_count = [0]
    frames_fed_per_run: list[int] = []

    class _FakeIngestor(StreamIngestor):
        def _launch(self) -> None:
            """Override FFmpeg launch with a synthetic frame feeder."""
            run = call_count[0]
            call_count[0] += 1

            n_frames = 5 if run == 0 else 10   # first run: 5 frames; second: 10

            def _feed():
                for _ in range(n_frames):
                    if self._stop_event.is_set():
                        return
                    frame = BLANK_FRAME
                    try:
                        self.video_queue.put(frame, timeout=0.5)
                        self.frames_received += 1
                    except queue.Full:
                        self.frames_dropped += 1
                # Simulate process death: we set _process to None
                self._process = None   # watchdog will detect this
                frames_fed_per_run.append(n_frames)

            self._process = MagicMock()  # alive placeholder
            self._process.poll.return_value = None   # alive
            t = threading.Thread(target=_feed, daemon=True)
            t.start()

    ingestor = _FakeIngestor(ip="127.0.0.1", port=9999, has_audio=False)
    ingestor.start()

    # Collect frames for 4 s total (covers reconnect + second run)
    collected: list[bytes] = []
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        frame = ingestor.read_video_frame()
        if frame is not None:
            collected.append(frame)
        if ingestor.reconnect_count >= 1 and len(collected) >= 10:
            break

    ingestor.stop()

    assert ingestor.reconnect_count >= 1, (
        "Expected at least one reconnect after simulated stream death, "
        f"got reconnect_count={ingestor.reconnect_count}"
    )
    assert len(collected) >= 10, (
        f"Expected at least 10 frames total (5+5 min), got {len(collected)}"
    )
    print(
        f"\n[dropout] frames={len(collected)} reconnects={ingestor.reconnect_count}"
    )


# ---------------------------------------------------------------------------
# Test 4 – latency benchmark (1 000 frames, p50 / p95 / p99)
# ---------------------------------------------------------------------------

@_shm_available
def test_latency_benchmark() -> None:
    """
    Write 1 000 frames, measure per-frame write latency.
    Assert p50 < 2 ms, p99 < 10 ms.
    Print a formatted results table.
    """
    N = 1_000
    timings: List[float] = []

    with _make_writer() as w:
        # Warm-up
        for _ in range(10):
            w.write_frame(BLANK_FRAME)

        for _ in range(N):
            t0 = time.perf_counter()
            w.write_frame(BLANK_FRAME)
            timings.append((time.perf_counter() - t0) * 1000)  # ms

    timings_sorted = sorted(timings)
    p50 = statistics.median(timings_sorted)
    p95 = timings_sorted[int(0.95 * N) - 1]
    p99 = timings_sorted[int(0.99 * N) - 1]
    p_min = timings_sorted[0]
    p_max = timings_sorted[-1]
    mean  = statistics.mean(timings)

    table = (
        f"\n{'─'*42}\n"
        f"  CamNet SHM Write Latency ({N} frames)\n"
        f"{'─'*42}\n"
        f"  Min   : {p_min:>8.3f} ms\n"
        f"  Mean  : {mean:>8.3f} ms\n"
        f"  p50   : {p50:>8.3f} ms\n"
        f"  p95   : {p95:>8.3f} ms\n"
        f"  p99   : {p99:>8.3f} ms\n"
        f"  Max   : {p_max:>8.3f} ms\n"
        f"{'─'*42}\n"
    )
    print(table)

    assert p50 < 2.0, f"p50 latency {p50:.3f} ms exceeds 2 ms target"
    assert p99 < 10.0, f"p99 latency {p99:.3f} ms exceeds 10 ms target"
