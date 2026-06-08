"""
CamNet – SRT Stream Ingestor
receiver/ingest.py

Ingests an SRT stream via FFmpeg, decodes it into raw BGRA video frames and
PCM audio chunks, and feeds them into thread-safe queues.

Dependencies: ffmpeg (on PATH), loguru
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AUDIO_CHUNK_BYTES = 4096          # read granularity for audio pipe
_VIDEO_QUEUE_MAX   = 4             # max frames buffered before drop
_AUDIO_QUEUE_MAX   = 4             # max audio chunks buffered before drop
_RECONNECT_DELAYS  = [0.5, 1.0, 2.0, 4.0, 8.0]  # exponential backoff schedule


class StreamIngestor:
    """
    Manages an FFmpeg subprocess that ingests an SRT camera stream and
    exposes decoded BGRA video frames and PCM audio chunks via thread-safe
    queues.

    FFmpeg topology
    ---------------
    Input : srt://<ip>:<port>?mode=caller&latency=80000
    Video : rawvideo, pix_fmt=bgra, 1920×1080 @ 60 fps → stdout (pipe:1)
    Audio : pcm_s16le, 48 000 Hz, 2 ch → pipe:3  (via extra fd)
    """

    def __init__(
        self,
        ip: str,
        port: int,
        resolution: Tuple[int, int] = (1920, 1080),
        fps: int = 60,
        has_audio: bool = True,
    ) -> None:
        self.ip          = ip
        self.port        = port
        self.width, self.height = resolution
        self.fps         = fps
        self.has_audio   = has_audio

        # Public queues
        self.video_queue: queue.Queue[bytes] = queue.Queue(maxsize=_VIDEO_QUEUE_MAX)
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)

        # Stats
        self.frames_received: int  = 0
        self.frames_dropped: int   = 0
        self.reconnect_count: int  = 0

        # Internal state
        self._process: Optional[subprocess.Popen] = None
        self._audio_pipe_r: Optional[int]  = None   # read end of audio pipe fd
        self._audio_pipe_w: Optional[int]  = None   # write end given to ffmpeg

        self._video_thread: Optional[threading.Thread] = None
        self._audio_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        self._stop_event    = threading.Event()
        self._restart_lock  = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def frame_size(self) -> int:
        """Bytes per raw BGRA frame."""
        return self.width * self.height * 4

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch FFmpeg and begin reading threads."""
        self._stop_event.clear()
        self._launch()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="camnet-watchdog"
        )
        self._watchdog_thread.start()
        logger.info(
            "StreamIngestor started: srt://{}:{}  {}x{}@{} fps  audio={}",
            self.ip, self.port, self.width, self.height, self.fps, self.has_audio,
        )

    def stop(self) -> None:
        """Gracefully terminate FFmpeg and all helper threads."""
        self._stop_event.set()
        self._terminate_process()
        logger.info("StreamIngestor stopped.")

    def read_video_frame(self) -> Optional[bytes]:
        """
        Block up to 0.1 s waiting for a BGRA frame from the video queue.
        Returns exactly ``frame_size`` bytes, or None on timeout.
        """
        try:
            return self.video_queue.get(timeout=0.1)
        except queue.Empty:
            return None

    def read_audio_chunk(self) -> Optional[bytes]:
        """
        Block up to 0.1 s waiting for a PCM audio chunk from the audio queue.
        Returns bytes or None on timeout.
        """
        try:
            return self.audio_queue.get(timeout=0.1)
        except queue.Empty:
            return None

    def is_alive(self) -> bool:
        """True if the FFmpeg process is currently running."""
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # Internal – process management
    # ------------------------------------------------------------------

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Construct the FFmpeg command with dual output pipes."""
        srt_url = (
            f"srt://{self.ip}:{self.port}"
            f"?mode=caller&latency=80000"
        )

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtbufsize", "100M",
            # ---- Input ----
            "-i", srt_url,
            # ---- Video output → stdout (pipe:1) ----
            "-map", "0:v:0",
            "-vf", f"scale={self.width}:{self.height},fps={self.fps}",
            "-pix_fmt", "bgra",
            "-f", "rawvideo",
            "pipe:1",
        ]

        if self.has_audio:
            cmd += [
                # ---- Audio output → pipe:3 (extra fd we create) ----
                "-map", "0:a:0",
                "-acodec", "pcm_s16le",
                "-ar", "48000",
                "-ac", "2",
                "-f", "s16le",
                "pipe:3",
            ]

        return cmd

    def _launch(self) -> None:
        """Create OS pipe for audio and spawn the FFmpeg process."""
        # Close any leftover handles from a previous run
        self._close_audio_pipe()

        # Create an OS-level pipe so FFmpeg can write audio to fd=3
        audio_r, audio_w = os.pipe()
        self._audio_pipe_r = audio_r
        self._audio_pipe_w = audio_w

        cmd = self._build_ffmpeg_cmd()
        logger.debug("FFmpeg cmd: {}", " ".join(cmd))

        popen_kwargs: dict = dict(
            stdin  = subprocess.DEVNULL,
            stdout = subprocess.PIPE,   # video frames → pipe:1
            stderr = subprocess.DEVNULL,
            bufsize = 0,
        )

        if self.has_audio:
            if sys.platform == "win32":
                # On Windows we pass the write-end handle via STARTUPINFO
                # and use pass_fds-equivalent via handles
                import msvcrt
                import ctypes
                HANDLE_FLAG_INHERIT = 0x00000001
                handle_w = msvcrt.get_osfhandle(audio_w)
                ctypes.windll.kernel32.SetHandleInformation(  # type: ignore[attr-defined]
                    handle_w, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
                )
                si = subprocess.STARTUPINFO()
                si.dwFlags     = 0
                popen_kwargs["startupinfo"] = si
                # FFmpeg on Windows: re-route pipe:3 → audio_w handle
                # We embed the handle number in an env var and pass it via
                # -f s16le pipe:<handle> — FFmpeg accepts Windows HANDLE numbers
                # when the fd alias is set via the environment variable
                # FFMPEG_PIPE3_HANDLE.  Instead, use a simpler approach:
                # write audio to a named pipe.
                #
                # Named-pipe approach for Windows audio:
                pipe_name = r"\\.\pipe\camnet_audio"
                self._audio_server = _WindowsNamedPipeServer(pipe_name)
                self._audio_server.start()
                # Replace the pipe:3 url in the command with the named pipe
                idx = cmd.index("pipe:3")
                cmd[idx] = pipe_name
                popen_kwargs.pop("close_fds", None)
                # Close the OS pipe we opened; named pipe takes over
                self._close_audio_pipe()
            else:
                # POSIX: pass the write-end fd as fd=3 into the child
                popen_kwargs["pass_fds"] = (audio_w,)

        self._process = subprocess.Popen(cmd, **popen_kwargs)

        # Close the write end in *this* process (only FFmpeg should write it)
        if not sys.platform == "win32" and self._audio_pipe_w is not None:
            os.close(self._audio_pipe_w)
            self._audio_pipe_w = None

        # Start reader threads
        self._video_thread = threading.Thread(
            target=self._video_reader_loop, daemon=True, name="camnet-video"
        )
        self._video_thread.start()

        if self.has_audio:
            self._audio_thread = threading.Thread(
                target=self._audio_reader_loop, daemon=True, name="camnet-audio"
            )
            self._audio_thread.start()

    def _terminate_process(self) -> None:
        """Send SIGTERM, wait 2 s, then SIGKILL."""
        proc = self._process
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit after SIGTERM — sending SIGKILL")
                proc.kill()
                proc.wait()
        except OSError:
            pass
        finally:
            self._process = None
            self._close_audio_pipe()

    def _close_audio_pipe(self) -> None:
        for fd_attr in ("_audio_pipe_r", "_audio_pipe_w"):
            fd = getattr(self, fd_attr, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_attr, None)

    # ------------------------------------------------------------------
    # Reader loops (daemon threads)
    # ------------------------------------------------------------------

    def _video_reader_loop(self) -> None:
        """Read raw BGRA frames from FFmpeg stdout."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        pipe = proc.stdout
        frame_sz = self.frame_size
        logger.debug("Video reader started, frame_size={}", frame_sz)
        while not self._stop_event.is_set():
            try:
                data = _read_exactly(pipe, frame_sz)
            except (OSError, ValueError):
                logger.debug("Video pipe closed.")
                break
            if data is None or len(data) < frame_sz:
                logger.debug("Video pipe EOF.")
                break
            self.frames_received += 1
            try:
                self.video_queue.put_nowait(data)
            except queue.Full:
                self.frames_dropped += 1
        logger.debug("Video reader exiting.")

    def _audio_reader_loop(self) -> None:
        """Read raw PCM audio from the audio pipe (fd=3 or named pipe)."""
        logger.debug("Audio reader started.")
        if sys.platform == "win32":
            # Read from the named pipe server's queue
            server: _WindowsNamedPipeServer = self._audio_server  # type: ignore[attr-defined]
            while not self._stop_event.is_set():
                chunk = server.read_chunk(timeout=0.5)
                if chunk is None:
                    if not self.is_alive():
                        break
                    continue
                try:
                    self.audio_queue.put_nowait(chunk)
                except queue.Full:
                    pass
        else:
            fd = self._audio_pipe_r
            if fd is None:
                return
            audio_file = os.fdopen(fd, "rb", buffering=0)
            while not self._stop_event.is_set():
                try:
                    chunk = audio_file.read(_AUDIO_CHUNK_BYTES)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    self.audio_queue.put_nowait(chunk)
                except queue.Full:
                    pass
        logger.debug("Audio reader exiting.")

    # ------------------------------------------------------------------
    # Watchdog / reconnection
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Monitor FFmpeg health; restart with exponential backoff on failure."""
        delay_idx = 0
        while not self._stop_event.is_set():
            time.sleep(0.25)
            if self._stop_event.is_set():
                break
            if not self.is_alive():
                if self._stop_event.is_set():
                    break
                delay = _RECONNECT_DELAYS[min(delay_idx, len(_RECONNECT_DELAYS) - 1)]
                self.reconnect_count += 1
                logger.warning(
                    "FFmpeg process died (reconnect #{}) — retrying in {}s",
                    self.reconnect_count, delay,
                )
                time.sleep(delay)
                if self._stop_event.is_set():
                    break
                with self._restart_lock:
                    self._terminate_process()
                    try:
                        self._launch()
                        delay_idx = 0
                        logger.info("Reconnected to srt://{}:{}", self.ip, self.port)
                    except Exception as exc:
                        delay_idx = min(delay_idx + 1, len(_RECONNECT_DELAYS) - 1)
                        logger.error("Reconnect failed: {}", exc)
            else:
                delay_idx = 0   # process healthy — reset backoff
        logger.debug("Watchdog loop exiting.")


# ---------------------------------------------------------------------------
# Windows Named Pipe helper (audio transport on Win32)
# ---------------------------------------------------------------------------

class _WindowsNamedPipeServer:
    """
    Listens on a Windows named pipe and buffers incoming audio bytes into a
    thread-safe queue.  Used only on win32 where pass_fds is not available.
    """

    def __init__(self, pipe_name: str) -> None:
        self._pipe_name  = pipe_name
        self._chunk_q: queue.Queue[bytes] = queue.Queue(maxsize=16)
        self._stop       = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="camnet-audio-pipe"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def read_chunk(self, timeout: float = 0.5) -> Optional[bytes]:
        try:
            return self._chunk_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _serve(self) -> None:
        import pywintypes   # type: ignore[import]
        import win32file    # type: ignore[import]
        import win32pipe    # type: ignore[import]
        import winerror     # type: ignore[import]

        PIPE_BUF = 65536
        while not self._stop.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    self._pipe_name,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    1,
                    PIPE_BUF,
                    PIPE_BUF,
                    0,
                    None,
                )
                win32pipe.ConnectNamedPipe(handle, None)
                while not self._stop.is_set():
                    try:
                        hr, data = win32file.ReadFile(handle, _AUDIO_CHUNK_BYTES)
                        if data:
                            try:
                                self._chunk_q.put_nowait(bytes(data))
                            except queue.Full:
                                pass
                    except pywintypes.error as e:
                        if e.args[0] in (winerror.ERROR_BROKEN_PIPE,
                                         winerror.ERROR_NO_DATA):
                            break
                        raise
                win32file.CloseHandle(handle)
            except Exception as exc:
                logger.warning("Audio named pipe error: {}", exc)
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _read_exactly(pipe, n: int) -> Optional[bytes]:
    """Read exactly *n* bytes from a binary pipe; return None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CamNet SRT ingest smoke-test")
    parser.add_argument("ip",   help="Camera IP address")
    parser.add_argument("port", type=int, help="SRT port")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()

    ingestor = StreamIngestor(
        ip=args.ip,
        port=args.port,
        fps=args.fps,
        has_audio=not args.no_audio,
    )
    ingestor.start()
    logger.info("Reading 300 frames…")
    try:
        for _ in range(300):
            frame = ingestor.read_video_frame()
            if frame:
                logger.info(
                    "frame={} bytes  received={}  dropped={}  reconnects={}",
                    len(frame), ingestor.frames_received,
                    ingestor.frames_dropped, ingestor.reconnect_count,
                )
    except KeyboardInterrupt:
        pass
    finally:
        ingestor.stop()
        logger.info(
            "Done.  received={}  dropped={}  reconnects={}",
            ingestor.frames_received, ingestor.frames_dropped,
            ingestor.reconnect_count,
        )
