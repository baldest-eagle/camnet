# CamNet — Network Camera to OBS Virtual Source

**CamNet** streams any camera on your local network (phone, secondary PC, IP cam) directly into OBS Studio as a native "Video Capture Device" — no browser source, no workarounds.

---

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  SENDER DEVICE (Phone / Secondary PC)                               │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────────────────────────────┐  │
│  │ Camera HW    │───▶│ Sender App (Python + GStreamer/OpenCV)   │  │
│  └──────────────┘    │  • Captures raw frames                   │  │
│                      │  • Encodes H.264 via GStreamer pipeline   │  │
│                      │  • Announces via mDNS (_camnet._tcp)     │  │
│                      │  • Streams via SRT / WebRTC              │  │
│                      └──────────────────────────────────────────┘  │
└────────────────────────────────────┬────────────────────────────────┘
                                     │  SRT / WebRTC (LAN, <100ms RTT)
                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PRIMARY PC (Windows 11) — OBS Studio                               │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Receiver Backend (Python orchestrator)                        │  │
│  │  • mDNS discovery (zeroconf)                                  │  │
│  │  • SRT ingest via libsrt / ffmpeg-python                      │  │
│  │  • Frame decode → raw BGRA buffer                             │  │
│  │  • Writes frames into Named Shared Memory                     │  │
│  └──────────────────────────┬─────────────────────────────────┘   │
│                              │  Win32 CreateFileMapping            │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ CamNet DirectShow Virtual Camera (C++ COM DLL)                │  │
│  │  • Registered via regsvr32 at install time                    │  │
│  │  • Implements IBaseFilter / CSource / IPin                    │  │
│  │  • Reads frames from Shared Memory at output frame rate       │  │
│  │  • Appears in Windows as "CamNet Virtual Camera"              │  │
│  └────────────────────────────────┬──────────────────────────────┘  │
│                                   │  DirectShow                      │
│                                   ▼                                  │
│               ┌───────────────────────────────┐                     │
│               │  OBS Studio                   │                     │
│               │  Video Capture Device source  │                     │
│               │  → "CamNet Virtual Camera"    │                     │
│               └───────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## User Review Required

> [!IMPORTANT]
> **Dual-Protocol Strategy**: The architecture supports **both SRT and WebRTC** to maximize compatibility. SRT is the primary streaming protocol (lower CPU overhead on LAN, works natively with FFmpeg/GStreamer, and is OBS-native). WebRTC is provided as a fallback for browser-based senders (smartphones without app install). The blueprint defaults to **SRT** — review if you want WebRTC-only or a combined approach.

> [!WARNING]
> **Driver Signing**: The DirectShow DLL (`camnet_vcam.dll`) must be **code-signed** to register cleanly on Windows 11. For development, Test Mode (`bcdedit /set testsigning on`) is used. For production distribution, the DLL must be signed with a trusted EV certificate or delivered via a WHQL-signed driver package. Review how you want to handle distribution signing.

> [!IMPORTANT]
> **Sender Platforms**: The Sender app is specified as Python + GStreamer. This works on Windows and Linux secondary PCs. For **smartphones**, a React Native / Expo web app is provided that uses `navigator.mediaDevices.getUserMedia` + WebRTC as the sender transport (no app install required). Please confirm if you need a native Android/iOS app instead.

---

## Open Questions

> [!IMPORTANT]
> 1. **Maximum stream resolution/FPS**: Default target is 1920×1080 @ 30fps. Do you need 4K @ 60fps? This affects the shared memory buffer size and DirectShow media type negotiation.
> 2. **Audio passthrough**: Should CamNet also route audio from the sender? The architecture can add an audio virtual device alongside the video one, but it adds significant complexity.
> 3. **Multi-camera**: Should the receiver support more than one network camera simultaneously (e.g., two phones at once as two separate virtual cameras)?

---

## Proposed Changes

### Component 1 — Project Scaffolding

