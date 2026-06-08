"""
CamNet Discovery — Shared pytest Fixtures
==========================================
Provides reusable fixtures for discovery round-trip tests.

Fixtures:
    announcer  — live DiscoveryAnnouncer (started, stopped on teardown)
    discovery  — live CamNetDiscovery    (started, stopped on teardown)
    tmp_shm_name — unique shared-memory segment name for test isolation
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator

import pytest
from loguru import logger

# Adjust the imports to the actual package paths in the project.
# When pytest is run from the camnet/ root the sender/ and receiver/
# directories are added to sys.path via conftest.py (this file), so plain
# module names work.
import sys
import os

# Ensure sender/ and receiver/ are importable when running pytest from
# the camnet/ root or from the tests/ directory.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SENDER_DIR = os.path.join(_PROJECT_ROOT, "sender")
_RECEIVER_DIR = os.path.join(_PROJECT_ROOT, "receiver")

for _p in (_PROJECT_ROOT, _SENDER_DIR, _RECEIVER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from discovery import CamNetDiscovery, DeviceRegistry  # noqa: E402  (receiver)

# We import sender's discovery under an alias to avoid name collision.
import importlib.util as _ilu
import types as _types


def _load_sender_discovery() -> _types.ModuleType:
    spec = _ilu.spec_from_file_location(
        "sender_discovery", os.path.join(_SENDER_DIR, "discovery.py")
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_sender_mod = _load_sender_discovery()
DiscoveryAnnouncer = _sender_mod.DiscoveryAnnouncer


# ---------------------------------------------------------------------------
# Configure loguru for pytest output
# ---------------------------------------------------------------------------

logger.remove()
logger.add(
    sink=lambda msg: print(msg, end=""),
    level="DEBUG",
    colorize=False,
    format="{time:HH:mm:ss} | {level:<8} | {message}",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def announcer() -> Generator[DiscoveryAnnouncer, None, None]:
    """Create, start, and (after the test) cleanly stop a :class:`DiscoveryAnnouncer`.

    Uses default parameters (port 9000, 1920×1080, 60 fps, audio enabled).
    Tests that need different parameters should create their own announcer
    instance manually.
    """
    ann = DiscoveryAnnouncer(
        srt_port=9000,
        resolution=(1920, 1080),
        fps=60,
        has_audio=True,
    )
    ann.start()
    # Give mDNS a moment to propagate before the test body runs.
    time.sleep(0.25)
    try:
        yield ann
    finally:
        if ann.is_running:
            ann.stop()
        # Extra settling time so the next test starts with a clean slate.
        time.sleep(0.25)


@pytest.fixture()
def discovery() -> Generator[CamNetDiscovery, None, None]:
    """Create, start, and (after the test) cleanly stop a :class:`CamNetDiscovery`.

    The fixture does **not** register any callbacks; individual tests are
    responsible for attaching their own ``on_device_found`` / ``on_device_lost``
    handlers before or after yielding.
    """
    disc = CamNetDiscovery()
    disc.start()
    try:
        yield disc
    finally:
        disc.stop()


@pytest.fixture()
def tmp_shm_name() -> str:
    """Return a unique shared-memory segment name for test isolation.

    The name is guaranteed to be unique within the test session and safe to use
    as a POSIX shared-memory identifier (no leading slash; caller should add one
    if the target platform requires it).

    Example::

        def test_something(tmp_shm_name):
            shm = shared_memory.SharedMemory(name=tmp_shm_name, create=True, size=1024)
    """
    return f"camnet_test_{uuid.uuid4().hex[:12]}"
