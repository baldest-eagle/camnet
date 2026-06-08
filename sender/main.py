"""
sender/main.py — CamNet Sender entry point.

Orchestrates camera capture, SRT streaming, mDNS announcement,
and an optional system tray icon for headless operation.

Usage:
    py main.py [--device 0] [--resolution 1920x1080] [--fps 60]
               [--port 9000] [--no-audio] [--no-tray]
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

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
    Path(__file__).parent / "logs" / "sender_{time:YYYY-MM-DD}.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
)


class CamNetSender:
    """
    Top-level orchestrator for the CamNet Sender application.

    Manages lifecycle of:
    - Camera capture
    - SRT streaming
    - mDNS announcement
    - Optional system tray
    """

    def __init__(
        self,
        device_index: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 60,
        srt_port: int = 9000,
        has_audio: bool = True,
        no_tray: bool = False,
    ) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.srt_port = srt_port
        self.has_audio = has_audio
        self.no_tray = no_tray

        self._running = False
        self._streamer = None
        self._announcer = None
        self._tray = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start all sender components."""
        logger.info(
            "CamNet Sender starting | device={} res={}x{} fps={} port={} audio={}",
            self.device_index, self.width, self.height,
            self.fps, self.srt_port, self.has_audio,
        )

        # 1. Start SRT streamer (captures camera + streams)
        from streamer import SRTStreamer, StreamerConfig
        stream_config = StreamerConfig(
            srt_port=self.srt_port,
            width=self.width,
            height=self.height,
            fps=self.fps,
        )
        self._streamer = SRTStreamer(stream_config, device_index=self.device_index)
        self._streamer.start()

        # 2. Announce via mDNS
        from discovery import DiscoveryAnnouncer
        self._announcer = DiscoveryAnnouncer(
            srt_port=self.srt_port,
            resolution=(self.width, self.height),
            fps=self.fps,
            has_audio=self.has_audio,
        )
        self._announcer.start()

        self._running = True
        logger.success(
            "CamNet Sender ready! Streaming on SRT port {}. "
            "Waiting for receiver to connect...",
            self.srt_port,
        )

        # 3. Start system tray (optional)
        if not self.no_tray:
            self._start_tray()

    def stop(self) -> None:
        """Stop all sender components gracefully."""
        logger.info("CamNet Sender stopping...")
        self._running = False

        if self._announcer:
            self._announcer.stop()

        if self._streamer:
            self._streamer.stop()

        if self._tray:
            self._tray.stop()

        self._stop_event.set()
        logger.success("CamNet Sender stopped.")

    def wait(self) -> None:
        """Block until stop() is called."""
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            self.stop()

    def _start_tray(self) -> None:
        """Start the system tray icon in a daemon thread."""
        try:
            import pystray
            from PIL import Image, ImageDraw

            def make_icon() -> Image.Image:
                """Generate a simple camera icon."""
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                # Camera body
                d.rectangle([8, 18, 56, 48], fill=(40, 120, 220), outline=(20, 80, 180), width=2)
                # Lens
                d.ellipse([20, 22, 44, 44], fill=(10, 10, 40), outline=(100, 180, 255), width=2)
                # Viewfinder bump
                d.rectangle([24, 12, 40, 20], fill=(40, 120, 220))
                return img

            def on_quit(icon, _item):
                icon.stop()
                self.stop()

            def on_status(icon, _item):
                alive = self._streamer.is_alive if self._streamer else False
                logger.info("Stream alive: {}", alive)

            icon = pystray.Icon(
                "CamNet Sender",
                make_icon(),
                "CamNet Sender",
                menu=pystray.Menu(
                    pystray.MenuItem("CamNet Sender", None, enabled=False),
                    pystray.MenuItem(
                        f"Streaming on port {self.srt_port}",
                        None, enabled=False
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Status", on_status),
                    pystray.MenuItem("Quit", on_quit),
                ),
            )
            self._tray = icon
            threading.Thread(
                target=icon.run,
                name="CamNetTray",
                daemon=True,
            ).start()
            logger.info("System tray icon started.")

        except ImportError:
            logger.warning(
                "pystray/Pillow not installed — running without system tray."
            )


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def _parse_resolution(ctx, param, value: str) -> tuple[int, int]:
    """Parse '1920x1080' → (1920, 1080)."""
    try:
        w, h = value.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise click.BadParameter(f"Expected WxH format, got: {value}")


@click.command()
@click.option(
    "--device", "-d", default=0, type=int,
    show_default=True, help="Camera device index."
)
@click.option(
    "--resolution", "-r", default="1920x1080",
    callback=_parse_resolution, is_eager=True,
    show_default=True, help="Capture resolution WxH."
)
@click.option(
    "--fps", "-f", default=60, type=int,
    show_default=True, help="Target frame rate."
)
@click.option(
    "--port", "-p", default=9000, type=int,
    show_default=True, help="SRT listener port."
)
@click.option(
    "--no-audio", is_flag=True, default=False,
    help="Disable audio capture and streaming."
)
@click.option(
    "--no-tray", is_flag=True, default=False,
    help="Disable system tray icon."
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Enable verbose debug logging."
)
def cli(
    device: int,
    resolution: tuple[int, int],
    fps: int,
    port: int,
    no_audio: bool,
    no_tray: bool,
    verbose: bool,
) -> None:
    """CamNet Sender — stream this device's camera to OBS on your primary PC."""
    if verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG", colorize=True)

    width, height = resolution

    # Register signal handlers for graceful shutdown
    app = CamNetSender(
        device_index=device,
        width=width,
        height=height,
        fps=fps,
        srt_port=port,
        has_audio=not no_audio,
        no_tray=no_tray,
    )

    def handle_signal(sig, frame):
        logger.info("Received signal {}, shutting down...", sig)
        app.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app.start()
    app.wait()


if __name__ == "__main__":
    cli()
