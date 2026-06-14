# CamNet Driver — DirectShow Virtual Camera

The `driver/` directory contains a C++ DirectShow COM filter that registers
as a Windows video capture device named **"CamNet Virtual Camera"**.

## How It Works

```
Python Receiver (shm_writer.py)
        │
        │  Win32 Named Shared Memory
        │  Global\CamNetFrame
        │  + Named Mutex Global\CamNetMutex
        ▼
C++ DirectShow Filter (camnet_vcam.dll)
  CSourceStream::FillBuffer()
        │
        │  DirectShow push-mode graph
        ▼
OBS Studio → Video Capture Device → "CamNet Virtual Camera"
```

## Shared Memory Layout

| Offset | Type    | Field              |
|--------|---------|--------------------|
| 0      | uint32  | magic = 0xCAFECAFE |
| 4      | uint32  | width              |
| 8      | uint32  | height             |
| 12     | uint32  | fps                |
| 16     | uint64  | frame_index        |
| 24     | uint64  | timestamp_ms       |
| 32     | uint32  | audio_chunk_size   |
| 36     | uint32  | flags (bit0=audio) |
| 40     | bytes   | raw BGRA pixels    |
| 40+px  | bytes   | PCM audio chunk    |

## Build Requirements

- **Windows 10/11 x64**
- **Visual Studio 2022** with "Desktop development with C++" workload
- **CMake 3.20+** (included with VS2022 or installable separately)
- **Windows SDK 10.0+** (for DirectShow headers)

### DirectShow Base Classes

The build requires the DirectShow base classes (`strmbase.lib` / `strmbasd.lib`).
These are included in the Windows SDK samples or can be obtained via:

**Option A — SDK Samples** (preferred):
```
%ProgramFiles(x86)%\Windows Kits\10\Source\<version>\ucrt\
```
Look for the `baseclasses` folder under `Samples\multimedia\directshow\baseclasses`.

**Option B — Pre-compiled NuGet**:
```powershell
# From the driver/ directory
nuget install directshow-baseclasses
```

**Option C — vcpkg**:
```powershell
vcpkg install directshow
```

## Build Steps

```powershell
# From the driver/ directory
cmake -B build -G "Visual Studio 17 2022" -A x64 `
      -DDSHOW_BASECLASSES="C:\path\to\baseclasses"

cmake --build build --config Release
```

The built `camnet_vcam.dll` will be automatically copied to `../driver_installer/`.

## Installation

After building, run the installer (as Administrator):

```powershell
cd ..\driver_installer
py install.py
```

Or for development (unsigned DLL, test-signing mode):

```powershell
py install.py --test-mode
# Reboot required for test-signing to take effect
py install.py --skip-build
```

## Manual Registration / Unregistration

```powershell
# Register (run as Administrator)
regsvr32 camnet_vcam.dll

# Unregister
regsvr32 /u camnet_vcam.dll
```

## Verification

After installation, open OBS Studio and add a source:
- **Sources panel** → `+` → **Video Capture Device**
- In the device dropdown, you should see **"CamNet Virtual Camera"**

If it doesn't appear, try:
1. Restart OBS Studio
2. Verify registry: `reg query "HKCR\CLSID\{B5C7E300-6D41-4A8E-9F12-3C4D5E6F7A8B}"`
3. Check the system Event Log for COM registration errors

## Code Signing (Production)

For production distribution without test-signing mode:
1. Obtain an EV Code Signing Certificate (DigiCert, Sectigo, etc.)
2. Sign the DLL: `signtool sign /fd sha256 /tr http://timestamp.digicert.com camnet_vcam.dll`
3. Verify: `signtool verify /pa camnet_vcam.dll`

WHQL signing is **not** required for DirectShow filters (only kernel-mode drivers need WHQL).
