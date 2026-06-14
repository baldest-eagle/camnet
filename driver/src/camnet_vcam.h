/*
 * driver/src/camnet_vcam.h
 * CamNet Virtual Camera — DirectShow source filter header.
 *
 * Implements a DirectShow CSource filter with a single output pin that
 * reads BGRA frames from Win32 Named Shared Memory and delivers them
 * to any application that requests a video capture device.
 *
 * The filter appears in Windows as "CamNet Virtual Camera" and is
 * selectable in OBS Studio under Sources > Video Capture Device.
 *
 * IPC layout (shared memory "Global\CamNetFrame"):
 *   Offset  0: uint32  magic        = 0xCAFECAFE
 *   Offset  4: uint32  width
 *   Offset  8: uint32  height
 *   Offset 12: uint32  fps
 *   Offset 16: uint64  frame_index  (monotonically increasing)
 *   Offset 24: uint64  timestamp_ms
 *   Offset 32: uint32  audio_chunk_size
 *   Offset 36: uint32  flags        (bit 0 = has_audio)
 *   Offset 40: raw BGRA pixels (width * height * 4 bytes)
 *
 * Build: CMake + MSVC 2022 or MinGW-w64
 * Register: regsvr32.exe camnet_vcam.dll
 */

#pragma once

#ifndef CAMNET_VCAM_H
#define CAMNET_VCAM_H

// Windows SDK and DirectShow headers
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <streams.h>       // DirectShow base classes (strmbasd.lib / strmbase.lib)
#include <initguid.h>
#include <uuids.h>
#include <dvdmedia.h>
#include <mfapi.h>

#include <cstdint>
#include <memory>
#include <string>
#include <atomic>

// --------------------------------------------------------------------------
// CamNet Filter GUID
// {B5C7E300-6D41-4A8E-9F12-3C4D5E6F7A8B}
// --------------------------------------------------------------------------
DEFINE_GUID(CLSID_CamNetVirtualCamera,
    0xb5c7e300, 0x6d41, 0x4a8e,
    0x9f, 0x12, 0x3c, 0x4d, 0x5e, 0x6f, 0x7a, 0x8b);

// --------------------------------------------------------------------------
// Shared memory layout constants
// --------------------------------------------------------------------------
constexpr uint32_t CAMNET_SHM_MAGIC        = 0xCAFECAFE;
constexpr DWORD    CAMNET_DEFAULT_WIDTH     = 1920;
constexpr DWORD    CAMNET_DEFAULT_HEIGHT    = 1080;
constexpr DWORD    CAMNET_DEFAULT_FPS       = 60;
constexpr DWORD    CAMNET_PIXEL_BYTES       = 4;            // BGRA
constexpr DWORD    CAMNET_HEADER_SIZE       = 40;
constexpr DWORD    CAMNET_AUDIO_BUFFER      = 192000;       // 1s @ 48kHz stereo s16le
constexpr LPCWSTR  CAMNET_SHM_NAME         = L"Global\\CamNetFrame";
constexpr LPCWSTR  CAMNET_MUTEX_NAME       = L"Global\\CamNetMutex";
constexpr LPCWSTR  CAMNET_FILTER_NAME      = L"CamNet Virtual Camera";
constexpr LPCWSTR  CAMNET_PIN_NAME         = L"Capture";
constexpr DWORD    CAMNET_MUTEX_TIMEOUT_MS = 100;
constexpr DWORD    CAMNET_FRAME_TIMEOUT_MS = 150;   // Max age of frame before black

// --------------------------------------------------------------------------
// Shared Memory Header (must match Python shm_writer.py layout exactly)
// --------------------------------------------------------------------------
#pragma pack(push, 1)
struct CamNetShmHeader {
    uint32_t magic;           // 0:  0xCAFECAFE
    uint32_t width;           // 4:  frame width
    uint32_t height;          // 8:  frame height
    uint32_t fps;             // 12: frame rate
    uint64_t frame_index;     // 16: monotonically increasing
    uint64_t timestamp_ms;    // 24: ms since Unix epoch
    uint32_t audio_chunk_size;// 32: bytes of audio after pixels
    uint32_t flags;           // 36: bit0 = has_audio
    // Pixel data follows at offset 40: width * height * 4 BGRA bytes
};
#pragma pack(pop)

