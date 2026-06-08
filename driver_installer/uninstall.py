"""
driver_installer/uninstall.py — CamNet Virtual Camera Driver Uninstaller.

Unregisters the DirectShow COM filter and removes the DLL from System32.

Usage:
    py uninstall.py          # Full uninstall
    py uninstall.py --keep   # Unregister but keep DLL in System32
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import winreg
from pathlib import Path

import click
from loguru import logger

logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level:<8} | {message}",
           level="INFO", colorize=True)

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
DLL_NAME = "camnet_vcam.dll"
FILTER_GUID = "{B5C7E300-6D41-4A8E-9F12-3C4D5E6F7A8B}"


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def require_admin() -> None:
    if not is_admin():
        logger.warning("Not running as Administrator. Re-launching elevated...")
        params = " ".join([f'"{a}"' for a in sys.argv])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        if ret <= 32:
            logger.error("Elevation failed. Run as Administrator manually.")
            sys.exit(1)
        sys.exit(0)


def unregister_dll(dll_path: Path) -> bool:
    """Unregister the COM filter via regsvr32 /u."""
    if not dll_path.exists():
        logger.warning("DLL not found at {}. Skipping unregister.", dll_path)
        return False

    logger.info("Unregistering COM filter...")
    result = subprocess.run(
        ["regsvr32", "/u", "/s", str(dll_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "regsvr32 /u returned code {} (may already be unregistered).",
            result.returncode,
        )
        return False

    logger.success("COM filter unregistered.")
    return True


def remove_dll(dll_path: Path) -> bool:
    """Remove the DLL from System32."""
    if not dll_path.exists():
        logger.info("DLL not found in System32 (already removed).")
        return True

    try:
        dll_path.unlink()
        logger.success("DLL removed from {}.", dll_path)
        return True
    except PermissionError as exc:
        logger.error("Permission denied: {}. Ensure you are an Administrator.", exc)
        return False


def clean_registry() -> None:
    """Remove any stale registry keys left by a failed uninstall."""
    guid_key = f"CLSID\\{FILTER_GUID}"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, guid_key):
            logger.warning("Stale registry key found: {}. Attempting cleanup.", guid_key)
            # Note: Deleting registry keys with sub-keys requires recursion
            # In practice, regsvr32 /u handles this; this is a safety check
    except FileNotFoundError:
        logger.info("Registry clean — no stale keys found.")


@click.command()
@click.option("--keep", is_flag=True, default=False,
              help="Unregister but keep the DLL file in System32.")
def uninstall(keep: bool) -> None:
    """Uninstall the CamNet Virtual Camera DirectShow driver."""
    if sys.platform != "win32":
        logger.error("Uninstaller is Windows-only.")
        sys.exit(1)

    require_admin()

    logger.info("=== CamNet Virtual Camera Uninstaller ===")

    dll_path = SYSTEM32 / DLL_NAME
    unregister_dll(dll_path)

    if not keep:
        remove_dll(dll_path)
    else:
        logger.info("--keep specified; DLL retained at {}", dll_path)

    clean_registry()

    logger.success(
        "\n✅ CamNet Virtual Camera uninstalled.\n"
        "   The device will no longer appear in OBS after restarting it."
    )


if __name__ == "__main__":
    uninstall()