#### [NEW] Project root structure at `c:\Users\kyleh\Desktop\camnet\`
```
camnet/
├── sender/                  # Cross-platform sender app
│   ├── main.py              # Entry point, CLI + tray icon
│   ├── capture.py           # Camera capture (OpenCV / GStreamer)
│   ├── encoder.py           # H.264 encoding pipeline
│   ├── streamer.py          # SRT / WebRTC stream output
│   ├── discovery.py         # mDNS service announcement
│   ├── web/                 # Browser-based sender (smartphone)
│   │   ├── index.html
│   │   └── sender.js        # getUserMedia → WebRTC
│   ├── requirements.txt
│   └── README.md
│
├── receiver/                # Primary PC backend
│   ├── main.py              # Entry point, system tray + GUI
│   ├── discovery.py         # mDNS browser + device registry
│   ├── ingest.py            # SRT ingest + frame decode pipeline
│   ├── shm_writer.py        # Named shared memory frame writer
│   ├── controller.py        # REST API for driver control
│   ├── requirements.txt
│   └── README.md
│
├── driver/                  # DirectShow virtual camera DLL (C++)
│   ├── CMakeLists.txt
│   ├── src/
│   │   ├── camnet_vcam.cpp  # Main filter + COM registration
│   │   ├── camnet_vcam.h
│   │   ├── shm_reader.cpp   # Shared memory frame consumer
│   │   ├── shm_reader.h
│   │   └── dllmain.cpp      # DllMain + registration exports
│   └── README.md
│
├── driver_installer/        # Python-based installer / registration
│   ├── install.py           # Builds + registers the DLL
│   ├── uninstall.py
│   └── README.md
│
├── tests/                   # Verifiable test suite
│   ├── test_discovery.py    # mDNS announce + browse round-trip
│   ├── test_latency.py      # Frame latency benchmark
│   ├── test_dropout.py      # Network dropout simulation
│   ├── test_shm.py          # Shared memory read/write correctness
│   └── conftest.py
│
└── README.md                # Top-level project readme
```

---

### Component 2 — Sender Application (Python)

#### [NEW] `sender/main.py`
- CLI argument parsing (`--device`, `--resolution`, `--fps`, `--protocol`)
- System tray integration (pystray) for headless operation
- Orchestrates capture → encode → stream pipeline

#### [NEW] `sender/discovery.py`
- Registers `_camnet._tcp.local.` mDNS service via `zeroconf`
- Announces: hostname, device name, IP, SRT port, resolution, FPS
- Handles graceful teardown on exit

#### [NEW] `sender/capture.py`
- OpenCV `VideoCapture` backend with fallback to GStreamer pipeline
- Hardware acceleration: DXVA2 on Windows, VideoToolbox on macOS, V4L2 on Linux
- Configurable resolution, FPS, and color format (BGR → BGRA conversion)

#### [NEW] `sender/encoder.py`
- GStreamer pipeline: `appsrc → videoconvert → x264enc tune=zerolatency → rtph264pay → srtpay`
- Profile: Baseline / Main, b-frames=0, key-int-max=30 for stream robustness
- Fallback: ffmpeg-python subprocess encoder

#### [NEW] `sender/streamer.py`
- SRT server mode: binds a port, accepts receiver connections
- WebRTC mode: `aiortc`-based peer offering for browser sender fallback

#### [NEW] `sender/web/index.html` + `sender.js`
- Smartphone-friendly browser sender
- `getUserMedia` → WebRTC datachannel or HTTP chunked MJPEG fallback
- Works without any app installation on iOS / Android

---

### Component 3 — Receiver Backend (Python)

#### [NEW] `receiver/discovery.py`
- `zeroconf` `ServiceBrowser` scanning for `_camnet._tcp.local.`
- Maintains a live registry of available senders with IP, port, capabilities
- Emits events on device connect / disconnect

#### [NEW] `receiver/ingest.py`
- Opens SRT stream to selected sender
- Decodes H.264 via FFmpeg subprocess (`ffmpeg -i srt://... -f rawvideo -pix_fmt bgra pipe:1`)
- Yields raw BGRA frames to the shared memory writer
- Handles reconnection on stream dropout with exponential backoff

#### [NEW] `receiver/shm_writer.py`
- Uses Python `mmap` + `ctypes` Win32 `CreateFileMapping` / `MapViewOfFile`
- Shared memory name: `Global\CamNetFrame_<deviceId>`
- Frame layout: `[4-byte width][4-byte height][4-byte timestamp_ms][raw BGRA pixels]`
- Mutex-protected writes for synchronization with DirectShow filter

#### [NEW] `receiver/main.py`
- PyQt6 or tkinter system tray GUI
- Displays: discovered cameras, selected stream, latency stats
- Exposes local REST API (`http://localhost:7432/`) for driver querying

