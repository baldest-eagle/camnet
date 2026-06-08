/*
 * driver/src/dllmain.cpp
 * Standard DLL entry point for the CamNet DirectShow virtual camera filter.
 *
 * The strmbase DllEntryPoint handles COM class factory registration,
 * module handle caching, and safe DLL lifecycle management.
 *
 * This file is kept minimal — all substantive logic lives in camnet_vcam.cpp.
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <streams.h>

// strmbase requires DllEntryPoint to be declared externally
extern "C" BOOL WINAPI DllEntryPoint(HINSTANCE hInstance, ULONG ulReason, LPVOID pv);

BOOL WINAPI DllMain(HINSTANCE hDllHandle, DWORD dwReason, LPVOID lpReserved) {
    if (dwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hDllHandle);
    }
    return DllEntryPoint(hDllHandle, dwReason, lpReserved);
}
