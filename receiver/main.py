"""
receiver/main.py — CamNet Receiver entry point (Windows / Linux).

Orchestrates:
- mDNS discovery of sender devices
- SRT stream ingest + decode
- Platform-specific frame output:
    Windows → Shared memory (DirectShow filter reads it)
    Linux   → V4L2 loopback device + POSIX SHM mirror
- REST API for driver / external communication
- System tray GUI for user control
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
# Ensure the project root is on sys.path so `from receiver.xxx` imports work
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import click
from loguru import logger

# Configure loguru
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="INFO",
    colorize=True,
)
logger.add(
    Path(__file__).parent / "logs" / "receiver_{time:YYYY-MM-DD}.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
)


class CamNetReceiver:
    """
    Top-level orchestrator for the CamNet Receiver.

    Lifecycle:
    1. Start REST controller → DirectShow/V4L2 consumer can query /shm_info
    2. Start mDNS discovery → detect sender devices on LAN
    3. Auto-connect to first discovered device (or wait for manual connect)
    4. Start ingest pipeline → decode SRT stream into frames
    5. Platform frame writer feeds the virtual camera:
       - Windows: Shared memory → DirectShow filter → OBS
       - Linux:   V4L2 loopback device → OBS / any V4L2 app
    """

    def __init__(
        self,
        auto_connect: bool = True,
        srt_port: int = 9000,
        width: int = 1920,
        height: int = 1080,
        fps: int = 60,
        no_tray: bool = False,
        v4l2_device: str = "",
        enable_shm: bool = True,
    ) -> None:
        self.auto_connect = auto_connect
        self.srt_port = srt_port
        self.width = width
        self.height = height
        self.fps = fps
        self.no_tray = no_tray
        self.v4l2_device = v4l2_device
        self.enable_shm = enable_shm

        self._stop_event = threading.Event()
        self._running = False

        # Import here so errors are surfaced at startup
        from receiver.controller import ReceiverState, ReceiverController
        from receiver.discovery import CamNetDiscovery
        from receiver.ingest import StreamIngestor
        from receiver.platform_shm import create_frame_writer, get_shm_name, get_mutex_name

        self._state = ReceiverState()
        self._state.shm_name = get_shm_name()
        self._state.shm_mutex_name = get_mutex_name()
        self._state.platform = sys.platform  # "win32" or "linux"

        self._controller = ReceiverController(self._state)
        self._discovery = CamNetDiscovery()
        self._ingestor: StreamIngestor | None = None

        # Platform-specific frame writer (created on start)
        self._frame_writer = create_frame_writer(
            width=width,
            height=height,
            fps=fps,
            has_audio=True,
            device=v4l2_device,
            enable_shm=enable_shm,
        )

        # Propagate resolved V4L2 device path to state (for /status endpoint)
        if sys.platform == "linux" and hasattr(self._frame_writer, "device"):
            self._state.v4l2_device = self._frame_writer.device
        self._tray = None
        self._pump_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all receiver components."""
        platform_label = "Windows (DirectShow)" if sys.platform == "win32" else "Linux (V4L2)"
        logger.info("CamNet Receiver starting [{}]...", platform_label)
        self._state.start_time = time.time()

        # 1. REST API (DirectShow filter / V4L2 consumer needs this)
        self._controller.on_connect_request(self._handle_connect)
        self._controller.on_disconnect_request(self._handle_disconnect)
        self._controller.set_devices_list_fn(self._get_devices_list)
        self._controller.start()

        # 2. Frame writer (SHM on Windows, V4L2+SHM on Linux)
        self._frame_writer.open()
        logger.info("Frame writer opened: {}", self._state.shm_name)

        # 3. mDNS discovery
        self._discovery.on_device_found(self._on_device_found)
        self._discovery.on_device_lost(self._on_device_lost)
        self._discovery.start()

        self._running = True
        logger.success(
            "CamNet Receiver ready!\n"
            "  Platform:     {}\n"
            "  REST API:     http://localhost:7432/\n"
            "  Shared Mem:   {}\n"
            "  Waiting for sender devices on LAN...",
            platform_label,
            self._state.shm_name,
        )

        # 4. System tray
        if not self.no_tray:
            self._start_tray()

    def stop(self) -> None:
        """Stop all components gracefully."""
        logger.info("CamNet Receiver shutting down...")
        self._running = False

        self._handle_disconnect()

        self._discovery.stop()

        if self._frame_writer:
            self._frame_writer.close()

        self._stop_event.set()
        logger.success("CamNet Receiver stopped.")

    def wait(self) -> None:
        """Block until stop() is called."""
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            self.stop()

    # ------------------------------------------------------------------
    # Device event handlers
    # ------------------------------------------------------------------

    def _on_device_found(self, device) -> None:
        logger.success(
            "📷 Sender discovered: {} @ {}:{} | {}@{}fps | audio={}",
            device.name, device.ip, device.port,
            device.resolution, device.fps, device.has_audio,
        )
        if self.auto_connect and not self._state.connected:
            logger.info("Auto-connecting to {}...", device.name)
            self._handle_connect(
                ip=device.ip,
                port=device.port,
                device_name=device.name,
            )

    def _on_device_lost(self, device) -> None:
        logger.warning("📷 Sender lost: {}", device.name)
        if self._state.connected and self._state.sender_name == device.name:
            logger.warning("Active stream device lost — waiting for reconnect...")
            # Ingestor handles reconnection internally via backoff

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    def _handle_connect(self, ip: str, port: int, device_name: str = "") -> None:
        """Connect to a specific sender device."""
        if self._state.connected:
            logger.info("Already connected — disconnecting first.")
            self._handle_disconnect()

        from receiver.ingest import StreamIngestor
        logger.info("Connecting to SRT stream at {}:{}...", ip, port)

        self._ingestor = StreamIngestor(
            ip=ip, port=port, fps=self.fps,
            resolution=(self.width, self.height),
            has_audio=True,
        )
        self._ingestor.start()

        self._state.connected = True
        self._state.sender_ip = ip
        self._state.sender_port = port
        self._state.sender_name = device_name
        self._state.resolution = f"{self.width}x{self.height}"
        self._state.fps = self.fps
        self._state.has_audio = True

        # Start the frame pump thread
        self._pump_thread = threading.Thread(
            target=self._frame_pump_loop,
            name="CamNetFramePump",
            daemon=True,
        )
        self._pump_thread.start()
        logger.success("Connected to {} ({}:{}).", device_name, ip, port)

    def _handle_disconnect(self) -> None:
        """Disconnect from the current stream."""
        if self._ingestor:
            self._ingestor.stop()
            self._ingestor = None

        self._state.connected = False
        self._state.sender_ip = ""
        self._state.sender_port = 0
        self._state.sender_name = ""
        logger.info("Disconnected from stream.")

    # ------------------------------------------------------------------
    # Frame pump loop
    # ------------------------------------------------------------------

    def _frame_pump_loop(self) -> None:
        """
        Continuously reads decoded frames from the ingestor and writes
        them into the platform frame writer (SHM / V4L2).
        """
        logger.info("Frame pump started.")
        ingestor = self._ingestor
        writer = self._frame_writer

        while self._running and self._state.connected:
            try:
                # Blocking get with short timeout
                try:
                    video_frame = ingestor.video_queue.get(timeout=0.5)
                except Exception:
                    continue

                # Try to get matching audio chunk (non-blocking)
                audio_chunk = b""
                try:
                    audio_chunk = ingestor.audio_queue.get_nowait()
                except Exception:
                    pass

                # Write to platform output
                write_start = time.perf_counter()
                ok = writer.write_frame(video_frame, audio_chunk)
                write_ms = (time.perf_counter() - write_start) * 1000

                # Update stats (only count as received if write succeeded)
                if ok:
                    self._state.frames_received += 1
                else:
                    self._state.frames_dropped += 1
                self._state.latency_ms = write_ms

                if self._state.frames_received % 300 == 0:
                    logger.debug(
                        "Pump: {} frames | write={:.2f}ms | dropped={}",
                        self._state.frames_received,
                        write_ms,
                        ingestor.stats.frames_dropped,
                    )

            except Exception as exc:
                logger.error("Frame pump error: {}", exc)
                time.sleep(0.1)

        logger.info("Frame pump exited.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_devices_list(self) -> list[dict]:
        """Return discovered devices as JSON-serializable list."""
        from dataclasses import asdict
        return [
            {
                "name": d.name,
                "ip": d.ip,
                "port": d.port,
                "resolution": d.resolution,
                "fps": d.fps,
                "has_audio": d.has_audio,
                "last_seen": d.last_seen,
            }
            for d in self._discovery.get_devices()
        ]

    def _start_tray(self) -> None:
        """Launch system tray icon."""
        try:
            import pystray
            from PIL import Image, ImageDraw

            def make_icon(connected: bool = False) -> Image.Image:
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                color = (0, 200, 80) if connected else (220, 80, 40)
                # Monitor shape
                d.rectangle([4, 8, 60, 46], fill=(30, 30, 60), outline=color, width=3)
                # Screen content indicator
                d.rectangle([10, 14, 54, 40], fill=color)
                # Stand
                d.rectangle([24, 46, 40, 52], fill=(30, 30, 60))
                d.rectangle([16, 52, 48, 56], fill=(30, 30, 60))
                return img

            state = self._state

            def on_quit(icon, _):
                icon.stop()
                self.stop()

            def on_connect(icon, _):
                devices = self._discovery.get_devices()
                if devices:
                    d = devices[0]
                    self._handle_connect(d.ip, d.port, d.name)
                else:
                    logger.warning("No devices discovered yet.")

            def on_disconnect(icon, _):
                self._handle_disconnect()

            icon = pystray.Icon(
                "CamNet Receiver",
                make_icon(False),
                "CamNet Receiver",
                menu=pystray.Menu(
                    pystray.MenuItem("CamNet Receiver", None, enabled=False),
                    pystray.MenuItem("Connect to first device", on_connect),
                    pystray.MenuItem("Disconnect", on_disconnect),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Open API Dashboard", lambda *_: (
                        __import__("webbrowser").open("http://localhost:7432/status")
                    )),
                    pystray.MenuItem("Quit", on_quit),
                ),
            )

            self._tray = icon
            threading.Thread(
                target=icon.run,
                name="CamNetTray",
                daemon=True,
            ).start()
            logger.info("System tray started.")

        except ImportError:
            logger.warning("pystray/Pillow not available — no system tray.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--auto-connect/--no-auto-connect", default=True, show_default=True,
              help="Auto-connect to the first discovered sender.")
