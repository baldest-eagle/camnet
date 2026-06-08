# CamNet 🎥→🖥️

**Stream any network camera directly into OBS Studio as a native Virtual Camera device.**

CamNet lets you use a smartphone, secondary PC webcam, or any local IP camera as a high-quality video source in OBS — appearing natively in the *Video Capture Device* menu with no browser source required.

## Features

- 🔍 **Zero-config discovery** — mDNS/ZeroConf auto-detects cameras on your LAN
- ⚡ **Ultra-low latency** — SRT streaming targets <80ms p50 on local network
- 🎬 **1080p @ 60fps** — H.264 (zerolatency) with AAC audio passthrough
- 🎙️ **Audio included** — Sender mic routed alongside video
- 📷 **OBS-native** — Appears as "CamNet Virtual Camera" in Video Capture Device
- 🪟 **Windows 11 ready** — DirectShow COM filter for maximum OBS compatibility

## Architecture

```
[Phone/Secondary PC]          [Primary Streaming PC]
  Sender App (Python)    →         Receiver Backend (Python)
  mDNS announce          ←→        mDNS discover
  SRT stream (H.264+AAC) →         FFmpeg decode
                                    ↓ Win32 Shared Memory
                                   C++ DirectShow DLL
                                    ↓ DirectShow
                                   OBS Studio
```

## Components

| Folder | Description |
|--------|-------------|
| `sender/` | Camera capture, encode, and stream app (cross-platform Python) |
| `receiver/` | Stream ingest, decode, and shared memory writer (Windows Python) |
| `driver/` | DirectShow Virtual Camera COM filter (C++) |
| `driver_installer/` | DLL build + COM registration scripts (Python) |
| `tests/` | Latency benchmarks, dropout tests, and unit tests |

## Quick Start

### Primary PC (Windows 11)
```bash
# 1. Install the virtual camera driver
py driver_installer/install.py

# 2. Start the receiver
cd receiver
pip install -r requirements.txt
py main.py
```

### Sender Device (any PC)
```bash
cd sender
pip install -r requirements.txt
py main.py --device 0 --resolution 1920x1080 --fps 60
```

### Smartphone Sender (no install)
Open `sender/web/index.html` in a browser on the same WiFi network.

### OBS Studio
1. Add Source → **Video Capture Device**
2. Select **"CamNet Virtual Camera"**
3. Add Source → **Audio Input Capture** → Select **"CamNet Virtual Audio"** (if audio enabled)

## Requirements

- **Receiver/Driver**: Windows 10/11, Python 3.11+, FFmpeg on PATH, Visual Studio 2022 (for driver build)
- **Sender**: Python 3.10+, OpenCV, GStreamer (optional, for hardware encode)
- **Network**: Both devices on same LAN subnet (WiFi or Ethernet)

## License

MIT
