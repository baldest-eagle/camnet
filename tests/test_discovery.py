"""
CamNet Discovery — Unit & Integration Tests
============================================
Tests the full mDNS announcement / discovery round-trip using real zeroconf
on the loopback interface so no mocking of network I/O is required.

Run with::

    cd camnet/
    pytest tests/test_discovery.py -v

Requirements: pytest, pytest-asyncio, zeroconf, loguru
"""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import pytest
from loguru import logger

# ---------------------------------------------------------------------------
# conftest provides `announcer` and `discovery` fixtures; we import the
# concrete types here only for type annotations.
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    from conftest import CamNetDiscovery, DeviceRegistry, DiscoveryAnnouncer

# Re-import module-level names used directly in tests (conftest already
# arranged sys.path so these are resolvable).
import importlib.util as _ilu
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (
    os.path.join(_PROJECT_ROOT, "sender"),
    os.path.join(_PROJECT_ROOT, "receiver"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Receiver discovery module (already on sys.path as "discovery" from receiver/)
from discovery import CamNetDiscovery, DeviceRegistry  # noqa: E402


def _load_sender_mod():
    spec = _ilu.spec_from_file_location(
        "sender_discovery",
        os.path.join(_PROJECT_ROOT, "sender", "discovery.py"),
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_sender_mod = _load_sender_mod()
DiscoveryAnnouncer = _sender_mod.DiscoveryAnnouncer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WAIT_TIMEOUT = 5.0   # seconds to wait for mDNS events in integration tests
_POLL_INTERVAL = 0.1  # polling granularity while waiting


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for(condition_fn, timeout: float = _WAIT_TIMEOUT, poll: float = _POLL_INTERVAL) -> bool:
    """Busy-poll *condition_fn* until it returns truthy or *timeout* expires.

    Returns ``True`` if the condition was satisfied, ``False`` on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# Test 1 — Announcer registers without error
# ---------------------------------------------------------------------------


class TestAnnouncerRegisters:
    """Verify that :class:`DiscoveryAnnouncer` can start and stop cleanly."""

    def test_announcer_registers(self) -> None:
        """Starting an announcer should succeed and report is_running=True."""
        ann = DiscoveryAnnouncer(srt_port=9001, resolution=(1280, 720), fps=30, has_audio=False)

        assert not ann.is_running, "Should not be running before start()"
        ann.start()
        try:
            assert ann.is_running, "Should be running after start()"
            assert ann.service_name is not None, "service_name should be set while running"
            assert ann.service_name.endswith("._camnet._tcp.local."), (
                f"Service name format unexpected: {ann.service_name!r}"
            )
        finally:
            ann.stop()

        assert not ann.is_running, "Should not be running after stop()"
        assert ann.service_name is None, "service_name should be None after stop()"

    def test_double_start_is_idempotent(self) -> None:
        """Calling start() twice should not raise and leave the service running."""
        ann = DiscoveryAnnouncer(srt_port=9002)
        ann.start()
        try:
            ann.start()  # second call — should be a no-op
            assert ann.is_running
        finally:
            ann.stop()

    def test_double_stop_is_idempotent(self) -> None:
        """Calling stop() twice should not raise."""
        ann = DiscoveryAnnouncer(srt_port=9003)
        ann.start()
        ann.stop()
        ann.stop()  # second call — must be safe
        assert not ann.is_running


# ---------------------------------------------------------------------------
# Test 2 — Discovery finds announcer
# ---------------------------------------------------------------------------


class TestDiscoveryFindsAnnouncer:
    """Integration: browser discovers a running announcer within the timeout."""

    def test_discovery_finds_announcer(self, announcer: "DiscoveryAnnouncer") -> None:
        """A freshly started :class:`CamNetDiscovery` should detect the announcer."""
        found_event = threading.Event()
        found_devices: list[DeviceRegistry] = []

        disc = CamNetDiscovery()

        def _on_found(dev: DeviceRegistry) -> None:
            found_devices.append(dev)
            found_event.set()

        disc.on_device_found(_on_found)
        disc.start()

        try:
            succeeded = found_event.wait(timeout=_WAIT_TIMEOUT)
            assert succeeded, (
                f"CamNetDiscovery did not discover the announcer within {_WAIT_TIMEOUT}s"
            )
            assert len(found_devices) >= 1
            dev = found_devices[0]
            assert dev.ip != ""
            assert dev.port == 9000
        finally:
            disc.stop()

    def test_get_devices_returns_discovered_device(self, announcer: "DiscoveryAnnouncer") -> None:
        """get_devices() must include the announced device once it's been found."""
        disc = CamNetDiscovery()
        disc.start()

        try:
            satisfied = _wait_for(lambda: len(disc.get_devices()) > 0)
            assert satisfied, "get_devices() returned empty list after timeout"

            devices = disc.get_devices()
            assert len(devices) >= 1

            names = [d.device_name for d in devices]
            expected_hostname = socket.gethostname()
            assert any(expected_hostname in n for n in names), (
                f"Expected hostname {expected_hostname!r} not found in discovered devices: {names}"
            )
        finally:
            disc.stop()


# ---------------------------------------------------------------------------
# Test 3 — Device removed on announcer stop
# ---------------------------------------------------------------------------


class TestDeviceRemovedOnStop:
    """Integration: once the announcer stops, the discovery registry removes it."""

    def test_device_removed_on_stop(self) -> None:
        """Device entry must disappear from get_devices() after announcer stops."""
        ann = DiscoveryAnnouncer(srt_port=9004, has_audio=False)
        ann.start()

        disc = CamNetDiscovery()
        lost_event = threading.Event()
        lost_devices: list[DeviceRegistry] = []

        def _on_lost(dev: DeviceRegistry) -> None:
            lost_devices.append(dev)
            lost_event.set()

        disc.on_device_lost(_on_lost)
        disc.start()

        try:
            # Wait until the device is found first.
            appeared = _wait_for(lambda: disc.device_count > 0)
            assert appeared, "Device never appeared — cannot test removal"

            # Now stop the announcer.
            ann.stop()

            # The browser should report the device as removed.
            removed = lost_event.wait(timeout=_WAIT_TIMEOUT)
            assert removed, (
                f"on_device_lost callback was not invoked within {_WAIT_TIMEOUT}s after stop"
            )

            # get_devices() should no longer include it.
            remaining = disc.get_devices()
            assert all(d.port != 9004 for d in remaining), (
                "Stopped device still appears in get_devices()"
            )
        finally:
            disc.stop()
            if ann.is_running:
                ann.stop()


# ---------------------------------------------------------------------------
# Test 4 — TXT record properties are correct
# ---------------------------------------------------------------------------


class TestTxtRecordProperties:
    """Verify that all expected TXT record fields are present and well-formed."""

    def test_txt_record_properties(self, announcer: "DiscoveryAnnouncer") -> None:
        """All TXT properties declared in the spec must appear in the discovered device."""
        disc = CamNetDiscovery()
        found_devices: list[DeviceRegistry] = []
        found_event = threading.Event()

        def _on_found(dev: DeviceRegistry) -> None:
            found_devices.append(dev)
            found_event.set()

        disc.on_device_found(_on_found)
        disc.start()

        try:
            ok = found_event.wait(timeout=_WAIT_TIMEOUT)
            assert ok, "No device discovered — cannot check TXT records"

            dev = found_devices[0]

            # version
            assert dev.version == "1.0", f"version mismatch: {dev.version!r}"

            # device_name
            expected_hostname = socket.gethostname()
            assert dev.device_name == expected_hostname, (
                f"device_name: expected {expected_hostname!r}, got {dev.device_name!r}"
            )

            # resolution
            assert dev.resolution == "1920x1080", (
                f"resolution mismatch: {dev.resolution!r}"
            )

            # fps
            assert dev.fps == 60, f"fps mismatch: {dev.fps}"

            # srt_port (reflected in the port field)
            assert dev.port == 9000, f"port mismatch: {dev.port}"

            # audio
            assert dev.has_audio is True, "has_audio should be True"

            # protocol
            assert dev.protocol == "srt", f"protocol mismatch: {dev.protocol!r}"

            # ip must be a valid IPv4 address
            try:
                socket.inet_aton(dev.ip)
            except OSError:
                pytest.fail(f"ip {dev.ip!r} is not a valid IPv4 address")

            # last_seen should be recent
            assert (time.time() - dev.last_seen) < 10, "last_seen timestamp is too old"

        finally:
            disc.stop()

    def test_update_info_changes_properties(self) -> None:
        """update_info() must re-register the service with new values."""
        ann = DiscoveryAnnouncer(srt_port=9005, resolution=(1280, 720), fps=30, has_audio=False)
        ann.start()

        disc = CamNetDiscovery()
        devices_seen: list[DeviceRegistry] = []
        event = threading.Event()

        def _on_found(dev: DeviceRegistry) -> None:
            devices_seen.append(dev)
            event.set()

        disc.on_device_found(_on_found)
        disc.start()

        try:
            # Wait for initial announcement
            ok = event.wait(timeout=_WAIT_TIMEOUT)
            assert ok, "Initial device not discovered"
            first = devices_seen[0]
            assert first.fps == 30

            # Reset and re-discover after update
            event.clear()
            devices_seen.clear()

            ann.update_info(fps=60, resolution=(1920, 1080))

            # Allow time for the updated registration to propagate
            time.sleep(1.0)

            # Re-query get_devices — the updated record should appear
            # We give it a bit more time via polling.
            def _updated_fps_seen() -> bool:
                return any(d.fps == 60 for d in disc.get_devices())

            updated = _wait_for(_updated_fps_seen, timeout=_WAIT_TIMEOUT)
            assert updated, "Updated fps=60 not reflected in discovery within timeout"
        finally:
            disc.stop()
            if ann.is_running:
                ann.stop()
