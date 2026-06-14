"""
driver_installer/install.py — CamNet Virtual Camera Driver Installer.

Builds the DirectShow DLL (if needed), copies it to System32, and
registers it as a COM in-process server so OBS sees "CamNet Virtual Camera"
in the Video Capture Device list.

Usage:
    py install.py                    # Full install (build + register)
    py install.py --skip-build       # Register a pre-built DLL
    py install.py --test-mode        # Enable Windows test-signing (dev only)
    py install.py --dll path/to.dll  # Use a specific DLL path

Requirements:
    - Run as Administrator (UAC elevation required for System32 + regsvr32)
    - MSVC 2022 / Visual Studio Build Tools (for --build step)
    - CMake 3.20+ on PATH
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

import click
from loguru import logger

# Configure logger
logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level:<8} | {message}",
           level="INFO", colorize=True)

# Paths
THIS_DIR = Path(__file__).parent
DRIVER_DIR = THIS_DIR.parent / "driver"
DLL_NAME = "camnet_vcam.dll"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

# COM registry keys for our filter
FILTER_GUID = "{B5C7E300-6D41-4A8E-9F12-3C4D5E6F7A8B}"
FILTER_NAME = "CamNet Virtual Camera"
DS_CATEGORY  = "{860BB310-5D01-11d0-BD3B-00A0C911CE86}"  # CLSID_VideoInputDeviceCategory


# --------------------------------------------------------------------------
# Privilege check
# --------------------------------------------------------------------------

def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def require_admin() -> None:
    """Re-launch this script elevated if not already running as admin."""
    if not is_admin():
        logger.warning("Not running as Administrator. Re-launching elevated...")
        params = " ".join([f'"{a}"' for a in sys.argv])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        if ret <= 32:
            logger.error("Failed to elevate. Run this script as Administrator manually.")
            sys.exit(1)
        sys.exit(0)


# --------------------------------------------------------------------------
# Build step
# --------------------------------------------------------------------------

def build_dll(driver_dir: Path, config: str = "Release") -> Path:
    """
    Build the DirectShow DLL using CMake + MSVC.
    Returns the path to the built DLL.
    """
    build_dir = driver_dir / "build"
    build_dir.mkdir(exist_ok=True)

    # Check prerequisites
    for tool in ("cmake", "cl"):
        if not shutil.which(tool):
            raise EnvironmentError(
                f"'{tool}' not found on PATH. "
                "Install Visual Studio 2022 with C++ Desktop workload + CMake."
            )

    logger.info("Configuring CMake...")
    cmake_cmd = [
        "cmake",
        "-B", str(build_dir),
        "-G", "Visual Studio 17 2022",
        "-A", "x64",
        str(driver_dir),
    ]
    result = subprocess.run(cmake_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("CMake configure failed:\n{}", result.stderr)
        raise RuntimeError("CMake configuration failed.")
    logger.success("CMake configured.")

    logger.info("Building {} configuration...", config)
    build_cmd = [
        "cmake",
        "--build", str(build_dir),
        "--config", config,
        "--parallel",
    ]
    result = subprocess.run(build_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Build failed:\n{}", result.stderr)
        raise RuntimeError("Build failed.")

    dll_path = build_dir / config / DLL_NAME
    if not dll_path.exists():
        raise FileNotFoundError(f"Built DLL not found at {dll_path}")

    logger.success("DLL built: {}", dll_path)
    return dll_path


# --------------------------------------------------------------------------
# Install / uninstall
# --------------------------------------------------------------------------

def install_dll(src_dll: Path) -> Path:
    """Copy the DLL to System32 and return the destination path."""
    dest = SYSTEM32 / DLL_NAME
    logger.info("Copying {} → {}", src_dll.name, dest)
    shutil.copy2(str(src_dll), str(dest))
    logger.success("DLL installed to {}", dest)
    return dest


def register_dll(dll_path: Path) -> None:
    """Register the COM server with regsvr32."""
    logger.info("Registering COM filter with regsvr32...")
    result = subprocess.run(
        ["regsvr32", "/s", str(dll_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"regsvr32 failed (code {result.returncode}).\n"
            f"stderr: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )
    logger.success("COM filter registered successfully.")


def verify_registration() -> bool:
    """
    Verify the filter GUID is present in the Windows registry under
    both HKCR\\CLSID and the DirectShow video input device category.
    """
    guid_key = f"CLSID\\{FILTER_GUID}"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, guid_key) as k:
            name, _ = winreg.QueryValueEx(k, "")
            logger.info("Registry: CLSID found → {}", name)

        # Verify InprocServer32 exists
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            f"{guid_key}\\InprocServer32") as k:
            path, _ = winreg.QueryValueEx(k, "")
            logger.info("Registry: InprocServer32 → {}", path)

        return True
    except FileNotFoundError:
        logger.error("Registry key not found. Registration may have failed.")
        return False
    except Exception as exc:
        logger.error("Registry verification error: {}", exc)
        return False


def enable_test_signing() -> None:
    """
    Enable Windows test-signing mode (for unsigned DLL development).
    WARNING: Requires reboot. This weakens driver signature enforcement.
    """
    logger.warning(
        "Enabling test-signing mode (bcdedit /set testsigning on). "
        "A reboot is required."
    )
    result = subprocess.run(
        ["bcdedit", "/set", "testsigning", "on"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("bcdedit failed: {}", result.stderr)
    else:
        logger.success("Test signing enabled. Please reboot to apply.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.command()
@click.option("--skip-build", is_flag=True, default=False,
              help="Skip CMake build, use existing DLL.")
@click.option("--dll", type=click.Path(exists=True), default=None,
              help="Path to a pre-built DLL (skips build step).")
@click.option("--test-mode", is_flag=True, default=False,
              help="Enable Windows test-signing mode (dev only, requires reboot).")
@click.option("--no-copy", is_flag=True, default=False,
              help="Register DLL in-place, don't copy to System32.")
def install(
    skip_build: bool,
    dll: str | None,
    test_mode: bool,
    no_copy: bool,
) -> None:
    """Install the CamNet Virtual Camera DirectShow driver."""

    # Validate platform
    if sys.platform != "win32":
        logger.error("Installer is Windows-only.")
        sys.exit(1)

    if platform.machine().lower() not in ("amd64", "x86_64"):
        logger.warning("Warning: DLL built for x64. Ensure system matches.")

    # Ensure admin privileges
    require_admin()

    logger.info("=== CamNet Virtual Camera Installer ===")
    logger.info("Windows: {}", platform.version())

    # 1. Resolve DLL path
    if dll:
        dll_path = Path(dll)
        logger.info("Using provided DLL: {}", dll_path)
    elif skip_build:
        # Look for pre-built DLL in installer dir
        dll_path = THIS_DIR / DLL_NAME
        if not dll_path.exists():
            dll_path = DRIVER_DIR / "build" / "Release" / DLL_NAME
        if not dll_path.exists():
            logger.error(
                "No pre-built DLL found. Run without --skip-build to build first, "
                "or provide --dll path/to/camnet_vcam.dll"
            )
            sys.exit(1)
        logger.info("Using pre-built DLL: {}", dll_path)
    else:
        try:
            dll_path = build_dll(DRIVER_DIR)
        except (EnvironmentError, RuntimeError, FileNotFoundError) as exc:
            logger.error("Build failed: {}", exc)
            sys.exit(1)

    # 2. Enable test signing (optional, dev mode)
    if test_mode:
        enable_test_signing()

    # 3. Copy to System32
    if no_copy:
        install_path = dll_path
        logger.info("Registering in-place (not copying to System32).")
    else:
        try:
            install_path = install_dll(dll_path)
        except PermissionError as exc:
            logger.error("Permission denied copying to System32: {}", exc)
            sys.exit(1)

    # 4. Register COM server
    try:
        register_dll(install_path)
    except RuntimeError as exc:
        logger.error("Registration failed: {}", exc)
        sys.exit(1)

    # 5. Verify
    if verify_registration():
        logger.success(
            "\n✅ CamNet Virtual Camera installed successfully!\n"
            "   Open OBS → Sources → Video Capture Device\n"
            "   Select 'CamNet Virtual Camera'\n"
            "   Then start the receiver: py receiver/main.py"
        )
    else:
        logger.error(
            "\n❌ Installation may have failed — registry verification did not pass.\n"
            "   Try running as Administrator and check the driver/README.md."
        )
        sys.exit(1)


if __name__ == "__main__":
    install()
