# CamNet 🎥→🖥️

**Stream any network camera directly into OBS Studio as a native Virtual Camera device.**

CamNet lets you use a smartphone, secondary PC webcam, or any local IP camera as a high-quality video source in OBS — appearing natively in the *Video Capture Device* menu with no browser source required.

## Features

- 🔍 **Zero-config discovery** — mDNS/ZeroConf auto-detects cameras on your LAN
- ⚡ **Ultra-low latency** — SRT streaming targets <80ms p50 on local network
- 🎬 **1080p @ 60fps** — H.264 (zerolatency) with AAC audio passthrough
- 🎙️ **Audio included** — Sender mic routed alongside video
- 🐧 **Linux support** — V4L2 loopback virtual camera for OBS and other apps
- 🪟 **Windows support** — DirectShow COM filter for maximum OBS compatibility

## Architecture

```
[Phone/Secondary PC]          [Primary Streaming PC]
  Sender App (Python)    →         Receiver Backend (Python)
  mDNS announce          ←→        mDNS discover
  SRT stream (H.264+AAC) →         FFmpeg decode
                                    ↓
                            ┌───────┴────────┐
                            │                │
                       Windows:          Linux:
                    Win32 SHM         V4L2 loopback
                   C++ DirectShow     /dev/videoN
                        DLL              ↓
                            │        OBS / VLC /
                            │        Chrome /
                            └───────┬────────┘
                                    ↓
                               OBS Studio
```

## Components

| Folder | Description |
|--------|-------------|
| `sender/` | Camera capture, encode, and stream app (cross-platform Python) |
| `receiver/` | Stream ingest, decode, and virtual camera output (Windows + Linux) |
| `driver/` | DirectShow Virtual Camera COM filter (C++, Windows only) |
| `driver_installer/` | DLL build + COM registration scripts (Python, Windows only) |
| `tests/` | Latency benchmarks, dropout tests, and unit tests |

## Quick Start

### Primary PC (Windows)

```bash
# 1. Install the virtual camera driver
py driver_installer/install.py

# 2. Start the receiver
cd receiver
pip install -r requirements.txt
py main.py
```

### Primary PC (Linux)

```bash
# 1. Run the automated setup script (installs v4l2loopback, ffmpeg, deps)
chmod +x linux_setup.sh
sudo ./linux_setup.sh

# Or install manually:
sudo modprobe v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1
sudo apt install ffmpeg python3-pip

# 2. Start the receiver
cd receiver
pip3 install -r requirements_linux.txt
python3 main.py

# Optional: specify a V4L2 device
python3 main.py --v4l2-device /dev/video2
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

**Windows:**
1. Add Source → **Video Capture Device**
2. Select **"CamNet Virtual Camera"**
3. Add Source → **Audio Input Capture** → Select **"CamNet Virtual Audio"** (if audio enabled)

**Linux:**
1. Add Source → **Video Capture Device**
2. Select **"CamNet Virtual Camera"** (typically `/dev/video2`)
3. Add Source → **Audio Input Capture** → Select the appropriate ALSA/PulseAudio source

## Platform Details

### Windows

- Uses Win32 Named Shared Memory (`Global\CamNetFrame`) for zero-copy IPC between the Python receiver and the C++ DirectShow filter
- The DirectShow COM DLL (`camnet_vcam.dll`) must be registered with `regsvr32` — see `driver_installer/`
- Requires Windows 10/11, Python 3.11+, FFmpeg on PATH

### Linux

- Uses the **v4l2loopback** kernel module to create a virtual V4L2 video device
- The receiver writes decoded BGRA frames directly to `/dev/videoN`
- Optionally mirrors frames to POSIX shared memory (`/CamNetFrame`) for external consumers
- No kernel driver compilation needed — v4l2loopback is available as a DKMS package on most distros
- Requires Linux kernel 5.x+, Python 3.10+, FFmpeg on PATH

#### V4L2 Loopback Setup

```bash
# Debian/Ubuntu
sudo apt install v4l2loopback-dkms
sudo modprobe v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1

# Fedora
sudo dnf install v4l2loopback
sudo modprobe v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1

# Arch Linux
sudo pacman -S v4l2loopback-dkms
sudo modprobe v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1

# Make it persistent across reboots
echo "v4l2loopback" | sudo tee /etc/modules-load.d/camnet.conf
echo "options v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1" | sudo tee /etc/modprobe.d/camnet.conf
```

#### Linux CLI Options

```
--v4l2-device PATH    V4L2 loopback device path (default: auto-detect)
--enable-shm/--disable-shm   Enable POSIX SHM mirror (default: enabled)
```

## Receiver REST API

The receiver exposes a local HTTP API on port 7432:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Overall receiver health and stream state |
| `/health` | GET | Lightweight liveness check |
| `/devices` | GET | List discovered CamNet sender devices |
| `/connect` | POST | Connect to a specific sender device |
| `/disconnect` | POST | Disconnect from current stream |
| `/shm_info` | GET | Shared memory layout info (platform-specific) |
| `/metrics` | GET | Prometheus-style metrics |

## Requirements

### Windows
- Windows 10/11, Python 3.11+, FFmpeg on PATH
- Visual Studio 2022 (for driver build only)

### Linux
- Linux kernel 5.x+, Python 3.10+, FFmpeg on PATH
- `v4l2loopback` kernel module

### Sender (any platform)
- Python 3.10+, OpenCV, GStreamer (optional, for hardware encode)

### Network
- Both devices on same LAN subnet (WiFi or Ethernet)

## License

MIT
