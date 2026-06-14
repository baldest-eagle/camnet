"""
sender/streamer.py — SRT stream output for CamNet Sender.

Manages an SRT server-mode socket that accepts one receiver connection,
then pumps MPEG-TS encoded video+audio into it.

Also provides a WebRTC fallback mode for browser-based senders.
"""

from __future__ import annotations

import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class StreamerConfig:
    # SRT settings
    srt_port: int = 9000
    srt_latency_ms: int = 80          # SRT receiver latency buffer
    srt_max_bw: int = -1              # -1 = unlimited
    srt_payload_size: int = 1316      # Standard MPEG-TS payload size

    # Source stream settings (from encoder)
    width: int = 1920
    height: int = 1080
    fps: int = 60
    video_bitrate: str = "6000k"
    audio_bitrate: str = "192k"
    sample_rate: int = 48000
    audio_channels: int = 2

    # Reconnect settings
    max_reconnects: int = 0           # 0 = unlimited
    reconnect_delay_s: float = 1.0


@dataclass
class StreamStats:
    bytes_sent: int = 0
    frames_sent: int = 0
    reconnect_count: int = 0
    start_time: float = field(default_factory=time.time)
    connected: bool = False

    @property
    def mbps(self) -> float:
        elapsed = time.time() - self.start_time
        return (self.bytes_sent * 8) / max(elapsed, 1.0) / 1_000_000


