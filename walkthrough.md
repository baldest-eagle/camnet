# CamNet — Project Walkthrough

## What Was Built

**CamNet** streams any network camera (phone, secondary PC, IP cam) into OBS Studio as a native **Video Capture Device** — no browser source, no plugins to install in OBS itself.

---

## Full Data Flow

```
📱 Sender (phone/secondary PC)
  Camera HW
    → sender/capture.py    (OpenCV + DirectShow/V4L2, 1080p@60fps)
    → sender/encoder.py    (FFmpeg subprocess, H.264 zerolatency + AAC)
    → sender/streamer.py   (SRT listener, port 9000, H.264+AAC MPEG-TS)
    → sender/discovery.py  (mDNS: _camnet._tcp.local. announcement)

🌐 Local Network (SRT, ~80ms latency target)

🖥️ Receiver (Windows 11 primary PC)
    → receiver/discovery.py   (zeroconf browser, auto-detects sender)
    → receiver/ingest.py      (FFmpeg SRT caller → raw BGRA + PCM pipes)
    → receiver/shm_writer.py  (Win32 CreateFileMapping + Named Mutex)
    ↓ Global\CamNetFrame (shared memory)
    → driver/src/camnet_vcam.cpp  (DirectShow CSource filter, FillBuffer())
    ↓ DirectShow push-mode graph
    → OBS Studio → Video Capture Device → "CamNet Virtual Camera"
```

---

## File Reference (29 files)

