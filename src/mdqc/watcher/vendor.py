from __future__ import annotations

import sys
from pathlib import Path

from mdqc.config.defaults import BRUKER_STABILITY_WINDOW_S
from mdqc.types import Vendor


def try_exclusive_open(path: Path) -> bool:
    if not path.exists() or path.is_dir():
        return False
    if sys.platform == "win32":
        try:
            import pywintypes
            import win32con
            import win32file
        except ImportError:
            return _fallback_open(path)
        try:
            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_READ,
                0,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )
        except pywintypes.error:
            return False
        handle.Close()
        return True
    return _fallback_open(path)


def _fallback_open(path: Path) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def is_artifact_complete(path: Path, vendor: Vendor) -> bool:
    if not path.exists():
        return False

    if vendor is Vendor.THERMO:
        if not path.is_file():
            return False
        return try_exclusive_open(path)

    if vendor is Vendor.BRUKER:
        if not path.is_dir():
            return False
        analysis = path / "analysis.tdf"
        if not analysis.is_file():
            return False
        if (path / "analysis.tdf-journal").exists():
            return False
        if (path / "analysis.tdf-lock").exists():
            return False
        return try_exclusive_open(analysis)

    if vendor is Vendor.WATERS:
        if not path.is_dir():
            return False
        func = path / "_FUNC001.DAT"
        if not func.is_file():
            return False
        return try_exclusive_open(func)

    if vendor is Vendor.SCIEX:
        if not path.is_file():
            return False
        scan = Path(str(path) + ".scan")
        if not scan.is_file():
            return False
        return try_exclusive_open(path) and try_exclusive_open(scan)

    if vendor is Vendor.AGILENT:
        if not path.is_dir():
            return False
        acq = path / "AcqData"
        if not acq.is_dir():
            return False
        try:
            return any(acq.iterdir())
        except OSError:
            return False

    return False


def vendor_stability_window(vendor: Vendor, default: int) -> int:
    if vendor is Vendor.BRUKER:
        return BRUKER_STABILITY_WINDOW_S
    return default
