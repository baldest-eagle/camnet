/*
 * driver/src/camnet_vcam.cpp
 * CamNet Virtual Camera — DirectShow source filter implementation.
 *
 * This COM DLL appears in Windows as a video capture device named
 * "CamNet Virtual Camera". When OBS Studio (or any DirectShow client)
 * opens the device, FillBuffer() is called for each frame and reads
 * the latest BGRA frame from the Win32 Named Shared Memory written by
 * the Python receiver/shm_writer.py.
 *
 * Build instructions:
 *   cmake -B build -G "Visual Studio 17 2022" -A x64
 *   cmake --build build --config Release
 *   regsvr32 build\Release\camnet_vcam.dll
 */

#include "camnet_vcam.h"

#include <algorithm>
#include <cstring>
#include <cassert>

// --------------------------------------------------------------------------
// DirectShow filter registration tables
// --------------------------------------------------------------------------

// Filter registration info (used by DllRegisterServer / AMovieDllRegisterServer2)
const AMOVIESETUP_MEDIATYPE sudOutputPinTypes[] = {
    {
        &MEDIATYPE_Video,
        &MEDIASUBTYPE_RGB32   // BGRA maps to RGB32 in DirectShow
    }
};

const AMOVIESETUP_PIN sudOutputPin[] = {
    {
        const_cast<LPWSTR>(CAMNET_PIN_NAME),
        FALSE,                  // Is it rendered?
        TRUE,                   // Is it an output pin?
        FALSE,                  // Zero instances allowed?
        FALSE,                  // Many instances allowed?
        &CLSID_NULL,
        nullptr,
        1,
        sudOutputPinTypes
    }
};

const AMOVIESETUP_FILTER sudFilter = {
    &CLSID_CamNetVirtualCamera,
    CAMNET_FILTER_NAME,
    MERIT_DO_NOT_USE,           // Don't auto-insert in filter graphs
    1,
    sudOutputPin
};

// Required by strmbase for COM registration
CFactoryTemplate g_Templates[] = {
    {
        CAMNET_FILTER_NAME,
        &CLSID_CamNetVirtualCamera,
        CCamNetVCam::CreateInstance,
        nullptr,
        &sudFilter
    }
};
int g_cTemplates = ARRAYSIZE(g_Templates);

// --------------------------------------------------------------------------
// CCamNetVCam implementation
// --------------------------------------------------------------------------

CCamNetVCam::CCamNetVCam(LPUNKNOWN pUnk, HRESULT* phr)
    : CSource(CAMNET_FILTER_NAME, pUnk, CLSID_CamNetVirtualCamera)
{
    m_pPin = new CCamNetVCamPin(phr, this, CAMNET_PIN_NAME);
    if (FAILED(*phr) || !m_pPin) {
        *phr = E_OUTOFMEMORY;
        return;
    }
    *phr = S_OK;
}

/* static */
CUnknown* WINAPI CCamNetVCam::CreateInstance(LPUNKNOWN pUnk, HRESULT* phr) {
    auto* pFilter = new CCamNetVCam(pUnk, phr);
    if (FAILED(*phr)) {
        delete pFilter;
        return nullptr;
    }
    return pFilter;
}

STDMETHODIMP CCamNetVCam::NonDelegatingQueryInterface(REFIID riid, void** ppv) {
    if (riid == IID_IAMFilterMiscFlags) {
        return GetInterface(static_cast<IAMFilterMiscFlags*>(this), ppv);
    }
    return CSource::NonDelegatingQueryInterface(riid, ppv);
}

// --------------------------------------------------------------------------
// CCamNetVCamPin implementation
// --------------------------------------------------------------------------

CCamNetVCamPin::CCamNetVCamPin(HRESULT* phr, CSource* pParent, LPCWSTR pPinName)
    : CSourceStream(pPinName, phr, pParent, pPinName)
    , m_hMapping(nullptr)
    , m_hMutex(nullptr)
    , m_pView(nullptr)
    , m_nWidth(CAMNET_DEFAULT_WIDTH)
    , m_nHeight(CAMNET_DEFAULT_HEIGHT)
    , m_nFPS(CAMNET_DEFAULT_FPS)
    , m_lastFrameIndex(UINT64_MAX)
    , m_rtStart(0)
    , m_shmOpen(false)
    , m_lastFrameTimeMs(0)
{
    // 100-ns units per frame: 10,000,000 / fps
    m_rtFrameDuration = static_cast<LONGLONG>(10000000LL / m_nFPS);
    InitializeCriticalSection(&m_cs);

    // Try to open shared memory immediately (may fail if receiver not running yet)
    OpenSharedMemory();

    *phr = S_OK;
}

CCamNetVCamPin::~CCamNetVCamPin() {
    CloseSharedMemory();
    DeleteCriticalSection(&m_cs);
}