// --------------------------------------------------------------------------
// Forward declarations
// --------------------------------------------------------------------------
class CCamNetVCamPin;
class CCamNetVCam;

// --------------------------------------------------------------------------
// CCamNetVCamPin — DirectShow output pin
//
// Inherits CSourceStream which handles the push-mode delivery loop.
// FillBuffer() is called by the base class thread for each frame.
// --------------------------------------------------------------------------
class CCamNetVCamPin : public CSourceStream {
public:
    CCamNetVCamPin(HRESULT* phr, CSource* pParent, LPCWSTR pPinName);
    virtual ~CCamNetVCamPin();

    // CSourceStream overrides
    HRESULT GetMediaType(CMediaType* pmt) override;
    HRESULT CheckMediaType(const CMediaType* pmt) override;
    HRESULT DecideBufferSize(IMemAllocator* pAlloc,
                             ALLOCATOR_PROPERTIES* pProperties) override;
    HRESULT FillBuffer(IMediaSample* pSample) override;

    // IAMStreamConfig (needed for OBS to negotiate resolution/FPS)
    HRESULT STDMETHODCALLTYPE GetNumberOfCapabilities(int* piCount, int* piSize);
    HRESULT STDMETHODCALLTYPE GetStreamCaps(int iIndex, AM_MEDIA_TYPE** ppmt,
                                             BYTE* pSCC);
    HRESULT STDMETHODCALLTYPE SetFormat(AM_MEDIA_TYPE* pmt);
    HRESULT STDMETHODCALLTYPE GetFormat(AM_MEDIA_TYPE** ppmt);

    // IQualityControl
    HRESULT STDMETHODCALLTYPE Notify(IBaseFilter* pSelf, Quality q) override;

private:
    // Shared memory reader
    bool    OpenSharedMemory();
    void    CloseSharedMemory();
    bool    ReadLatestFrame(BYTE* pDest, DWORD bufSize);
    void    FillBlackFrame(BYTE* pDest, DWORD bufSize);

    // State
    HANDLE              m_hMapping;
    HANDLE              m_hMutex;
    volatile BYTE*      m_pView;
    DWORD               m_nWidth;
    DWORD               m_nHeight;
    DWORD               m_nFPS;
    uint64_t            m_lastFrameIndex;
    LONGLONG            m_rtFrameDuration;  // 100-ns units per frame
    REFERENCE_TIME      m_rtStart;
    std::atomic<bool>   m_shmOpen;
    CRITICAL_SECTION    m_cs;
    DWORD               m_lastFrameTimeMs;  // for stale detection
};

// --------------------------------------------------------------------------
// CCamNetVCam — DirectShow source filter
//
// Implements IBaseFilter / CSource. One output pin (CCamNetVCamPin).
// Exposes IAMFilterMiscFlags so OBS knows this is a live source.
// --------------------------------------------------------------------------
class CCamNetVCam : public CSource,
                    public IAMFilterMiscFlags {
public:
    // COM creation
    static CUnknown* WINAPI CreateInstance(LPUNKNOWN pUnk, HRESULT* phr);

    // IUnknown (delegated to CSource)
    DECLARE_IUNKNOWN;
    STDMETHODIMP NonDelegatingQueryInterface(REFIID riid, void** ppv) override;

    // IAMFilterMiscFlags
    ULONG STDMETHODCALLTYPE GetMiscFlags() override {
        return AM_FILTER_MISC_FLAGS_IS_SOURCE;
    }

private:
    CCamNetVCam(LPUNKNOWN pUnk, HRESULT* phr);
    CCamNetVCamPin* m_pPin;
};

#endif // CAMNET_VCAM_H