---

### Component 4 — DirectShow Virtual Camera (C++)

#### [NEW] `driver/src/camnet_vcam.cpp`
- Implements `CSource` (DirectShow base class) with one output pin
- Media type: `MEDIATYPE_Video / MEDIASUBTYPE_RGB32`, 1920×1080 @ 30fps
- `FillBuffer()` reads from shared memory on each frame request
- COM registration: `DllRegisterServer` / `DllUnregisterServer`
- Filter GUID: `{A1B2C3D4-...}` (unique UUID generated for CamNet)

#### [NEW] `driver/src/shm_reader.cpp`
- Win32 `OpenFileMapping` + `MapViewOfFile`
- Frame copy with `memcpy` into DirectShow media sample buffer
- Falls back to black frame on timeout (>100ms since last frame)
- Named mutex for synchronization with Python writer

#### [NEW] `driver/CMakeLists.txt`
- MinGW or MSVC toolchain
- Links: `strmiids.lib`, `uuid.lib`, `winmm.lib`, `ole32.lib`, `oleaut32.lib`
- Output: `camnet_vcam.dll` (both x64 and x86 for maximum compatibility)

---

### Component 5 — Driver Installer (Python)

#### [NEW] `driver_installer/install.py`
- Verifies Windows 11 / Windows 10 version
- Copies DLL to `%SystemRoot%\System32\`
- Runs `regsvr32.exe /s camnet_vcam.dll` for COM registration
- Verifies registration by querying registry key
- Optional: enables test-signing mode for development

#### [NEW] `driver_installer/uninstall.py`
- Runs `regsvr32.exe /u /s camnet_vcam.dll`
- Removes DLL from System32

---

### Component 6 — Test Suite

#### [NEW] `tests/test_discovery.py`
- Spins up a mock mDNS announcer and verifies `receiver/discovery.py` detects it within 3 seconds

#### [NEW] `tests/test_latency.py`
- Measures end-to-end frame latency: timestamp embedded in frame → extracted at receiver
- Target: p50 < 80ms, p99 < 150ms on local LAN

#### [NEW] `tests/test_dropout.py`
- Simulates network dropout using socket close + reconnect
- Verifies receiver reconnects within 2 seconds and resumes frames

#### [NEW] `tests/test_shm.py`
- Validates shared memory write/read round-trip for frame integrity

---

## Technology Stack Summary

| Layer | Technology | Rationale |
|---|---|---|
| Network Discovery | `zeroconf` (Python) | Pure-Python, Bonjour-compatible, used by Home Assistant |
| Streaming Protocol | **SRT** (primary) + WebRTC fallback | SRT: <100ms LAN latency, built into FFmpeg; WebRTC: browser sender |
| Video Codec | **H.264** (Baseline/Main, zerolatency) | Hardware-decodable everywhere, DirectShow compatible |
| Ingest / Decode | `ffmpeg` subprocess + `ffmpeg-python` | Proven, GPU-accelerated, handles SRT natively |
| IPC Frames | Win32 Named Shared Memory + Mutex | Zero-copy between Python receiver and C++ DirectShow filter |
| Virtual Camera | DirectShow CSource filter (C++) | OBS Studio reads it as a native `Video Capture Device` |
| Sender UI | Python + pystray (tray) | Lightweight, cross-platform system tray app |
| Receiver UI | Python + tkinter (tray + config) | No heavy GUI dependency |
| Build System | CMake + MSVC/MinGW | Standard for Windows COM DLL compilation |
| Installer | Python + `subprocess` (regsvr32) | Simple, no NSIS/WiX dependency for MVP |

---

## Verification Plan

### Automated Tests
```bash
# From camnet/ root
py -m pytest tests/ -v --tb=short

# Individual benchmarks
py tests/test_latency.py --duration 30 --target-p99 150
py tests/test_dropout.py --dropout-duration 3.0
```

### Manual Verification
1. Register DLL: `py driver_installer/install.py`
2. Launch receiver: `py receiver/main.py`
3. Launch sender on secondary PC: `py sender/main.py --device 0`
4. Open OBS → Sources → Video Capture Device → Select **"CamNet Virtual Camera"**
5. Confirm live video feed appears with <150ms visual latency

### Build Verification
```bash
cd driver && cmake -B build -G "Visual Studio 17 2022" && cmake --build build --config Release
```