// --------------------------------------------------------------------------
// GetMediaType — negotiate video format with the consuming application
// --------------------------------------------------------------------------
HRESULT CCamNetVCamPin::GetMediaType(CMediaType* pmt) {
    CheckPointer(pmt, E_POINTER);
    CAutoLock cAutoLock(m_pFilter->pStateLock());

    // Build VIDEOINFOHEADER for RGB32 (BGRA)
    auto* pvi = reinterpret_cast<VIDEOINFOHEADER*>(
        pmt->AllocFormatBuffer(sizeof(VIDEOINFOHEADER))
    );
    if (!pvi) return E_OUTOFMEMORY;
    ZeroMemory(pvi, sizeof(VIDEOINFOHEADER));

    pvi->AvgTimePerFrame = m_rtFrameDuration;
    pvi->bmiHeader.biSize        = sizeof(BITMAPINFOHEADER);
    pvi->bmiHeader.biWidth       = static_cast<LONG>(m_nWidth);
    pvi->bmiHeader.biHeight      = -static_cast<LONG>(m_nHeight); // Top-down
    pvi->bmiHeader.biPlanes      = 1;
    pvi->bmiHeader.biBitCount    = 32;
    pvi->bmiHeader.biCompression = BI_RGB;
    pvi->bmiHeader.biSizeImage   = m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES;

    // Set video bitrate estimate (informational)
    pvi->dwBitRate = m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES * 8 * m_nFPS;

    SetRectEmpty(&pvi->rcSource);
    SetRectEmpty(&pvi->rcTarget);

    pmt->SetType(&MEDIATYPE_Video);
    pmt->SetFormatType(&FORMAT_VideoInfo);
    pmt->SetTemporalCompression(FALSE);
    pmt->SetSubtype(&MEDIASUBTYPE_RGB32);
    pmt->SetSampleSize(pvi->bmiHeader.biSizeImage);

    return S_OK;
}

// --------------------------------------------------------------------------
// CheckMediaType — accept only RGB32 at our dimensions
// --------------------------------------------------------------------------
HRESULT CCamNetVCamPin::CheckMediaType(const CMediaType* pmt) {
    CheckPointer(pmt, E_POINTER);

    if (pmt->majortype != MEDIATYPE_Video)        return E_INVALIDARG;
    if (pmt->subtype   != MEDIASUBTYPE_RGB32)     return E_INVALIDARG;
    if (pmt->formattype != FORMAT_VideoInfo)      return E_INVALIDARG;
    if (!pmt->pbFormat)                           return E_INVALIDARG;

    auto* pvi = reinterpret_cast<VIDEOINFOHEADER*>(pmt->pbFormat);
    if (pvi->bmiHeader.biWidth  != static_cast<LONG>(m_nWidth))  return E_INVALIDARG;
    if (abs(pvi->bmiHeader.biHeight) != static_cast<LONG>(m_nHeight)) return E_INVALIDARG;

    return S_OK;
}

// --------------------------------------------------------------------------
// DecideBufferSize — tell the allocator how large each sample should be
// --------------------------------------------------------------------------
HRESULT CCamNetVCamPin::DecideBufferSize(IMemAllocator* pAlloc,
                                          ALLOCATOR_PROPERTIES* pRequest) {
    CheckPointer(pAlloc, E_POINTER);
    CheckPointer(pRequest, E_POINTER);

    CAutoLock cAutoLock(m_pFilter->pStateLock());

    DWORD cbFrame = m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES;
    pRequest->cBuffers  = 2;     // Double-buffered
    pRequest->cbBuffer  = static_cast<long>(cbFrame);
    pRequest->cbAlign   = 1;
    pRequest->cbPrefix  = 0;

    ALLOCATOR_PROPERTIES actual;
    HRESULT hr = pAlloc->SetProperties(pRequest, &actual);
    if (FAILED(hr)) return hr;
    if (actual.cbBuffer < static_cast<long>(cbFrame)) return E_FAIL;

    return S_OK;
}

