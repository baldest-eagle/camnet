# CamNet User's Guidebook (v2.0)

> **Stream any network camera directly into OBS Studio as a native Virtual Camera device.**

---

## Table of Contents

- [1. Introduction](#1-introduction)
  - [1.1 Key Features](#11-key-features)
  - [1.2 System Architecture](#12-system-architecture)
- [2. Requirements and Prerequisites](#2-requirements-and-prerequisites)
  - [2.1 Windows Requirements](#21-windows-requirements)
  - [2.2 Linux Requirements](#22-linux-requirements)
  - [2.3 Sender Device Requirements](#23-sender-device-requirements)
- [3. Installation](#3-installation)
  - [3.1 Windows: Driver Build and Registration](#31-windows-driver-build-and-registration)
  - [3.2 Linux: V4L2 Loopback Setup](#32-linux-v4l2-loopback-setup)
  - [3.3 Python Dependencies](#33-python-dependencies)
- [4. Quick Start Guide](#4-quick-start-guide)
  - [4.1 Starting the Receiver](#41-starting-the-receiver)
  - [4.2 Starting the Sender](#42-starting-the-sender)
  - [4.3 Configuring OBS Studio](#43-configuring-obs-studio)
- [5. Configuration Reference](#5-configuration-reference)
  - [5.1 Receiver CLI Options](#51-receiver-cli-options)
  - [5.2 Sender CLI Options](#52-sender-cli-options)
  - [5.3 Shared Memory Layout](#53-shared-memory-layout)
  - [5.4 REST API](#54-rest-api)
- [6. Platform Details](#6-platform-details)
  - [6.1 Windows: DirectShow Virtual Camera](#61-windows-directshow-virtual-camera)
  - [6.2 Linux: V4L2 Loopback Virtual Camera](#62-linux-v4l2-loopback-virtual-camera)
  - [6.3 Platform Abstraction Layer](#63-platform-abstraction-layer)
- [7. Troubleshooting](#7-troubleshooting)
- [8. Advanced Usage](#8-advanced-usage)
- [9. Latency Targets and Performance](#9-latency-targets-and-performance)
- [10. Project File Reference](#10-project-file-reference)

---

## 1. Introduction

CamNet is an open-source tool that streams any network camera directly into OBS Studio as a native **Video Capture Device**. Whether you want to use a smartphone, a secondary PC webcam, or any local IP camera as a high-quality video source, CamNet makes it appear natively in the *Video Capture Device* menu — no browser source, no OBS plugins required.

Starting with **version 2.0**, CamNet is fully **multiplatform**: it supports both **Windows** and **Linux** as receiver platforms. On Windows, CamNet uses a DirectShow COM filter (virtual camera driver) for maximum OBS compatibility. On Linux, it leverages the **v4l2loopback** kernel module to create a V4L2 virtual camera device, which works with OBS Studio, VLC, Chrome, and any other V4L2-compatible application.

### 1.1 Key Features

| Feature | Description |
|---------|-------------|
| Zero-config discovery | mDNS/ZeroConf auto-detects cameras on your LAN |
| Ultra-low latency | SRT streaming targets <80ms p50 on local network |
| 1080p @ 60fps | H.264 (zerolatency) with AAC audio passthrough |
| Audio included | Sender mic routed alongside video |
| Linux support | V4L2 loopback virtual camera for OBS and other apps |
| Windows support | DirectShow COM filter for maximum OBS compatibility |
| REST API | Built-in HTTP API on port 7432 for status and control |
| System tray GUI | Visual connection status with one-click connect/disconnect |

### 1.2 System Architecture

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

Both platforms share the same sender application and network protocol. Only the final output stage (virtual camera) is platform-specific.

---

## 2. Requirements and Prerequisites

### 2.1 Windows Requirements

| Requirement | Details |
|-------------|---------|
| Operating System | Windows 10/11 (64-bit) |
| Python | 3.11 or later |
| FFmpeg | On system PATH |
| Visual Studio 2022 | Required only for building the DirectShow driver DLL |
| Network | Same LAN subnet as sender |

### 2.2 Linux Requirements

| Requirement | Details |
|-------------|---------|
| Operating System | Linux kernel 5.x+ (Debian, Ubuntu, Fedora, Arch, etc.) |
| Python | 3.10 or later |
| FFmpeg | On system PATH |
| v4l2loopback | Kernel module (DKMS package on most distros) |
| Network | Same LAN subnet as sender |

### 2.3 Sender Device Requirements

| Requirement | Details |
|-------------|---------|
| Python | 3.10 or later |
| OpenCV | For camera capture |
| GStreamer | Optional, for hardware-accelerated encoding |
| Camera | Any webcam accessible via OpenCV |

---

## 3. Installation

### 3.1 Windows: Driver Build and Registration

#### 3.1.1 Building the Driver DLL

```bash
cd driver
cmake -B build -G "Visual Studio 17 2022" -A x64 ^
  -DDSHOW_BASECLASSES="C:\path\to\baseclasses"
cmake --build build --config Release
```

#### 3.1.2 Installing the Driver

```bash
cd driver_installer
py install.py

# For development/testing (enables test signing, reboot required):
py install.py --test-mode

# If you already built the DLL manually:
py install.py --skip-build
```

> **Tip:** If the install script fails with a regsvr32 error, ensure no application (including OBS) is using the virtual camera.

#### 3.1.3 Uninstalling the Driver

```bash
cd driver_installer
py uninstall.py
```

### 3.2 Linux: V4L2 Loopback Setup

#### 3.2.1 Automated Setup

```bash
chmod +x linux_setup.sh
sudo ./linux_setup.sh
```

The script automatically:
1. Detects your distribution (Debian/Ubuntu, Fedora, or Arch)
2. Installs system packages (ffmpeg, v4l2loopback-dkms, python3-pip)
3. Loads the v4l2loopback kernel module with CamNet label
4. Verifies `/dev/video2` exists and is functional
5. Writes persistent configuration for reboots
6. Installs Python dependencies

#### 3.2.2 Manual Setup

**Install the package:**

| Distro | Command |
|--------|---------|
| Debian/Ubuntu | `sudo apt install v4l2loopback-dkms` |
| Fedora | `sudo dnf install v4l2loopback` |
| Arch Linux | `sudo pacman -S v4l2loopback-dkms` |

**Load the module:**

```bash
sudo modprobe v4l2loopback \
  video_nr=2 \
  card_label='CamNet Virtual Camera' \
  exclusive_caps=1
```

**Make it persistent across reboots:**

```bash
echo "v4l2loopback" | sudo tee /etc/modules-load.d/camnet.conf
echo "options v4l2loopback video_nr=2 card_label='CamNet Virtual Camera' exclusive_caps=1" | \
  sudo tee /etc/modprobe.d/camnet.conf
```

### 3.3 Python Dependencies

```bash
# Windows receiver:
pip install -r receiver/requirements.txt

# Linux receiver:
pip3 install -r receiver/requirements_linux.txt

# Sender (any platform):
pip install -r sender/requirements.txt
```

---

## 4. Quick Start Guide

### 4.1 Starting the Receiver

#### Windows

```bash
cd receiver
pip install -r requirements.txt
py main.py
```

#### Linux

```bash
cd receiver
pip3 install -r requirements_linux.txt
python3 main.py

# Optional: specify a custom V4L2 device:
python3 main.py --v4l2-device /dev/video2
```

### 4.2 Starting the Sender

#### Desktop Sender (Python)

```bash
cd sender
pip install -r requirements.txt
py main.py --device 0 --resolution 1920x1080 --fps 60
```

#### Smartphone Sender (Browser)

Open `sender/web/index.html` in Chrome/Safari on the same WiFi network.
Enter the receiver's IP and press **Start Streaming**.

### 4.3 Configuring OBS Studio

#### Windows

1. Add Source → **Video Capture Device**
2. Select **"CamNet Virtual Camera"**
3. Add Source → **Audio Input Capture** → Select **"CamNet Virtual Audio"**

#### Linux

1. Add Source → **Video Capture Device**
2. Select **"CamNet Virtual Camera"** (typically `/dev/video2`)
3. Add Source → **Audio Input Capture** → Select the appropriate ALSA/PulseAudio source

> **Tip:** If OBS doesn't show the CamNet Virtual Camera on Linux, verify v4l2loopback is loaded (`lsmod | grep v4l2loopback`) and that `/dev/video2` exists. Restart OBS after starting the receiver.

---

## 5. Configuration Reference

### 5.1 Receiver CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--auto-connect / --no-auto-connect` | True | Auto-connect to first discovered sender |
| `--resolution, -r` | 1920x1080 | Capture resolution (WxH) |
| `--fps, -f` | 60 | Target frame rate |
| `--no-tray` | False | Disable system tray icon |
| `--v4l2-device` | auto-detect | [Linux] V4L2 device path (e.g. /dev/video2) |
| `--enable-shm / --disable-shm` | enabled | [Linux] Enable POSIX SHM mirror |
| `--verbose, -v` | False | Enable debug logging |

### 5.2 Sender CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--device` | 0 | Camera device index |
| `--resolution, -r` | 1920x1080 | Capture resolution (WxH) |
| `--fps, -f` | 60 | Target frame rate |
| `--port, -p` | 9000 | SRT listener port |
| `--no-audio` | False | Disable audio capture and streaming |
| `--no-tray` | False | Disable system tray icon |
| `--verbose, -v` | False | Enable debug logging |

### 5.3 Shared Memory Layout

Both platforms use the same shared memory layout:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 bytes | magic | `0xCAFECAFE` |
| 4 | 4 bytes | width | Frame width in pixels |
| 8 | 4 bytes | height | Frame height in pixels |
| 12 | 4 bytes | fps | Target frame rate |
| 16 | 8 bytes | frame_index | Monotonically increasing counter |
| 24 | 8 bytes | timestamp_ms | Milliseconds since Unix epoch |
| 32 | 4 bytes | audio_chunk_size | Audio data size in bytes |
| 36 | 4 bytes | flags | Bit flags (bit 0 = has_audio) |
| 40 | W×H×4 | pixels | Raw BGRA pixel data |
| 40+pixels | variable | audio | PCM s16le stereo 48 kHz |

- **Windows:** `Global\CamNetFrame` (Named File Mapping), synced by `Global\CamNetMutex`
- **Linux:** `/CamNetFrame` (POSIX shm_open), synced by `/tmp/camnet_shm.lock` (fcntl flock)

### 5.4 REST API

The receiver exposes HTTP API on port 7432:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Receiver health, stream state, platform info |
| `/health` | GET | Lightweight liveness check |
| `/devices` | GET | Discovered CamNet sender devices |
| `/connect` | POST | Connect to a sender (IP + port in body) |
| `/disconnect` | POST | Disconnect current stream |
| `/shm_info` | GET | Shared memory layout info |
| `/metrics` | GET | Prometheus-style metrics |

---

## 6. Platform Details

### 6.1 Windows: DirectShow Virtual Camera

CamNet on Windows uses a DirectShow COM filter (`camnet_vcam.dll`) implemented as a CSource push-mode filter in C++. The filter reads frame data from the `Global\CamNetFrame` Named Shared Memory segment. When the Python receiver decodes a frame, it writes BGRA pixel data into the shared memory. The filter's `FillBuffer()` method reads from this shared memory, copying pixels into the DirectShow output buffer.

Synchronization is handled by the `Global\CamNetMutex` named mutex — the receiver acquires the mutex before writing, and the filter acquires it before reading, ensuring no partially-written frames are ever consumed.

### 6.2 Linux: V4L2 Loopback Virtual Camera

On Linux, the `V4L2LoopbackWriter` class manages the V4L2 device interaction. On startup, it:

1. Opens the device in write-only, non-blocking mode (`O_WRONLY | O_NONBLOCK`)
2. Queries capabilities via `VIDIOC_QUERYCAP`
3. Sets the video format via `VIDIOC_S_FMT` (BGR32/BGRA)
4. Writes decoded frames via `os.write()`

If the device buffer is full, `BlockingIOError` is raised and the frame is skipped gracefully. This prevents latency accumulation.

The writer also maintains an optional POSIX SHM mirror (`/CamNetFrame`) with the same layout as the Windows shared memory, synchronized via `fcntl flock()` on `/tmp/camnet_shm.lock`.

Auto-detection is handled by `find_v4l2_loopback_device()`, which scans `/sys/class/video4linux/` for a device whose card label contains "CamNet".

### 6.3 Platform Abstraction Layer

The `receiver/platform_shm.py` module provides platform-agnostic frame output:

- **`FrameWriter` protocol** — defines `open()`, `close()`, `write_frame()`, `frame_index`
- **`create_frame_writer()` factory** — returns `ShmFrameWriter` (Windows) or `V4L2LoopbackWriter` (Linux)
- **`get_shm_name()` / `get_mutex_name()`** — platform-specific naming helpers

The main receiver orchestrator uses only the `FrameWriter` interface, keeping the core pipeline completely platform-agnostic.

---

## 7. Troubleshooting

### Cross-Platform Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| No sender discovered | mDNS blocked / different subnet | Same subnet; open UDP 5353; disable VPN |
| Stream connects but no video | Resolution mismatch | Match sender/receiver resolution; restart OBS |
| High latency (>200ms) | Network congestion / WiFi | Use Ethernet; reduce resolution/FPS |
| Audio not working | Mic not configured on sender | Check sender has accessible microphone |

### Windows-Specific Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Virtual Camera not in OBS | DLL not registered | Run `install.py` as Admin; restart OBS |
| Cannot write to SHM | Insufficient privileges | Run receiver as Admin |
| DLL build fails | Missing DirectShow base classes | Provide `-DDSHOW_BASECLASSES` path |

### Linux-Specific Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| `/dev/video2` not found | v4l2loopback not loaded | `sudo modprobe v4l2loopback`; check `dmesg` |
| OBS can't open device | `exclusive_caps` not set | Reload with `exclusive_caps=1`; restart OBS |
| Permission denied | User not in `video` group | `sudo usermod -aG video $USER`; re-login |
| Black/garbled video | Pixel format mismatch | Verify BGR32 format; run self-test |
| SHM lock errors | Stale lock file | Remove `/tmp/camnet_shm.lock`; restart receiver |

> **Linux Debug Tip:** Run the V4L2 self-test: `python3 receiver/v4l2_output.py` — writes 100 alternating-color frames for visual verification.

---

## 8. Advanced Usage

### Headless Mode

```bash
# Windows:
py main.py --no-tray

# Linux:
python3 main.py --no-tray
```

### Manual Connection Control

```bash
# Start with auto-connect disabled:
python3 main.py --no-auto-connect

# List discovered devices:
curl http://localhost:7432/devices

# Connect to a specific device:
curl -X POST http://localhost:7432/connect \
  -H "Content-Type: application/json" \
  -d '{"ip": "192.168.1.100", "port": 9000, "device_name": "Phone Camera"}'

# Disconnect:
curl -X POST http://localhost:7432/disconnect
```

### Multi-Camera Setup

For multiple virtual cameras on Linux, create multiple v4l2loopback devices:

```bash
sudo modprobe v4l2loopback video_nr=2,3 card_label='CamNet Cam 1,CamNet Cam 2' exclusive_caps=1 max_devices=2
```

Then run multiple receiver instances with `--v4l2-device /dev/video2` and `--v4l2-device /dev/video3`.

### Running the Test Suite

```bash
pip install pytest pytest-asyncio zeroconf loguru

# Windows:
py -m pytest tests/ -v --tb=short

# Linux:
python3 -m pytest tests/ -v --tb=short
```

---

## 9. Latency Targets and Performance

| Metric | Target | How Achieved |
|--------|--------|--------------|
| SRT network latency | <80ms p50 | `latency=80000µs` in SRT URL |
| SHM/V4L2 write | <2ms p50, <10ms p99 | Direct memcpy / os.write() |
| H.264 encode | Minimal | `ultrafast zerolatency bframes=0` |
| Total pipeline | <150ms p99 | SRT + decode + write ≈ 100–130ms on LAN |

On Linux, non-blocking V4L2 writes prevent the receiver from falling behind. Dropped frame counts are reported via `/status` and `/metrics`.

---

## 10. Project File Reference

| Path | Description |
|------|-------------|
| `sender/` | Camera capture, encode, and stream (cross-platform Python) |
| `sender/main.py` | CLI entry point, system tray, lifecycle |
| `sender/capture.py` | Camera capture, 1080p60, BGR→BGRA |
| `sender/encoder.py` | FFmpeg H.264 encoder + GStreamer fallback |
| `sender/streamer.py` | SRT server-mode + WebRTC fallback |
| `sender/discovery.py` | mDNS `_camnet._tcp.local.` announcer |
| `sender/web/` | Browser-based sender (index.html + sender.js) |
| `receiver/` | Stream ingest, decode, virtual camera output |
| `receiver/main.py` | Orchestrator, auto-connect, frame pump |
| `receiver/discovery.py` | mDNS browser, DeviceRegistry |
| `receiver/ingest.py` | FFmpeg SRT caller, video + audio pipes |
| `receiver/shm_writer.py` | Win32 Named SHM writer (Windows only) |
| `receiver/v4l2_output.py` | V4L2 loopback writer (Linux only) |
| `receiver/platform_shm.py` | Platform-abstracted FrameWriter factory |
| `receiver/controller.py` | Flask REST API (port 7432) |
| `receiver/requirements.txt` | Windows receiver dependencies |
| `receiver/requirements_linux.txt` | Linux receiver dependencies |
| `driver/` | DirectShow COM filter (C++, Windows only) |
| `driver/src/camnet_vcam.h` | Filter GUID, SHM constants |
| `driver/src/camnet_vcam.cpp` | CSource filter + FillBuffer |
| `driver/src/dllmain.cpp` | DLL entry point |
| `driver/CMakeLists.txt` | MSVC build config |
| `driver_installer/` | DLL build + COM registration scripts |
| `driver_installer/install.py` | Build, copy, register DLL |
| `driver_installer/uninstall.py` | Unregister and remove DLL |
| `tests/` | Latency benchmarks and unit tests |
| `tests/conftest.py` | Shared pytest fixtures |
| `tests/test_discovery.py` | mDNS round-trip tests |
| `tests/test_shm.py` | SHM integrity tests |
| `tests/test_latency.py` | Latency benchmarks, dropout tests |
| `linux_setup.sh` | Automated Linux setup script |
| `README.md` | Project overview and quick start |

---

*CamNet v2.0 — MIT License — [github.com/baldest-eagle/camnet](https://github.com/baldest-eagle/camnet)*