@click.option("--resolution", "-r", default="1920x1080", show_default=True,
              callback=lambda ctx, param, v: tuple(int(x) for x in v.lower().split("x")),
              help="Capture resolution WxH.")
@click.option("--fps", "-f", default=60, type=int, show_default=True,
              help="Target frame rate.")
@click.option("--no-tray", is_flag=True, default=False,
              help="Disable system tray icon.")
@click.option("--v4l2-device", default="", show_default="auto-detect",
              help="[Linux] V4L2 loopback device path (e.g. /dev/video2).")
@click.option("--enable-shm/--disable-shm", default=True, show_default=True,
              help="[Linux] Enable POSIX SHM mirror alongside V4L2 output.")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable verbose debug logging.")
def cli(
    auto_connect: bool,
    resolution: tuple,
    fps: int,
    no_tray: bool,
    v4l2_device: str,
    enable_shm: bool,
    verbose: bool,
) -> None:
    """CamNet Receiver — connect a network camera to OBS Studio (Windows / Linux)."""
    if sys.platform not in ("win32", "linux"):
        logger.error(
            "CamNet Receiver requires Windows or Linux. "
            f"Current platform: {sys.platform}"
        )
        sys.exit(1)

    if verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG", colorize=True)

    width, height = resolution
    app = CamNetReceiver(
        auto_connect=auto_connect,
        width=width,
        height=height,
        fps=fps,
        no_tray=no_tray,
        v4l2_device=v4l2_device,
        enable_shm=enable_shm,
    )

    def handle_signal(sig, frame):
        app.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app.start()
    app.wait()


if __name__ == "__main__":
    cli()