// --------------------------------------------------------------------------
// FillBuffer — called by CSourceStream thread for each video frame.
//
// This is the hot path. It:
// 1. Acquires the Win32 mutex (with timeout)
// 2. Checks shared memory magic and frame freshness
// 3. Copies pixel data into the DirectShow media sample
// 4. Sets PTS / DTS on the sample
// --------------------------------------------------------------------------
HRESULT CCamNetVCamPin::FillBuffer(IMediaSample* pSample) {
    CheckPointer(pSample, E_POINTER);

    BYTE* pData = nullptr;
    HRESULT hr = pSample->GetPointer(&pData);
    if (FAILED(hr) || !pData) return hr;

    DWORD cbFrame = m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES;

    // Attempt to re-open SHM if it was closed (receiver restarted)
    if (!m_shmOpen) {
        OpenSharedMemory();
    }

    bool frameCopied = false;
    if (m_shmOpen) {
        frameCopied = ReadLatestFrame(pData, cbFrame);
    }

    if (!frameCopied) {
        // Output a solid black frame so OBS doesn't freeze
        FillBlackFrame(pData, cbFrame);
    }

    // Set media sample properties
    pSample->SetActualDataLength(static_cast<long>(cbFrame));
    pSample->SetSyncPoint(TRUE);
    pSample->SetPreroll(FALSE);
    pSample->SetDiscontinuity(m_rtStart == 0 ? TRUE : FALSE);

    // Timestamps in 100-nanosecond units
    REFERENCE_TIME rtEnd = m_rtStart + m_rtFrameDuration;
    pSample->SetTime(&m_rtStart, &rtEnd);
    m_rtStart = rtEnd;

    // Pace delivery to target FPS
    // CSourceStream's worker thread sleeps based on AvgTimePerFrame
    // so we don't need an explicit sleep here.

    return S_OK;
}

// --------------------------------------------------------------------------
// ReadLatestFrame — read from shared memory with mutex protection
// --------------------------------------------------------------------------
bool CCamNetVCamPin::ReadLatestFrame(BYTE* pDest, DWORD bufSize) {
    if (!m_pView || !m_hMutex) return false;

    DWORD waitResult = WaitForSingleObject(m_hMutex, CAMNET_MUTEX_TIMEOUT_MS);
    if (waitResult != WAIT_OBJECT_0 && waitResult != WAIT_ABANDONED) {
        // Timeout — use whatever was last written (stale frame)
        return false;
    }

    bool success = false;
    __try {
        auto* header = reinterpret_cast<const CamNetShmHeader*>(
            const_cast<BYTE*>(m_pView)
        );

        // Validate magic number
        if (header->magic != CAMNET_SHM_MAGIC) {
            __leave;
        }

        // Check frame staleness
        DWORD nowMs = GetTickCount();
        uint64_t frameAgeMs = static_cast<uint64_t>(nowMs) -
                              static_cast<uint64_t>(header->timestamp_ms % UINT32_MAX);
        // Use frame_index instead for freshness check
        if (header->frame_index == m_lastFrameIndex) {
            // Same frame as last time — still deliver it (receiver may be slow)
            // but track time so we can fall back to black if really stale
            if (nowMs - m_lastFrameTimeMs > CAMNET_FRAME_TIMEOUT_MS * 5) {
                __leave;  // Too stale — black frame
            }
        } else {
            m_lastFrameTimeMs = nowMs;
        }
        m_lastFrameIndex = header->frame_index;

        // Update dimensions if receiver changed them
        if (header->width > 0 && header->width <= 4096 &&
            header->height > 0 && header->height <= 4096) {
            m_nWidth  = header->width;
            m_nHeight = header->height;
            if (header->fps > 0 && header->fps <= 120) {
                m_nFPS = header->fps;
                m_rtFrameDuration = 10000000LL / m_nFPS;
            }
        }

        // Copy pixel data
        const BYTE* pPixels = (const BYTE*)(m_pView) + CAMNET_HEADER_SIZE;
        DWORD pixelBytes = m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES;
        DWORD copyBytes  = std::min(pixelBytes, bufSize);
        memcpy(pDest, pPixels, copyBytes);
        success = true;
    }
    __finally {
        ReleaseMutex(m_hMutex);
    }

    return success;
}

// --------------------------------------------------------------------------
// FillBlackFrame — fill the buffer with opaque black BGRA pixels
// --------------------------------------------------------------------------
void CCamNetVCamPin::FillBlackFrame(BYTE* pDest, DWORD bufSize) {
    // BGRA: B=0, G=0, R=0, A=255
    for (DWORD i = 0; i < bufSize; i += 4) {
        pDest[i + 0] = 0;    // B
        pDest[i + 1] = 0;    // G
        pDest[i + 2] = 0;    // R
        pDest[i + 3] = 255;  // A
    }
}

