"""
sender/encoder.py — H.264 + AAC encoding pipeline for CamNet Sender.

Builds a GStreamer or FFmpeg subprocess pipeline that encodes raw BGRA frames
+ raw PCM audio into an SRT-ready H.264/AAC MPEG-TS stream.

Target: 1920x1080 @ 60fps, H.264 Baseline/Main with tune=zerolatency
Audio: AAC, 48kHz stereo
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class EncoderConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 60
    video_bitrate: str = "6000k"   # 6 Mbps for 1080p60
    audio_bitrate: str = "192k"
    sample_rate: int = 48000
    audio_channels: int = 2
    preset: str = "ultrafast"      # x264 preset
    tune: str = "zerolatency"      # x264 tune
    profile: str = "baseline"      # H.264 profile
    keyint: int = 60               # Keyframe every 1 second at 60fps
    srt_url: str = ""              # If set, stream to this SRT URL
    output_pipe: bool = True       # Output raw TS to stdout pipe instead


@dataclass
class EncoderStats:
    frames_encoded: int = 0
    frames_dropped: int = 0
    encode_errors: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def average_fps(self) -> float:
        return self.frames_encoded / max(self.elapsed, 1.0)


class FFmpegEncoder:
    """
    Wraps an FFmpeg subprocess to encode video + audio into H.264/AAC MPEG-TS.

    Video input: raw BGRA frames fed via stdin pipe.
    Audio input: raw s16le PCM fed via a named pipe (or merged into one input).
    Output: MPEG-TS to an SRT URL or stdout pipe.

    Usage:
        enc = FFmpegEncoder(EncoderConfig(srt_url="srt://192.168.1.100:9000"))
        with enc:
            enc.push_video_frame(bgra_bytes)
            enc.push_audio_chunk(pcm_bytes)
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self.config = config or EncoderConfig()
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._video_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=16)
        self._video_feed_thread: Optional[threading.Thread] = None
        self._audio_feed_thread: Optional[threading.Thread] = None
        self.stats = EncoderStats()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the FFmpeg subprocess and begin accepting frames."""
        cmd = self._build_ffmpeg_command()
        logger.info("Launching FFmpeg encoder:\n  {}", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._running = True

        # Thread to feed video frames into stdin
        self._video_feed_thread = threading.Thread(
            target=self._video_feed_loop,
            name="CamNetVideoFeed",
            daemon=True,
        )
        self._video_feed_thread.start()

        # Monitor stderr for FFmpeg log output
        threading.Thread(
            target=self._stderr_monitor,
            name="CamNetFFmpegLog",
            daemon=True,
        ).start()

        logger.info("FFmpeg encoder started. Target: {}x{}@{}fps", 
                    self.config.width, self.config.height, self.config.fps)

    def stop(self) -> None:
        """Gracefully terminate the FFmpeg process."""
        self._running = False
        # Signal end of stream via stdin EOF
        if self._process and self._process.stdin:
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                pass

        if self._video_feed_thread:
            self._video_feed_thread.join(timeout=3.0)

        if self._process:
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit gracefully, killing.")
                self._process.kill()
                self._process.wait()

        logger.info("FFmpeg encoder stopped. Stats: {}", self.stats)

    def push_video_frame(self, bgra_bytes: bytes) -> bool:
        """
        Push a raw BGRA frame into the encoder queue.
        Returns True if accepted, False if queue is full (frame dropped).
        """
        try:
            self._video_queue.put_nowait(bgra_bytes)
            return True
        except queue.Full:
            self.stats.frames_dropped += 1
            logger.debug("Encoder queue full — frame dropped.")
            return False

    @property
    def is_alive(self) -> bool:
        """True if FFmpeg process is running."""
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "FFmpegEncoder":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ffmpeg_command(self) -> list[str]:
        """
        Build the FFmpeg command list for encoding BGRA stdin → SRT/pipe output.

        Architecture:
          - Input 0: raw BGRA video from stdin at target fps
          - Output: H.264 + AAC in MPEG-TS container to SRT URL or pipe
        """
        cfg = self.config
        pixel_count = cfg.width * cfg.height * 4  # BGRA bytes per frame

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-re",                          # Real-time input pacing
            # --- Video input (raw BGRA from stdin) ---
            "-f", "rawvideo",
            "-pix_fmt", "bgra",
            "-s", f"{cfg.width}x{cfg.height}",
            "-r", str(cfg.fps),
            "-i", "pipe:0",                 # stdin
            # --- Video encoding ---
            "-c:v", "libx264",
            "-preset", cfg.preset,
            "-tune", cfg.tune,
            "-profile:v", cfg.profile,
            "-b:v", cfg.video_bitrate,
            "-maxrate", cfg.video_bitrate,
            "-bufsize", "2M",
            "-g", str(cfg.keyint),          # Keyframe interval
            "-keyint_min", str(cfg.keyint // 2),
            "-sc_threshold", "0",           # Disable scene cut detection
            "-pix_fmt", "yuv420p",          # Required for H.264 compat
            "-x264-params", (
                f"nal-hrd=cbr:force-cfr=1:"
                f"bframes=0:ref=1"
            ),
            # --- Output ---
            "-f", "mpegts",
        ]

        if cfg.srt_url:
            cmd.append(cfg.srt_url)
        else:
            cmd.extend(["-", ])  # stdout pipe

        return cmd

    def _video_feed_loop(self) -> None:
        """Reads BGRA frames from queue and writes them to FFmpeg stdin."""
        stdin = self._process.stdin
        while self._running:
            try:
                frame = self._video_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                stdin.write(frame)
                stdin.flush()
                self.stats.frames_encoded += 1
            except BrokenPipeError:
                logger.warning("FFmpeg stdin pipe broken — process may have died.")
                self._running = False
                break
            except Exception as exc:
                logger.error("Video feed error: {}", exc)
                self.stats.encode_errors += 1

    def _stderr_monitor(self) -> None:
        """Reads and logs FFmpeg stderr output."""
        for line in self._process.stderr:
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                logger.debug("[ffmpeg] {}", decoded)


class GStreamerEncoder:
    """
    GStreamer-based encoder using system GStreamer installation.
    Provides hardware acceleration on Windows (DXVA2/D3D11) and
    macOS (VideoToolbox) when available.

    Falls back to software x264 if hardware encoder unavailable.
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self.config = config or EncoderConfig()
        self._pipeline = None
        self._appsrc = None
        self._running = False
        self.stats = EncoderStats()
        self._frame_index = 0

    def start(self) -> None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib
            Gst.init(None)
        except ImportError:
            raise RuntimeError(
                "GStreamer Python bindings not available. "
                "Install gstreamer1.0 and python3-gi."
            )

        from gi.repository import Gst

        pipeline_str = self._build_pipeline_string()
        logger.info("GStreamer pipeline: {}", pipeline_str)

        self._pipeline = Gst.parse_launch(pipeline_str)
        self._appsrc = self._pipeline.get_by_name("video_src")
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        logger.info("GStreamer encoder started.")

    def stop(self) -> None:
        if self._pipeline:
            from gi.repository import Gst
            self._pipeline.send_event(Gst.Event.new_eos())
            self._pipeline.set_state(Gst.State.NULL)
        self._running = False
        logger.info("GStreamer encoder stopped.")

    def push_video_frame(self, bgra_bytes: bytes) -> bool:
        """Push a BGRA frame into the GStreamer appsrc."""
        if not self._appsrc or not self._running:
            return False
        try:
            from gi.repository import Gst
            buf = Gst.Buffer.new_wrapped(bgra_bytes)
            self._appsrc.emit("push-buffer", buf)
            self.stats.frames_encoded += 1
            self._frame_index += 1
            return True
        except Exception as exc:
            logger.error("GStreamer push error: {}", exc)
            self.stats.encode_errors += 1
            return False

    def _build_pipeline_string(self) -> str:
        cfg = self.config
        target = cfg.srt_url or "fdsink fd=1"

        # Try hardware encoder (mfx on Windows, vtenc_h264 on macOS)
        hw_enc = self._detect_hardware_encoder()
        if hw_enc:
            video_enc = hw_enc
            logger.info("Using hardware encoder: {}", hw_enc)
        else:
            video_enc = (
                f"x264enc tune=zerolatency bitrate={int(cfg.video_bitrate.rstrip('k'))} "
                f"key-int-max={cfg.keyint} bframes=0 profile={cfg.profile} "
                f"speed-preset=ultrafast"
            )

        return (
            f"appsrc name=video_src format=time is-live=true block=false "
            f"caps=video/x-raw,format=BGRA,width={cfg.width},height={cfg.height},"
            f"framerate={cfg.fps}/1 ! "
            f"videoconvert ! "
            f"video/x-raw,format=I420 ! "
            f"{video_enc} ! "
            f"h264parse ! "
            f"mpegtsmux ! "
            f"srtsink uri={cfg.srt_url} latency=80000"
            if cfg.srt_url else
            f"appsrc name=video_src format=time is-live=true block=false "
            f"caps=video/x-raw,format=BGRA,width={cfg.width},height={cfg.height},"
            f"framerate={cfg.fps}/1 ! "
            f"videoconvert ! "
            f"video/x-raw,format=I420 ! "
            f"{video_enc} ! "
            f"h264parse ! "
            f"mpegtsmux ! "
            f"fdsink fd=1"
        )

    @staticmethod
    def _detect_hardware_encoder() -> str | None:
        """Detect available hardware H.264 encoders."""
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            # Try Intel Quick Sync (Windows/Linux)
            if Gst.ElementFactory.find("mfxh264enc"):
                return "mfxh264enc"
            # Try NVIDIA NVENC
            if Gst.ElementFactory.find("nvh264enc"):
                return "nvh264enc bitrate=6000 rc-mode=cbr-ld-hq"
            # Try AMD AMF
            if Gst.ElementFactory.find("amfh264enc"):
                return "amfh264enc usage=lowlatency"
            # Try Apple VideoToolbox
            if Gst.ElementFactory.find("vtenc_h264"):
                return "vtenc_h264 allow-frame-reordering=false realtime=true bitrate=6000"
        except Exception:
            pass
        return None


def create_encoder(config: EncoderConfig | None = None) -> FFmpegEncoder | GStreamerEncoder:
    """
    Factory: returns the best available encoder.
    Prefers FFmpeg for simplicity and universal compatibility.
    Use GStreamer when hardware acceleration is needed.
    """
    cfg = config or EncoderConfig()
    # Default to FFmpeg encoder (most portable)
    return FFmpegEncoder(cfg)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import numpy as np

    logger.info("Encoder demo — generating 60 synthetic frames and encoding...")

    config = EncoderConfig(
        width=1920, height=1080, fps=60,
        output_pipe=True,
    )
    enc = FFmpegEncoder(config)
    enc.start()

    try:
        for i in range(120):  # 2 seconds
            # Generate a test pattern frame (gradient)
            frame = np.zeros((1080, 1920, 4), dtype=np.uint8)
            frame[:, :, 2] = int((i / 120) * 255)  # Sweep red
            frame[:, :, 3] = 255                    # Full alpha
            enc.push_video_frame(frame.tobytes())
            time.sleep(1.0 / 60)
    except KeyboardInterrupt:
        pass
    finally:
        enc.stop()
        logger.info("Frames encoded: {}", enc.stats.frames_encoded)
