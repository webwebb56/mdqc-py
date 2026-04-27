from __future__ import annotations

from pathlib import Path

import pytest

from mdqc.config import defaults
from mdqc.types import Vendor
from mdqc.watcher.vendor import (
    is_artifact_complete,
    try_exclusive_open,
    vendor_stability_window,
)


def _touch(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_thermo_requires_file(tmp_path: Path) -> None:
    raw = _touch(tmp_path / "run.raw")
    assert is_artifact_complete(raw, Vendor.THERMO)


def test_thermo_directory_rejected(tmp_path: Path) -> None:
    d = tmp_path / "dir.raw"
    d.mkdir()
    assert not is_artifact_complete(d, Vendor.THERMO)


def test_bruker_requires_analysis_tdf(tmp_path: Path) -> None:
    d = tmp_path / "run.d"
    d.mkdir()
    assert not is_artifact_complete(d, Vendor.BRUKER)
    _touch(d / "analysis.tdf")
    assert is_artifact_complete(d, Vendor.BRUKER)


def test_bruker_lock_file_blocks(tmp_path: Path) -> None:
    d = tmp_path / "run.d"
    d.mkdir()
    _touch(d / "analysis.tdf")
    _touch(d / "analysis.tdf-journal")
    assert not is_artifact_complete(d, Vendor.BRUKER)
    (d / "analysis.tdf-journal").unlink()
    _touch(d / "analysis.tdf-lock")
    assert not is_artifact_complete(d, Vendor.BRUKER)
    (d / "analysis.tdf-lock").unlink()
    assert is_artifact_complete(d, Vendor.BRUKER)


def test_waters_requires_func001(tmp_path: Path) -> None:
    d = tmp_path / "run.raw"
    d.mkdir()
    assert not is_artifact_complete(d, Vendor.WATERS)
    _touch(d / "_FUNC001.DAT")
    assert is_artifact_complete(d, Vendor.WATERS)


def test_sciex_requires_both_files(tmp_path: Path) -> None:
    wiff = _touch(tmp_path / "run.wiff")
    assert not is_artifact_complete(wiff, Vendor.SCIEX)
    _touch(tmp_path / "run.wiff.scan")
    assert is_artifact_complete(wiff, Vendor.SCIEX)


def test_agilent_requires_acqdata_with_files(tmp_path: Path) -> None:
    d = tmp_path / "run.d"
    d.mkdir()
    assert not is_artifact_complete(d, Vendor.AGILENT)
    acq = d / "AcqData"
    acq.mkdir()
    assert not is_artifact_complete(d, Vendor.AGILENT)
    _touch(acq / "MSScan.bin")
    assert is_artifact_complete(d, Vendor.AGILENT)


def test_vendor_stability_window_bruker_overrides() -> None:
    assert vendor_stability_window(Vendor.BRUKER, 60) == defaults.BRUKER_STABILITY_WINDOW_S
    assert defaults.BRUKER_STABILITY_WINDOW_S > 60


def test_vendor_stability_window_others_use_default() -> None:
    for vendor in (Vendor.THERMO, Vendor.WATERS, Vendor.SCIEX, Vendor.AGILENT):
        assert vendor_stability_window(vendor, 42) == 42


def test_try_exclusive_open_on_missing_returns_false(tmp_path: Path) -> None:
    assert not try_exclusive_open(tmp_path / "missing.bin")


def test_try_exclusive_open_on_directory_returns_false(tmp_path: Path) -> None:
    assert not try_exclusive_open(tmp_path)


def test_try_exclusive_open_on_real_file(tmp_path: Path) -> None:
    f = _touch(tmp_path / "data.bin")
    assert try_exclusive_open(f)


@pytest.mark.windows_only
def test_try_exclusive_open_blocks_when_held() -> None:
    pytest.skip("documented manually; FILE_SHARE_NONE behaviour exercised on Windows only")