// --------------------------------------------------------------------------
// OpenSharedMemory — open the named file mapping and mutex
// --------------------------------------------------------------------------
bool CCamNetVCamPin::OpenSharedMemory() {
    if (m_shmOpen) return true;

    // Open the file mapping (receiver must have created it first)
    m_hMapping = OpenFileMappingW(
        FILE_MAP_READ,
        FALSE,
        CAMNET_SHM_NAME
    );

    if (!m_hMapping) {
        // Receiver not running yet — will retry on next FillBuffer call
        return false;
    }

    // Calculate SHM size to map
    DWORD shmSize = CAMNET_HEADER_SIZE +
                    (m_nWidth * m_nHeight * CAMNET_PIXEL_BYTES) +
                    CAMNET_AUDIO_BUFFER;

    m_pView = reinterpret_cast<volatile BYTE*>(
        MapViewOfFile(m_hMapping, FILE_MAP_READ, 0, 0, shmSize)
    );

    if (!m_pView) {
        CloseHandle(m_hMapping);
        m_hMapping = nullptr;
        return false;
    }

    // Open the named mutex for synchronization
    m_hMutex = OpenMutexW(SYNCHRONIZE, FALSE, CAMNET_MUTEX_NAME);
    if (!m_hMutex) {
        UnmapViewOfFile(const_cast<BYTE*>(m_pView));
        m_pView = nullptr;
        CloseHandle(m_hMapping);
        m_hMapping = nullptr;
        return false;
    }

    m_shmOpen = true;
    OutputDebugStringW(L"[CamNet] Shared memory opened successfully.\n");
    return true;
}

// --------------------------------------------------------------------------
// CloseSharedMemory
// --------------------------------------------------------------------------
void CCamNetVCamPin::CloseSharedMemory() {
    m_shmOpen = false;

    if (m_hMutex) {
        CloseHandle(m_hMutex);
        m_hMutex = nullptr;
    }
    if (m_pView) {
        UnmapViewOfFile(const_cast<BYTE*>(m_pView));
        m_pView = nullptr;
    }
    if (m_hMapping) {
        CloseHandle(m_hMapping);
        m_hMapping = nullptr;
    }
}

// --------------------------------------------------------------------------
// IAMStreamConfig stubs (required for OBS to enumerate capabilities)
// --------------------------------------------------------------------------
HRESULT CCamNetVCamPin::GetNumberOfCapabilities(int* piCount, int* piSize) {
    if (!piCount || !piSize) return E_POINTER;
    *piCount = 1;
    *piSize  = sizeof(VIDEO_STREAM_CONFIG_CAPS);
    return S_OK;
}

HRESULT CCamNetVCamPin::GetStreamCaps(int iIndex, AM_MEDIA_TYPE** ppmt, BYTE* pSCC) {
    if (iIndex != 0) return E_INVALIDARG;
    if (!ppmt || !pSCC) return E_POINTER;

    HRESULT hr = GetMediaType(&m_mt);
    if (FAILED(hr)) return hr;

    *ppmt = CreateMediaType(&m_mt);
    if (!*ppmt) return E_OUTOFMEMORY;

    auto* pCaps = reinterpret_cast<VIDEO_STREAM_CONFIG_CAPS*>(pSCC);
    ZeroMemory(pCaps, sizeof(VIDEO_STREAM_CONFIG_CAPS));
    pCaps->guid               = FORMAT_VideoInfo;
    pCaps->VideoStandard      = 0;
    pCaps->InputSize.cx       = m_nWidth;
    pCaps->InputSize.cy       = m_nHeight;
    pCaps->MinCroppingSize    = pCaps->InputSize;
    pCaps->MaxCroppingSize    = pCaps->InputSize;
    pCaps->CropGranularityX   = 1;
    pCaps->CropGranularityY   = 1;
    pCaps->MinOutputSize      = pCaps->InputSize;
    pCaps->MaxOutputSize      = pCaps->InputSize;
    pCaps->OutputGranularityX = 1;
    pCaps->OutputGranularityY = 1;
    REFERENCE_TIME frameInterval = 10000000 / m_nFPS;
    pCaps->MinFrameInterval = frameInterval;
    pCaps->MaxFrameInterval = frameInterval;

    return S_OK;
}

HRESULT CCamNetVCamPin::SetFormat(AM_MEDIA_TYPE* pmt) {
    // For now we only support our fixed format
    return CheckMediaType(reinterpret_cast<CMediaType*>(pmt));
}

HRESULT CCamNetVCamPin::GetFormat(AM_MEDIA_TYPE** ppmt) {
    if (!ppmt) return E_POINTER;
    *ppmt = CreateMediaType(&m_mt);
    return *ppmt ? S_OK : E_OUTOFMEMORY;
}

HRESULT CCamNetVCamPin::Notify(IBaseFilter* /*pSelf*/, Quality /*q*/) {
    // Quality control: ignore for now (live source can't buffer ahead)
    return S_OK;
}

// --------------------------------------------------------------------------
// Required strmbase exports
// --------------------------------------------------------------------------
STDAPI DllRegisterServer() {
    return AMovieDllRegisterServer2(TRUE);
}

STDAPI DllUnregisterServer() {
    return AMovieDllRegisterServer2(FALSE);
}

extern "C" BOOL WINAPI DllEntryPoint(HINSTANCE, ULONG, LPVOID);


