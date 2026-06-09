cmake_minimum_required(VERSION 3.20)
project(camnet_vcam LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------------------------------------------------------------------------
# Build configuration
# ---------------------------------------------------------------------------
if(NOT MSVC)
    message(WARNING
        "CamNet DirectShow driver is designed for MSVC (Visual Studio 2019/2022). "
        "MinGW builds are not officially supported — strmbase linkage may fail. "
        "Recommended: cmake -G \"Visual Studio 17 2022\" -A x64 .."
    )
endif()

# Target Windows 10+ for shared memory API availability
add_compile_definitions(
    _WIN32_WINNT=0x0A00   # Windows 10
    WIN32_LEAN_AND_MEAN
    UNICODE
    _UNICODE
)

# ---------------------------------------------------------------------------
# DirectShow / Windows SDK paths
# ---------------------------------------------------------------------------
# The DirectShow base class library (strmbase.lib) ships with the Windows SDK
# under Samples\multimedia\directshow\baseclasses — or can be obtained from
# the "directshow-baseclasses" NuGet package.
#
# Set DSHOW_BASECLASSES to the strmbase source root, e.g.:
#   cmake -DDSHOW_BASECLASSES="C:/dshow_baseclasses" ..
# ---------------------------------------------------------------------------
if(NOT DEFINED DSHOW_BASECLASSES)
    # Try well-known SDK locations
    set(_SDK_ROOTS
        "$ENV{ProgramFiles\(x86\)}/Windows Kits/10/Include"
        "$ENV{ProgramFiles}/Windows Kits/10/Include"
    )

    # Look for strmif.h as a proxy for DirectShow headers
    find_path(DSHOW_INCLUDE_DIR
        NAMES strmif.h
        PATHS ${_SDK_ROOTS}
        PATH_SUFFIXES um
        DOC "DirectShow include directory"
    )

    if(NOT DSHOW_INCLUDE_DIR)
        message(WARNING
            "DirectShow headers not auto-detected. "
            "Pass -DDSHOW_BASECLASSES=<path> to cmake. "
            "See driver/README.md for setup instructions."
        )
    endif()
else()
    # User supplied baseclasses path — build strmbase from source
    set(STRMBASE_SOURCES
        ${DSHOW_BASECLASSES}/amextra.cpp
        ${DSHOW_BASECLASSES}/amfilter.cpp
        ${DSHOW_BASECLASSES}/amvideo.cpp
        ${DSHOW_BASECLASSES}/arithutil.cpp
        ${DSHOW_BASECLASSES}/combase.cpp
        ${DSHOW_BASECLASSES}/cprop.cpp
        ${DSHOW_BASECLASSES}/ctlutil.cpp
        ${DSHOW_BASECLASSES}/ddmm.cpp
        ${DSHOW_BASECLASSES}/dllentry.cpp
        ${DSHOW_BASECLASSES}/dllsetup.cpp
        ${DSHOW_BASECLASSES}/mtype.cpp
        ${DSHOW_BASECLASSES}/outputq.cpp
        ${DSHOW_BASECLASSES}/perflog.cpp
        ${DSHOW_BASECLASSES}/pstream.cpp
        ${DSHOW_BASECLASSES}/pullpin.cpp
        ${DSHOW_BASECLASSES}/refclock.cpp
        ${DSHOW_BASECLASSES}/renbase.cpp
        ${DSHOW_BASECLASSES}/schedule.cpp
        ${DSHOW_BASECLASSES}/seekpt.cpp
        ${DSHOW_BASECLASSES}/source.cpp
        ${DSHOW_BASECLASSES}/strmctl.cpp
        ${DSHOW_BASECLASSES}/sysclock.cpp
        ${DSHOW_BASECLASSES}/transfrm.cpp
        ${DSHOW_BASECLASSES}/transip.cpp
        ${DSHOW_BASECLASSES}/videoctl.cpp
        ${DSHOW_BASECLASSES}/vtrans.cpp
        ${DSHOW_BASECLASSES}/winctrl.cpp
        ${DSHOW_BASECLASSES}/winutil.cpp
        ${DSHOW_BASECLASSES}/wxdebug.cpp
        ${DSHOW_BASECLASSES}/wxlist.cpp
        ${DSHOW_BASECLASSES}/wxutil.cpp
    )

    add_library(strmbase STATIC ${STRMBASE_SOURCES})
    target_include_directories(strmbase PUBLIC ${DSHOW_BASECLASSES})
    target_compile_options(strmbase PRIVATE /W0)  # Suppress warnings in base classes
    set(STRMBASE_LIB strmbase)
endif()

# ---------------------------------------------------------------------------
# camnet_vcam DLL target
# ---------------------------------------------------------------------------
add_library(camnet_vcam SHARED
    src/dllmain.cpp
    src/camnet_vcam.cpp
    src/camnet_vcam.h
)

target_include_directories(camnet_vcam PRIVATE
    src/
    ${DSHOW_BASECLASSES}
)

# Link required Windows + DirectShow libraries
target_link_libraries(camnet_vcam PRIVATE
    # DirectShow base classes (either pre-built or our static build above)
    $<TARGET_NAME_IF_EXISTS:strmbase>

    # Windows SDK libs
    strmiids        # DirectShow GUIDs
    uuid            # COM GUIDs
    winmm           # Multimedia timer
    ole32           # COM runtime
    oleaut32        # OLE automation
    comctl32        # Common controls (for setup dialogs)

    # Ensure DLL exports are correct
    kernel32
    user32
)

# Compile options for the DLL
target_compile_options(camnet_vcam PRIVATE
    /W3             # Warning level 3
    /WX-            # Don't treat warnings as errors (strmbase has some)
    /Zi             # Debug info even in Release (for crash diagnosis)
    /O2             # Optimize for speed
    /EHsc           # C++ exception handling
)

# ---------------------------------------------------------------------------
# DEF file to export COM registration entry points
# ---------------------------------------------------------------------------
set(DEF_FILE "${CMAKE_CURRENT_BINARY_DIR}/camnet_vcam.def")
file(WRITE ${DEF_FILE}
"LIBRARY camnet_vcam\n"
"EXPORTS\n"
"    DllMain\n"
"    DllGetClassObject   PRIVATE\n"
"    DllCanUnloadNow     PRIVATE\n"
"    DllRegisterServer   PRIVATE\n"
"    DllUnregisterServer PRIVATE\n"
)
target_link_options(camnet_vcam PRIVATE /DEF:${DEF_FILE})

# ---------------------------------------------------------------------------
# Post-build: copy DLL to a convenient location
# ---------------------------------------------------------------------------
add_custom_command(TARGET camnet_vcam POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:camnet_vcam>
        ${CMAKE_SOURCE_DIR}/../driver_installer/camnet_vcam.dll
    COMMENT "Copying camnet_vcam.dll to driver_installer/"
)

# ---------------------------------------------------------------------------
# Install target (optional: cmake --install build --prefix C:/camnet)
# ---------------------------------------------------------------------------
install(TARGETS camnet_vcam
    RUNTIME DESTINATION bin
)
install(FILES
    $<TARGET_PDB_FILE:camnet_vcam>
    DESTINATION bin
    OPTIONAL
)
