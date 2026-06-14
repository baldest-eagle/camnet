"""
receiver/controller.py — REST API for CamNet Receiver.

Exposes a local HTTP API on port 7432 so the DirectShow filter (or any
external tool) can query stream status, select a camera device, and
retrieve the current shared memory name/offset layout.

Endpoints:
  GET  /status          - Overall receiver health and stream state
  GET  /health          - Lightweight liveness check (for monitoring)
  GET  /devices         - List discovered CamNet sender devices
  POST /connect         - Connect to a specific sender device
  POST /disconnect      - Disconnect from current stream
  GET  /shm_info        - Shared memory layout info (for C++ consumer)
  GET  /metrics         - Prometheus-style metrics
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from loguru import logger


@dataclass
class ReceiverState:
    """Mutable state shared between the REST controller and stream manager."""
    connected: bool = False
    sender_ip: str = ""
    sender_port: int = 0
    sender_name: str = ""
    resolution: str = ""
    fps: int = 0
    has_audio: bool = False
    frames_received: int = 0
    frames_dropped: int = 0
    reconnect_count: int = 0
    latency_ms: float = 0.0
    start_time: float = 0.0
    shm_name: str = ""
    shm_mutex_name: str = ""
    platform: str = ""          # "win32" or "linux"
    v4l2_device: str = ""       # Linux: /dev/videoN path


class ReceiverController:
    """
    Flask-based REST API controller for the CamNet Receiver.

    The DirectShow filter polls /shm_info once on startup to learn the
    shared memory name. Stream state is polled by the tray GUI.
    """

    API_PORT = 7432

    def __init__(self, state: ReceiverState) -> None:
        self.state = state
        self._app = Flask("CamNetReceiver")
        CORS(self._app)
        self._connect_callback = None
        self._disconnect_callback = None
        self._server_thread: Optional[threading.Thread] = None
        self._register_routes()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_connect_request(self, callback) -> None:
        """Register callback invoked when POST /connect is received."""
        self._connect_callback = callback

    def on_disconnect_request(self, callback) -> None:
        """Register callback invoked when POST /disconnect is received."""
        self._disconnect_callback = callback

    def start(self) -> None:
        """Start the Flask server in a daemon thread."""
        self._server_thread = threading.Thread(
            target=self._run_flask,
            name="CamNetController",
            daemon=True,
        )
        self._server_thread.start()
        logger.info("REST controller listening on http://localhost:{}/", self.API_PORT)

    def stop(self) -> None:
        """No-op: daemon thread exits with the main process."""
        logger.info("REST controller stopping.")

    # ------------------------------------------------------------------
    # Route definitions
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:
        app = self._app

        @app.route("/health")
        def health():
            """Lightweight liveness probe — returns 200 if the process is alive."""
            return jsonify({"status": "ok"})

        @app.route("/status")
        def status():
            return jsonify({
                "service": "CamNet Receiver",
                "version": "1.0",
                "platform": self.state.platform or sys.platform,
                "connected": self.state.connected,
                "sender": {
                    "name": self.state.sender_name,
                    "ip": self.state.sender_ip,
                    "port": self.state.sender_port,
                    "resolution": self.state.resolution,
                    "fps": self.state.fps,
                    "has_audio": self.state.has_audio,
                } if self.state.connected else None,
                "v4l2_device": self.state.v4l2_device or None,
                "stats": {
                    "frames_received": self.state.frames_received,
                    "frames_dropped": self.state.frames_dropped,
                    "reconnect_count": self.state.reconnect_count,
                    "latency_ms": round(self.state.latency_ms, 2),
                    "uptime_s": round(
                        time.time() - self.state.start_time
                        if self.state.start_time else 0, 1
                    ),
                },
            })

        @app.route("/devices")
        def devices():
            devices_list = getattr(self, "_devices_list_fn", lambda: [])()
            return jsonify({"devices": devices_list, "count": len(devices_list)})

        @app.route("/connect", methods=["POST"])
        def connect():
            body = request.get_json(silent=True) or {}
            device_name = body.get("device_name", "")
            ip = body.get("ip", "")
            try:
                port = int(body.get("port", 9000))
            except (ValueError, TypeError):
                return jsonify({"error": "port must be an integer"}), 400

            if not ip:
                return jsonify({"error": "ip is required"}), 400

            if not (1 <= port <= 65535):
                return jsonify({"error": "port must be between 1 and 65535"}), 400

            if self._connect_callback:
                try:
                    self._connect_callback(ip=ip, port=port, device_name=device_name)
                    return jsonify({"status": "connecting", "ip": ip, "port": port})
                except Exception as exc:
                    logger.error("Connect callback error: {}", exc)
                    return jsonify({"error": str(exc)}), 500

            return jsonify({"error": "No connect handler registered"}), 503

        @app.route("/disconnect", methods=["POST"])
        def disconnect():
            if self._disconnect_callback:
                try:
                    self._disconnect_callback()
                    return jsonify({"status": "disconnected"})
                except Exception as exc:
                    return jsonify({"error": str(exc)}), 500
            return jsonify({"error": "No disconnect handler registered"}), 503

        @app.route("/shm_info")
        def shm_info():
            """Returns shared memory layout for the DirectShow filter to read."""
            try:
                w = 1920 if not self.state.resolution else int(self.state.resolution.split("x")[0])
                h = 1080 if not self.state.resolution else int(self.state.resolution.split("x")[1])
            except (ValueError, IndexError):
                w, h = 1920, 1080
            return jsonify({
                "shm_name": self.state.shm_name,
                "mutex_name": self.state.shm_mutex_name,
                "layout": {
                    "offset_magic": 0,         # uint32 = 0xCAFECAFE
                    "offset_width": 4,          # uint32
                    "offset_height": 8,         # uint32
                    "offset_fps": 12,           # uint32
                    "offset_frame_index": 16,   # uint64
                    "offset_timestamp_ms": 24,  # uint64
                    "offset_audio_size": 32,    # uint32
                    "offset_flags": 36,         # uint32 (bit0=has_audio)
                    "offset_pixels": 40,        # raw BGRA
                    "pixel_bytes": w * h * 4,
                    "audio_buffer_bytes": 192000,  # 1s @ 48kHz stereo s16le
                    "total_bytes": 40 + (w * h * 4) + 192000,
                },
                "width": w,
                "height": h,
                "fps": self.state.fps or 60,
                "pixel_format": "BGRA",
            })

        @app.route("/metrics")
        def metrics():
            """Prometheus-style plain text metrics."""
            lines = [
                "# HELP camnet_frames_received Total frames received from sender",
                "# TYPE camnet_frames_received counter",
                f"camnet_frames_received {self.state.frames_received}",
                "# HELP camnet_frames_dropped Total frames dropped",
                "# TYPE camnet_frames_dropped counter",
                f"camnet_frames_dropped {self.state.frames_dropped}",
                "# HELP camnet_latency_ms Current estimated frame latency in milliseconds",
                "# TYPE camnet_latency_ms gauge",
                f"camnet_latency_ms {self.state.latency_ms:.2f}",
                "# HELP camnet_connected 1 if connected to a sender, 0 otherwise",
                "# TYPE camnet_connected gauge",
                f"camnet_connected {1 if self.state.connected else 0}",
            ]
            return Response("\n".join(lines), mimetype="text/plain")

    def set_devices_list_fn(self, fn) -> None:
        """Set a callable that returns the current list of discovered devices."""
        self._devices_list_fn = fn

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_flask(self) -> None:
        import logging as stdlib_logging
        # Suppress Flask's default werkzeug logging (we use loguru)
        stdlib_logging.getLogger("werkzeug").setLevel(stdlib_logging.ERROR)
        self._app.run(
            host="127.0.0.1",
            port=self.API_PORT,
            debug=False,
            use_reloader=False,
        )
