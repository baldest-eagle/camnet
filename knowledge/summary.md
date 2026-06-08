# CamNet — Knowledge Summary (Tier-1 RAG)

**Project:** CamNet  
**Location:** `c:\Users\kyleh\Desktop\camnet`  
**Status:** Scaffolded — awaiting C++ driver build and OBS integration test  
**Config:** `camnet.antigravity.json`

---

## What CamNet Does

Streams any camera on the local network (phone, secondary PC, IP cam) into
**OBS Studio** as a native `Video Capture Device` named **"CamNet Virtual Camera"**.
No browser source, no OBS plugins to install — it appears in the DirectShow device list.

---

## System Architecture

```
[Sender] Camera → FFmpeg H.264+AAC → SRT listener (port 9000) → mDNS announce
                                                       ↓ LAN SRT stream
[Receiver] SRT ingest → FFmpeg decode → BGRA frames → Win32 Named SHM
                                                       ↓ Global\CamNetFrame
[Driver]   C++ DirectShow CSource filter (camnet_vcam.dll) → OBS
```

---

## Key Technical Details

| Detail | Value |
|--------|-------|
| Stream protocol | SRT (primary), WebRTC (browser fallback) |
| Codec | H.264 Baseline, zerolatency, bframes=0 + AAC 48kHz stereo |
| Target resolution | 1920×1080 @ 60fps (30fps fallback) |
| SRT latency buffer | 80ms |
| IPC mechanism | Win32 `CreateFileMapping` — `Global\CamNetFrame` |
| IPC sync | Win32 Named Mutex — `Global\CamNetMutex` |
| Filter GUID | `{B5C7E300-6D41-4A8E-9F12-3C4D5E6F7A8B}` |
| REST API port | `http://localhost:7432/` |
| mDNS service type | `_camnet._tcp.local.` |
| Audio | Yes — PCM s16le 48kHz stereo routed alongside video in SHM |

---

## Shared Memory Layout (offset map)

```
Offset  0: uint32  magic        = 0xCAFECAFE
Offset  4: uint32  width
Offset  8: uint32  height
Offset 12: uint32  fps
Offset 16: uint64  frame_index
Offset 24: uint64  timestamp_ms
Offset 32: uint32  audio_chunk_size
Offset 36: uint32  flags (bit0=has_audio)
Offset 40: raw BGRA pixels (width * height * 4)
Offset 40+px: PCM audio chunk (up to 192000 bytes)
```

---

## File Map

```
camnet/
├── camnet.antigravity.json   ← Antigravity IDE project config (run targets, debug, env)
├── README.md
├── knowledge/
│   └── summary.md            ← THIS FILE (Tier-1 RAG)
│
├── sender/                   ← Cross-platform Python sender
│   ├── main.py               ← CLI + tray orchestrator
│   ├── capture.py            ← OpenCV/GStreamer capture, 1080p60, BGRA
│   ├── encoder.py            ← FFmpeg H.264 encoder + GStreamer HW fallback
│   ├── streamer.py           ← SRT server-mode + WebRTC aiortc fallback
│   ├── discovery.py          ← zeroconf mDNS announcer
│   ├── web/
│   │   ├── index.html        ← Mobile browser sender UI (glassmorphism)
│   │   └── sender.js         ← WebRTC getUserMedia + live stats
│   └── requirements.txt
│
├── receiver/                 ← Windows primary PC backend
│   ├── main.py               ← Orchestrator, auto-connect, frame pump
│   ├── discovery.py          ← zeroconf mDNS browser + DeviceRegistry
│   ├── ingest.py             ← FFmpeg SRT caller, BGRA+PCM pipes, reconnect backoff
│   ├── shm_writer.py         ← Win32 Named SHM + mutex frame writer
│   ├── controller.py         ← Flask REST API (:7432)
│   └── requirements.txt
│
├── driver/                   ← C++ DirectShow COM filter
│   ├── src/
│   │   ├── camnet_vcam.h     ← Filter GUID, SHM layout constants, class decls
│   │   ├── camnet_vcam.cpp   ← CSource filter + FillBuffer SHM reader
│   │   └── dllmain.cpp       ← DllMain → strmbase
│   ├── CMakeLists.txt        ← MSVC build, strmbase link, DEF file
│   └── README.md             ← Build guide + DirectShow base class setup
│
├── driver_installer/
│   ├── install.py            ← CMake build + System32 copy + regsvr32 + UAC
│   └── uninstall.py          ← regsvr32 /u + remove + registry cleanup
│
└── tests/
    ├── conftest.py           ← Shared fixtures (announcer, discovery, tmp_shm_name)
    ├── test_discovery.py     ← mDNS round-trip tests (4 test classes)
    ├── test_shm.py           ← SHM open/write/read/mutex (6 tests)
    └── test_latency.py       ← Throughput + dropout + p50/p99 benchmark (4 tests)
```

---

## Quick Commands (from project root)

```powershell
# Receiver (primary PC)
py receiver/main.py

# Sender (secondary PC)
py sender/main.py --device 0 --fps 60

# Install driver (as Admin)
py driver_installer/install.py --skip-build

# Run all tests
py -m pytest tests/ -v

# Check receiver API
curl http://localhost:7432/status
```

---

## Current Status / Next Steps

- [x] All Python modules written (sender, receiver, tests)
- [x] C++ DirectShow filter written (camnet_vcam.cpp)
- [x] CMake build configured
- [x] Installer written
- [ ] **Build DLL** — needs VS2022 Build Tools + DirectShow base classes
- [ ] **OBS integration test** — register DLL, start receiver+sender, verify in OBS
- [ ] **Audio virtual device** — add camnet_vaudio.dll for mic passthrough to OBS