### Sender (`sender/`)
| File | Role |
|------|------|
| [main.py](file:///c:/Users/kyleh/Desktop/camnet/sender/main.py) | CLI entry (Click), system tray, lifecycle orchestration |
| [capture.py](file:///c:/Users/kyleh/Desktop/camnet/sender/capture.py) | Camera capture, 1080p60, BGR→BGRA, drift-corrected pacing |
| [encoder.py](file:///c:/Users/kyleh/Desktop/camnet/sender/encoder.py) | FFmpeg H.264 encoder + GStreamer HW-accel fallback |
| [streamer.py](file:///c:/Users/kyleh/Desktop/camnet/sender/streamer.py) | SRT server-mode + WebRTC aiortc fallback |
| [discovery.py](file:///c:/Users/kyleh/Desktop/camnet/sender/discovery.py) | mDNS `_camnet._tcp.local.` service announcer |
| [web/index.html](file:///c:/Users/kyleh/Desktop/camnet/sender/web/index.html) | Mobile-first browser sender UI |
| [web/sender.js](file:///c:/Users/kyleh/Desktop/camnet/sender/web/sender.js) | WebRTC getUserMedia → SDP offer/answer → live stats |

### Receiver (`receiver/`)
| File | Role |
|------|------|
| [main.py](file:///c:/Users/kyleh/Desktop/camnet/receiver/main.py) | Orchestrator, auto-connect, frame pump thread |
| [discovery.py](file:///c:/Users/kyleh/Desktop/camnet/receiver/discovery.py) | mDNS browser, DeviceRegistry, connect/lost callbacks |
| [ingest.py](file:///c:/Users/kyleh/Desktop/camnet/receiver/ingest.py) | FFmpeg SRT caller, BGRA video pipe + PCM audio pipe, reconnect backoff |
| [shm_writer.py](file:///c:/Users/kyleh/Desktop/camnet/receiver/shm_writer.py) | Win32 Named SHM + Named Mutex, binary frame header |
| [controller.py](file:///c:/Users/kyleh/Desktop/camnet/receiver/controller.py) | Flask REST API (`:7432`): status, devices, connect, shm_info, metrics |

### Driver (`driver/`)
| File | Role |
|------|------|
| [src/camnet_vcam.h](file:///c:/Users/kyleh/Desktop/camnet/driver/src/camnet_vcam.h) | Filter GUID, SHM layout constants, class declarations |
| [src/camnet_vcam.cpp](file:///c:/Users/kyleh/Desktop/camnet/driver/src/camnet_vcam.cpp) | CCamNetVCam (CSource) + CCamNetVCamPin (CSourceStream), FillBuffer, SHM reader |
| [src/dllmain.cpp](file:///c:/Users/kyleh/Desktop/camnet/driver/src/dllmain.cpp) | DllMain → strmbase DllEntryPoint |
| [CMakeLists.txt](file:///c:/Users/kyleh/Desktop/camnet/driver/CMakeLists.txt) | MSVC build, strmbase linking, DEF file, post-build copy |
| [README.md](file:///c:/Users/kyleh/Desktop/camnet/driver/README.md) | Build requirements, DirectShow base class setup, signing guide |

### Installer (`driver_installer/`)
| File | Role |
|------|------|
| [install.py](file:///c:/Users/kyleh/Desktop/camnet/driver_installer/install.py) | Build DLL, copy to System32, regsvr32, registry verify, UAC elevation |
| [uninstall.py](file:///c:/Users/kyleh/Desktop/camnet/driver_installer/uninstall.py) | regsvr32 /u, remove from System32, registry cleanup |

### Tests (`tests/`)
| File | Covers |
|------|--------|
| [conftest.py](file:///c:/Users/kyleh/Desktop/camnet/tests/conftest.py) | Shared fixtures: announcer, discovery, tmp_shm_name |
| [test_discovery.py](file:///c:/Users/kyleh/Desktop/camnet/tests/test_discovery.py) | mDNS announce→browse round-trip, TXT record fields, removal |
| [test_shm.py](file:///c:/Users/kyleh/Desktop/camnet/tests/test_shm.py) | SHM open/close, header magic, pixel integrity, frame index, audio, mutex |
| [test_latency.py](file:///c:/Users/kyleh/Desktop/camnet/tests/test_latency.py) | Write latency (<5ms), 60fps throughput, dropout reconnect, p50/p99 benchmark |

---

## Quick Start Commands

### 1. Install Driver (Primary PC, Administrator)
```powershell
cd c:\Users\kyleh\Desktop\camnet\driver_installer
py install.py --test-mode   # Dev: enables test signing, reboot required
# OR after building with CMake:
py install.py --skip-build
```

### 2. Start Receiver (Primary PC)
```powershell
cd c:\Users\kyleh\Desktop\camnet\receiver
pip install -r requirements.txt
py main.py                   # Auto-connects to first discovered sender
```

### 3. Start Sender (Secondary PC)
```powershell
cd c:\Users\kyleh\Desktop\camnet\sender
pip install -r requirements.txt
py main.py --device 0 --fps 60
```

### 4. Smartphone Sender (no install)
Open `sender/web/index.html` in Chrome/Safari on the same WiFi.
Enter the receiver's IP address and press **Start Streaming**.

### 5. OBS Studio
- Add Source → **Video Capture Device**
- Select **"CamNet Virtual Camera"**
- Add Source → **Audio Input Capture** → **"CamNet Virtual Audio"** *(when audio virtual device is added)*

### 6. Run Tests
```powershell
cd c:\Users\kyleh\Desktop\camnet
pip install pytest pytest-asyncio zeroconf loguru pywin32
py -m pytest tests/ -v --tb=short
```

---

## Driver Build Prerequisites

Before installing, build the C++ DLL:

```powershell
# Install Visual Studio 2022 Build Tools first, then:
cd c:\Users\kyleh\Desktop\camnet\driver
cmake -B build -G "Visual Studio 17 2022" -A x64 -DDSHOW_BASECLASSES="path\to\baseclasses"
cmake --build build --config Release
```

The DirectShow base classes are found in the Windows SDK under:
`%ProgramFiles(x86)%\Windows Kits\10\Source\<version>\Samples\multimedia\directshow\baseclasses`

---

## Latency Targets

| Metric | Target | How Achieved |
|--------|--------|--------------|
| SRT network latency | <80ms p50 | `latency=80000µs` in SRT URL |
| SHM write time | <2ms p50, <10ms p99 | Direct `memcpy` under Win32 mutex |
| H.264 encode | Minimal | `preset=ultrafast tune=zerolatency bframes=0` |
| Total pipeline | <150ms p99 | SRT + decode + SHM ≈ 100–130ms on LAN |

---

## Next Steps

1. **Build the DLL** — install Visual Studio 2022 Build Tools + DirectShow base classes
2. **Code signing** — for public distribution, obtain an EV cert and sign `camnet_vcam.dll`
3. **Virtual Audio Device** — add a virtual audio device (`camnet_vaudio.dll`) so OBS can route sender mic audio too
4. **Auto-discovery UI** — add a receiver GUI that shows a list of discovered senders with one-click connect
5. **Android/iOS native app** — replace the browser sender with a React Native app for better camera API access and background operation
