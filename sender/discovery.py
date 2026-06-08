"""
CamNet Sender — mDNS Service Announcer
=======================================
Registers a `_camnet._tcp.local.` mDNS service so that any CamNet receiver on
the same LAN can auto-discover this sender without manual IP configuration.

Requires: zeroconf>=0.132.2, loguru>=0.7.2
"""

from __future__ import annotations

import signal
import socket
import threading
import time
from typing import Any

from loguru import logger
from zeroconf import IPVersion, ServiceInfo, Zeroconf

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_TYPE = "_camnet._tcp.local."
SERVICE_VERSION = "1.0"
PROTOCOL = "srt"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _local_ip() -> str:
    """Return the primary non-loopback IPv4 address of this machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            # Doesn't need to be reachable; we just need the routing decision.
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _build_service_info(
    hostname: str,
    ip: str,
    srt_port: int,
    resolution: tuple[int, int],
    fps: int,
    has_audio: bool,
) -> ServiceInfo:
    """Construct a :class:`ServiceInfo` object from the given parameters."""
    service_name = f"CamNet-{hostname}.{SERVICE_TYPE}"
    res_str = f"{resolution[0]}x{resolution[1]}"

    properties: dict[str, str] = {
        "version": SERVICE_VERSION,
        "device_name": hostname,
        "resolution": res_str,
        "fps": str(fps),
        "srt_port": str(srt_port),
        "audio": "true" if has_audio else "false",
        "protocol": PROTOCOL,
    }

    # zeroconf expects property values as bytes
    txt_records = {k: v.encode() for k, v in properties.items()}

    packed_ip = socket.inet_aton(ip)

    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=service_name,
        addresses=[packed_ip],
        port=srt_port,
        properties=txt_records,
        server=f"{hostname}.local.",
    )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class DiscoveryAnnouncer:
    """Announces this CamNet sender via mDNS so receivers can discover it.

    Usage::

        announcer = DiscoveryAnnouncer(srt_port=9000, resolution=(1920, 1080), fps=60)
        announcer.start()
        # ... run your sender pipeline ...
        announcer.stop()
    """

    def __init__(
        self,
        srt_port: int = 9000,
        resolution: tuple[int, int] = (1920, 1080),
        fps: int = 60,
        has_audio: bool = True,
    ) -> None:
        self._srt_port = srt_port
        self._resolution = resolution
        self._fps = fps
        self._has_audio = has_audio

        self._hostname: str = socket.gethostname()
        self._ip: str = _local_ip()

        self._zeroconf: Zeroconf | None = None
        self._service_info: ServiceInfo | None = None
        self._lock = threading.Lock()
        self._running = False

        logger.debug(
            "DiscoveryAnnouncer initialised: host={} ip={} port={} res={}x{} fps={} audio={}",
            self._hostname,
            self._ip,
            self._srt_port,
            *self._resolution,
            self._fps,
            self._has_audio,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register the mDNS service on the local network."""
        with self._lock:
            if self._running:
                logger.warning("DiscoveryAnnouncer is already running — ignoring start()")
                return

            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            self._service_info = _build_service_info(
                hostname=self._hostname,
                ip=self._ip,
                srt_port=self._srt_port,
                resolution=self._resolution,
                fps=self._fps,
                has_audio=self._has_audio,
            )

            logger.info(
                "Registering mDNS service '{}' at {}:{} ...",
                self._service_info.name,
                self._ip,
                self._srt_port,
            )
            self._zeroconf.register_service(self._service_info)
            self._running = True
            logger.success(
                "mDNS service registered: {} → {}:{}",
                self._service_info.name,
                self._ip,
                self._srt_port,
            )

    def stop(self) -> None:
        """Unregister the mDNS service and release resources."""
        with self._lock:
            if not self._running:
                logger.debug("DiscoveryAnnouncer.stop() called but not running — no-op")
                return

            logger.info("Unregistering mDNS service '{}'...", self._service_info.name)  # type: ignore[union-attr]
            try:
                self._zeroconf.unregister_service(self._service_info)  # type: ignore[union-attr]
            except Exception as exc:  # pragma: no cover
                logger.error("Error during mDNS unregister: {}", exc)
            finally:
                self._zeroconf.close()  # type: ignore[union-attr]
                self._zeroconf = None
                self._service_info = None
                self._running = False
                logger.success("mDNS service unregistered cleanly.")

    def update_info(self, **kwargs: Any) -> None:
        """Re-register the service with updated properties.

        Accepted keyword arguments: ``srt_port``, ``resolution``,
        ``fps``, ``has_audio``.  Any unrecognised keys are silently ignored
        to keep the API forward-compatible.
        """
        updated = False

        if "srt_port" in kwargs:
            self._srt_port = int(kwargs["srt_port"])
            updated = True
        if "resolution" in kwargs:
            self._resolution = tuple(kwargs["resolution"])  # type: ignore[assignment]
            updated = True
        if "fps" in kwargs:
            self._fps = int(kwargs["fps"])
            updated = True
        if "has_audio" in kwargs:
            self._has_audio = bool(kwargs["has_audio"])
            updated = True

        if not updated:
            logger.debug("update_info called with no recognised fields — no-op")
            return

        logger.info("Updating mDNS announcement with new properties: {}", kwargs)
        was_running = self._running
        if was_running:
            self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the service is currently registered."""
        return self._running

    @property
    def service_name(self) -> str | None:
        """Return the full mDNS service name, or ``None`` if not running."""
        if self._service_info is not None:
            return self._service_info.name
        return None


# ---------------------------------------------------------------------------
# Signal handling helper
# ---------------------------------------------------------------------------


def _install_signal_handlers(announcer: DiscoveryAnnouncer, stop_event: threading.Event) -> None:
    """Install SIGINT / SIGTERM handlers that perform a clean shutdown."""

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        sig_name = signal.Signals(signum).name
        logger.info("Received {} — shutting down mDNS announcer...", sig_name)
        announcer.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="DEBUG", colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    stop_event = threading.Event()

    ann = DiscoveryAnnouncer(
        srt_port=9000,
        resolution=(1920, 1080),
        fps=60,
        has_audio=True,
    )

    _install_signal_handlers(ann, stop_event)

    ann.start()
    logger.info("Announcer running. Press Ctrl+C to stop.")

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if ann.is_running:
            ann.stop()

    logger.info("Exited cleanly.")
    sys.exit(0)
