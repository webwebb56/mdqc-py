"""Resolves standard MD QC paths.

Layout under %PROGRAMDATA%\\MassDynamics\\QC\\ on Windows. On non-Windows
(dev only), uses ~/.mdqc/ so the agent runs out-of-the-box for local testing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Root data directory.

    Windows: C:\\ProgramData\\MassDynamics\\QC
    Non-Windows (dev): ~/.mdqc
    Override with MDQC_DATA_DIR env var.
    """
    override = os.environ.get("MDQC_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "MassDynamics" / "QC"
    return Path.home() / ".mdqc"


def config_path() -> Path:
    """Path to config.toml. Override with MDQC_CONFIG env var."""
    override = os.environ.get("MDQC_CONFIG")
    if override:
        return Path(override)
    return data_dir() / "config.toml"


def log_dir() -> Path:
    return data_dir() / "logs"


def spool_dir() -> Path:
    return data_dir() / "spool"


def spool_pending() -> Path:
    return spool_dir() / "pending"


def spool_uploading() -> Path:
    return spool_dir() / "uploading"


def spool_completed() -> Path:
    return spool_dir() / "completed"


def spool_failed() -> Path:
    return spool_dir() / "failed"


def spool_work() -> Path:
    """Scratch dir for in-progress extraction outputs (CSVs etc.)."""
    return spool_dir() / "work"


def methods_dir() -> Path:
    """Where the bundled Skyline template lives."""
    return data_dir() / "methods"


def templates_dir() -> Path:
    """Alternative template location (customer-supplied)."""
    return data_dir() / "templates"


def crashes_dir() -> Path:
    return data_dir() / "crashes"


def certs_dir() -> Path:
    """Service-account-readable directory for exported PFX/PEM material (v1.1)."""
    return data_dir() / "certs"


def runtime_file() -> Path:
    """Where the service writes its IPC port + token."""
    from mdqc.config.defaults import RUNTIME_FILE_NAME

    return data_dir() / RUNTIME_FILE_NAME


def failed_files_path() -> Path:
    return data_dir() / "failed_files.json"


def activity_log_path() -> Path:
    return data_dir() / "activity_log.json"


def processed_registry_path() -> Path:
    return data_dir() / "processed_files.json"


def update_state_path() -> Path:
    return data_dir() / "update_state.json"


def ensure_dirs() -> None:
    """Create all required directories. Safe to call repeatedly."""
    for d in (
        data_dir(),
        log_dir(),
        spool_dir(),
        spool_pending(),
        spool_uploading(),
        spool_completed(),
        spool_failed(),
        spool_work(),
        methods_dir(),
        templates_dir(),
        crashes_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def bundled_assets_dir() -> Path:
    """Locate bundled assets (Skyline template, icon, report definition).

    When frozen by PyInstaller, assets live under sys._MEIPASS/assets.
    In dev, they live at ../../assets relative to this file.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent.parent.parent / "assets"


def ensure_bundled_assets() -> None:
    """Copy the bundled Skyline template + report into methods_dir() if not present.

    Mirrors the Rust agent's first-run installation behaviour. Overwrite if the
    bundled version is newer (compare mtime).
    """
    import shutil

    src = bundled_assets_dir()
    methods = methods_dir()
    methods.mkdir(parents=True, exist_ok=True)
    for name in ("QC_Method.sky", "MD_QC_Report.skyr"):
        bundled = src / name
        if not bundled.exists():
            continue
        target = methods / name
        if not target.exists() or bundled.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(bundled, target)
