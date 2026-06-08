"""
CamNet Receiver — mDNS Service Browser & Device Registry
=========================================================
Continuously browses the local network for `_camnet._tcp.local.` services
announced by CamNet senders, maintaining a thread-safe live registry of
discovered devices.

Requires: zeroconf>=0.132.2, loguru>=0.7.2
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger
from zeroconf import ServiceBrowser, ServiceListener, ServiceStateChange, Zeroconf

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_TYPE = "_camnet._tcp.local."

# Maximum age (seconds) before a device is considered stale in get_devices().
DEVICE_STALE_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DeviceRegistry:
    """Holds all discovered information about a single CamNet sender."""

    name: str
    """The raw mDNS service name, e.g. ``CamNet-mymachine._camnet._tcp.local.``"""

    ip: str
    """IPv4 address of the sender host."""

    port: int
    """SRT streaming port advertised by the sender."""

    resolution: str
    """Resolution string, e.g. ``1920x1080``."""

    fps: int
    """Frames-per-second advertised by the sender."""

    has_audio: bool
    """Whether the sender is streaming audio."""

    protocol: str
    """Transport protocol — typically ``srt``."""

    version: str
    """CamNet protocol version string."""

    device_name: str
    """Human-readable device / hostname."""

    last_seen: float = field(default_factory=time.time)
    """Unix timestamp of when this record was last refreshed."""

    def is_stale(self, max_age: float = DEVICE_STALE_SECONDS) -> bool:
        """Return ``True`` if the record has not been refreshed recently."""
        return (time.time() - self.last_seen) > max_age

    def __str__(self) -> str:
        return (
            f"DeviceRegistry(name={self.device_name!r}, ip={self.ip}, port={self.port}, "
            f"res={self.resolution}, fps={self.fps}, audio={self.has_audio}, "
            f"protocol={self.protocol}, version={self.version})"
        )


# ---------------------------------------------------------------------------
# Internal zeroconf listener
# ---------------------------------------------------------------------------


class _CamNetListener(ServiceListener):
    """Internal :class:`ServiceListener` that feeds events into :class:`CamNetDiscovery`."""

    def __init__(self, registry: CamNetDiscovery) -> None:
        self._registry = registry

    # zeroconf calls these with positional keyword args, so we must accept them:

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
        self._registry._handle_service_change(zc, type_, name, ServiceStateChange.Added)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
        self._registry._handle_service_change(zc, type_, name, ServiceStateChange.Removed)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
        self._registry._handle_service_change(zc, type_, name, ServiceStateChange.Updated)


# ---------------------------------------------------------------------------
# Main discovery class
# ---------------------------------------------------------------------------


class CamNetDiscovery:
    """Browse the local network for CamNet senders and maintain a live registry.

    Usage::

        discovery = CamNetDiscovery()
        discovery.on_device_found(lambda dev: print("Found:", dev))
        discovery.on_device_lost(lambda dev: print("Lost:", dev))
        discovery.start()

        # later …
        devices = discovery.get_devices()
        discovery.stop()
    """

    def __init__(self) -> None:
        self._devices: dict[str, DeviceRegistry] = {}
        self._lock = threading.RLock()

        self._found_callbacks: list[Callable[[DeviceRegistry], None]] = []
        self._lost_callbacks: list[Callable[[DeviceRegistry], None]] = []

        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._running = False

        logger.debug("CamNetDiscovery initialised.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start browsing the network for CamNet services."""
        if self._running:
            logger.warning("CamNetDiscovery is already running — ignoring start()")
            return

        self._zeroconf = Zeroconf()
        listener = _CamNetListener(self)
        self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, listener)
        self._running = True
        logger.success("CamNetDiscovery started — browsing for '{}'", SERVICE_TYPE)

    def stop(self) -> None:
        """Stop browsing and release all resources."""
        if not self._running:
            logger.debug("CamNetDiscovery.stop() called but not running — no-op")
            return

        logger.info("Stopping CamNetDiscovery...")
        try:
            if self._zeroconf is not None:
                self._zeroconf.close()
        except Exception as exc:  # pragma: no cover
            logger.error("Error closing Zeroconf: {}", exc)
        finally:
            self._zeroconf = None
            self._browser = None
            self._running = False
            logger.success("CamNetDiscovery stopped.")

    # ------------------------------------------------------------------
    # Device access
    # ------------------------------------------------------------------

    def get_devices(self) -> list[DeviceRegistry]:
        """Return the list of currently known (non-stale) CamNet devices."""
        with self._lock:
            return [dev for dev in self._devices.values() if not dev.is_stale()]

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_device_found(self, callback: Callable[[DeviceRegistry], None]) -> None:
        """Register *callback* to be called whenever a new device is discovered.

        The callback receives the :class:`DeviceRegistry` instance and is
        invoked from a background thread — ensure thread-safety.
        """
        self._found_callbacks.append(callback)

    def on_device_lost(self, callback: Callable[[DeviceRegistry], None]) -> None:
        """Register *callback* to be called whenever a device disappears.

        The callback receives the last-known :class:`DeviceRegistry` snapshot.
        """
        self._lost_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_service_change(
        self,
        zc: Zeroconf,
        type_: str,
        name: str,
        change: ServiceStateChange,
    ) -> None:
        """Dispatch a mDNS service event to the appropriate handler."""
        if change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            self._on_service_added(zc, type_, name)
        elif change == ServiceStateChange.Removed:
            self._on_service_removed(name)

    def _on_service_added(self, zc: Zeroconf, type_: str, name: str) -> None:
        """Resolve service info and add/update the registry."""
        info = zc.get_service_info(type_, name)
        if info is None:
            logger.warning("Could not resolve service info for '{}' — skipping", name)
            return

        # Decode TXT properties (values are bytes)
        props: dict[str, str] = {}
        for k, v in info.properties.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else str(v)
            props[key] = val

        # Resolve the IPv4 address
        addresses = info.parsed_scoped_addresses()
        if not addresses:
            # Fallback via hostname resolution
            try:
                ip = socket.gethostbyname(info.server or name)
            except socket.gaierror:
                ip = "0.0.0.0"
        else:
            ip = addresses[0]

        device = DeviceRegistry(
            name=name,
            ip=ip,
            port=info.port,
            resolution=props.get("resolution", "unknown"),
            fps=_safe_int(props.get("fps", "0")),
            has_audio=props.get("audio", "false").lower() == "true",
            protocol=props.get("protocol", "srt"),
            version=props.get("version", ""),
            device_name=props.get("device_name", name),
            last_seen=time.time(),
        )

        is_new: bool
        with self._lock:
            is_new = name not in self._devices
            self._devices[name] = device

        if is_new:
            logger.success("New CamNet sender discovered: {}", device)
            for cb in self._found_callbacks:
                _invoke_callback(cb, device)
        else:
            logger.debug("Updated existing CamNet sender: {}", device)

    def _on_service_removed(self, name: str) -> None:
        """Remove a device from the registry and fire lost-callbacks."""
        with self._lock:
            device = self._devices.pop(name, None)

        if device is not None:
            logger.info("CamNet sender gone: {}", device)
            for cb in self._lost_callbacks:
                _invoke_callback(cb, device)
        else:
            logger.debug("Received remove event for unknown service '{}' — ignoring", name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the browser is active."""
        return self._running

    @property
    def device_count(self) -> int:
        """Return the number of currently known devices (may include stale)."""
        with self._lock:
            return len(self._devices)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _safe_int(value: str, default: int = 0) -> int:
    """Parse *value* as an integer, returning *default* on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _invoke_callback(cb: Callable[[DeviceRegistry], None], device: DeviceRegistry) -> None:
    """Call *cb* in a daemon thread to avoid blocking the zeroconf event loop."""
    t = threading.Thread(target=cb, args=(device,), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import time

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )

    disc = CamNetDiscovery()

    def _on_found(dev: DeviceRegistry) -> None:
        print(f"\n[FOUND]  {dev}")

    def _on_lost(dev: DeviceRegistry) -> None:
        print(f"\n[LOST]   {dev.device_name} @ {dev.ip}")

    disc.on_device_found(_on_found)
    disc.on_device_lost(_on_lost)
    disc.start()

    logger.info("Browsing for CamNet senders — press Ctrl+C to stop.")
    try:
        while True:
            devices = disc.get_devices()
            if devices:
                logger.info("{} device(s) currently visible:", len(devices))
                for d in devices:
                    logger.info("  • {}", d)
            else:
                logger.info("No devices found yet...")
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        disc.stop()

    sys.exit(0)
