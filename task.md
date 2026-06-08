# CamNet — Task Tracker

## Phase 1: Project Scaffolding
- [x] Create implementation plan artifact
- [x] Create full directory structure + config files
- [x] Write top-level README.md

## Phase 2: Sender Application
- [x] `sender/discovery.py` — mDNS announcer (subagent A)
- [x] `sender/capture.py` — camera capture + 1080p60 config
- [x] `sender/encoder.py` — H.264 + AAC pipeline (60fps support)
- [x] `sender/streamer.py` — SRT output (video + audio)
- [x] `sender/main.py` — CLI entry point + tray icon
- [x] `sender/web/index.html` + `sender.js` — browser sender
- [x] `sender/requirements.txt`

## Phase 3: Receiver Backend
- [x] `receiver/discovery.py` — mDNS browser (subagent A)
- [x] `receiver/ingest.py` — SRT ingest + decode (subagent B)
- [x] `receiver/shm_writer.py` — Win32 shared memory writer (subagent B)
- [x] `receiver/controller.py` — REST API
- [x] `receiver/main.py` — tray GUI + orchestration
- [x] `receiver/requirements.txt`

## Phase 4: DirectShow Virtual Camera Driver (C++)
- [x] `driver/src/camnet_vcam.h` — header + SHM layout constants
- [x] `driver/src/camnet_vcam.cpp` — filter + pin implementation
- [x] `driver/src/dllmain.cpp` — DLL entry point
- [x] `driver/CMakeLists.txt` — build configuration
- [x] `driver/README.md` — build + install guide

## Phase 5: Driver Installer
- [x] `driver_installer/install.py`
- [x] `driver_installer/uninstall.py`

## Phase 6: Test Suite
- [x] `tests/conftest.py` — shared fixtures
- [x] `tests/test_discovery.py` — mDNS round-trip tests
- [x] `tests/test_latency.py` — frame throughput + dropout simulation
- [x] `tests/test_shm.py` — shared memory integrity tests

## Phase 7: Verification
- [ ] Run `py -m pytest tests/ -v` (requires FFmpeg + zeroconf installed)
- [ ] Manual OBS integration test (requires driver build + registration)
- [x] Project file structure verified (29 files)