class SRTStreamer:
    """
    Runs an FFmpeg process in SRT server mode, pulling from a local camera
    via DSHOW/V4L2 and streaming H.264+AAC MPEG-TS to a waiting receiver.

    The Receiver acts as an SRT caller; this Sender acts as an SRT listener
    (server). This avoids firewall issues on the receiver side.

    Full pipeline:
      Camera → FFmpeg(capture+encode) → SRT listener → Receiver
    """

    def __init__(self, config: StreamerConfig | None = None,
                 device_index: int = 0) -> None:
        self.config = config or StreamerConfig()
        self.device_index = device_index
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.stats = StreamStats()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the SRT stream."""
        self._running = True
        self._launch_stream()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="CamNetStreamMonitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info(
            "SRT stream started on port {}. Waiting for receiver...",
            self.config.srt_port,
        )

    def stop(self) -> None:
        """Stop the SRT stream gracefully."""
        self._running = False
        self._terminate_process()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
        logger.info("SRT streamer stopped. Stats: {}", self.stats)

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def srt_url(self) -> str:
        return f"srt://0.0.0.0:{self.config.srt_port}?mode=listener"

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SRTStreamer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ffmpeg_command(self) -> list[str]:
        """
        Build FFmpeg command to capture from camera and stream via SRT.

        On Windows, uses DirectShow (dshow) for camera access.
        On Linux, uses v4l2.
        """
        import sys
        cfg = self.config

        srt_output = (
            f"srt://0.0.0.0:{cfg.srt_port}"
            f"?mode=listener"
            f"&latency={cfg.srt_latency_ms * 1000}"  # microseconds
            f"&peerlatency={cfg.srt_latency_ms * 1000}"
            f"&rcvbuf=8388608"
            f"&sndbuf=8388608"
        )

        if sys.platform == "win32":
            # Dynamically find DirectShow devices to avoid @device_idx_ syntax issues
            import subprocess
            import re
            
            try:
                res = subprocess.run(['ffmpeg', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'], stderr=subprocess.PIPE, text=True, encoding='utf-8')
                v_devs = []
                a_devs = []
                for line in res.stderr.split('\n'):
                    m = re.search(r'\]\s+"([^"]+)"\s+\((video|audio)\)', line)
                    if m:
                        if m.group(2) == 'video': v_devs.append(m.group(1))
                        else: a_devs.append(m.group(1))
                        
                video_name = v_devs[self.device_index] if self.device_index < len(v_devs) else v_devs[0] if v_devs else "video=Integrated Camera"
                audio_name = a_devs[self.device_index] if self.device_index < len(a_devs) else a_devs[0] if a_devs else "audio=Microphone"
                
                video_input = [
                    "-f", "dshow",
                    "-video_size", f"{cfg.width}x{cfg.height}",
                    "-framerate", str(cfg.fps),
                    "-i", f"video={video_name}",
                ]
                audio_input = [
                    "-f", "dshow",
                    "-i", f"audio={audio_name}",
                ]
            except Exception as e:
                logger.error(f"Failed to enumerate devices, falling back: {e}")
                video_input = [
                    "-f", "dshow",
                    "-video_size", f"{cfg.width}x{cfg.height}",
                    "-framerate", str(cfg.fps),
                    "-i", f"video=@device_idx_{self.device_index}",
                ]
                audio_input = [
                    "-f", "dshow",
                    "-i", f"audio=@device_idx_{self.device_index}",
                ]
        elif sys.platform == "darwin":
            video_input = [
                "-f", "avfoundation",
                "-framerate", str(cfg.fps),
                "-video_size", f"{cfg.width}x{cfg.height}",
                "-i", f"{self.device_index}:0",
            ]
            audio_input = []  # avfoundation handles audio in same input
        else:
            video_input = [
                "-f", "v4l2",
                "-input_format", "mjpeg",
                "-video_size", f"{cfg.width}x{cfg.height}",
                "-framerate", str(cfg.fps),
                "-i", f"/dev/video{self.device_index}",
            ]
            audio_input = [
                "-f", "alsa",
                "-i", "default",
            ]

        return [
            "ffmpeg",
            "-loglevel", "warning",
            *video_input,
            *audio_input,
            # Video encode
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-b:v", cfg.video_bitrate,
            "-maxrate", cfg.video_bitrate,
            "-bufsize", "2M",
            "-g", str(cfg.fps),              # Keyframe every second
            "-keyint_min", str(cfg.fps // 2),
            "-sc_threshold", "0",
            "-pix_fmt", "yuv420p",
            "-x264-params", "bframes=0:ref=1:nal-hrd=cbr",
            # Audio encode
            "-c:a", "aac",
            "-b:a", cfg.audio_bitrate,
            "-ar", str(cfg.sample_rate),
            "-ac", str(cfg.audio_channels),
            # Output
            "-f", "mpegts",
            srt_output,
        ]

    def _launch_stream(self) -> None:
        """Launch the FFmpeg process."""
        cmd = self._build_ffmpeg_command()
        logger.info("Launching FFmpeg SRT streamer:\n  {}", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(
            target=self._log_stderr,
            name="CamNetFFmpegErr",
            daemon=True,
        ).start()

    def _terminate_process(self) -> None:
        """Terminate the FFmpeg subprocess."""
        if self._process:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            self._process = None

    def _monitor_loop(self) -> None:
        """Watch the FFmpeg process and restart on crash."""
        reconnect_delay = self.config.reconnect_delay_s
        max_reconnects = self.config.max_reconnects
        reconnect_count = 0

        while self._running:
            if self._process and self._process.poll() is not None:
                exit_code = self._process.returncode
                logger.warning("FFmpeg exited with code {}.", exit_code)

                if max_reconnects and reconnect_count >= max_reconnects:
                    logger.error("Max reconnects reached. Stopping streamer.")
                    self._running = False
                    break

                reconnect_count += 1
                self.stats.reconnect_count += 1
                logger.info(
                    "Reconnecting in {:.1f}s (attempt #{})...",
                    reconnect_delay,
                    reconnect_count,
                )
                time.sleep(reconnect_delay)
                # Exponential backoff, cap at 8s
                reconnect_delay = min(reconnect_delay * 2, 8.0)

                if self._running:
                    self._launch_stream()
                    reconnect_delay = self.config.reconnect_delay_s  # Reset on success start
            else:
                self.stats.connected = self.is_alive
                time.sleep(1.0)

    def _log_stderr(self) -> None:
        """Log FFmpeg stderr output."""
        for line in self._process.stderr:
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                logger.debug("[ffmpeg-stream] {}", decoded)


class WebRTCSender:
    """
    WebRTC-based sender for smartphone/browser clients.
    Uses aiortc to serve a WebRTC offer to a connecting browser.

    The browser accesses a local HTTP page that:
    1. Gets camera permission via getUserMedia
    2. Connects to this aiortc server via WebRTC signaling
    3. Streams video + audio over WebRTC DataChannel / MediaTrack

    This class receives the WebRTC stream and converts it to
    the same BGRA frame format as the SRT path.
    """

    def __init__(self, config: StreamerConfig | None = None,
                 http_port: int = 8080) -> None:
        self.config = config or StreamerConfig()
        self.http_port = http_port
        self._running = False
        self._frame_callback = None
        self._server = None

    def on_frame(self, callback) -> None:
        """Register callback to receive decoded BGRA frames from WebRTC peer."""
        self._frame_callback = callback

    def start(self) -> None:
        """Start the WebRTC signaling HTTP server."""
        import asyncio
        import threading

        self._running = True
        loop = asyncio.new_event_loop()
        self._loop = loop
        threading.Thread(
            target=lambda: loop.run_until_complete(self._serve()),
            name="CamNetWebRTC",
            daemon=True,
        ).start()
        logger.info(
            "WebRTC sender listening at http://localhost:{}/", self.http_port
        )

    def stop(self) -> None:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _serve(self) -> None:
        """Async HTTP server for WebRTC signaling (SDP offer/answer)."""
        try:
            from aiohttp import web
            from aiortc import RTCPeerConnection, RTCSessionDescription

            pcs: set[RTCPeerConnection] = set()

            async def offer(request):
                params = await request.json()
                offer_sdp = RTCSessionDescription(
                    sdp=params["sdp"], type=params["type"]
                )
                pc = RTCPeerConnection()
                pcs.add(pc)

                @pc.on("track")
                async def on_track(track):
                    if track.kind == "video":
                        asyncio.ensure_future(self._consume_video(track))
                    elif track.kind == "audio":
                        logger.debug("WebRTC audio track received.")

                await pc.setRemoteDescription(offer_sdp)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                return web.json_response({
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                })

            app = web.Application()
            app.router.add_post("/offer", offer)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", self.http_port)
            await site.start()

            while self._running:
                await asyncio.sleep(1.0)

        except ImportError:
            logger.error(
                "aiortc or aiohttp not installed. "
                "WebRTC mode unavailable. Use SRT instead."
            )

    async def _consume_video(self, track) -> None:
        """Consume video frames from WebRTC track and call frame callback."""
        import av
        while self._running:
            try:
                frame = await track.recv()
                if self._frame_callback:
                    # Convert VideoFrame → numpy BGRA
                    img = frame.to_ndarray(format="bgra")
                    self._frame_callback(img.tobytes())
            except Exception as exc:
                logger.warning("WebRTC video track ended: {}", exc)
                break


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logger.info("Starting SRT streamer on port 9000...")
    config = StreamerConfig(srt_port=9000, width=1920, height=1080, fps=60)
    streamer = SRTStreamer(config, device_index=0)

    try:
        with streamer:
            logger.info("Streaming. Press Ctrl+C to stop.")
            while True:
                time.sleep(5)
                logger.info(
                    "Stream alive={} stats={}",
                    streamer.is_alive,
                    streamer.stats,
                )
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
