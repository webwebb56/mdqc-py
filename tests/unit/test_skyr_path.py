"""Tests for configurable .skyr report path (v0.5.0).

`[skyline].report_skyr_path`:
  - "auto"   -> bundled methods_dir()/MD_QC_Report.skyr if present, else None
  - explicit -> used verbatim; a missing explicit path fails the extraction
                with a clear message (no silent fallback).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mdqc.config.paths import methods_dir
from mdqc.config.schema import SkylineConfig
from mdqc.extractor import Extractor
from mdqc.extractor.skyline import SkylineRunResult
from mdqc.types import ExtractionStatus


def _extractor(tmp_path: Path, **skyline_kw) -> Extractor:
    exe = tmp_path / "SkylineCmd.exe"
    exe.write_text("fake")
    cfg = SkylineConfig(path=str(exe), timeout_seconds=30, **skyline_kw)
    return Extractor(cfg, work_dir=tmp_path / "work")


# ─── _resolve_skyr_path ──────────────────────────────────────────────────────

def test_auto_with_no_bundled_report_returns_none(tmp_path: Path) -> None:
    ex = _extractor(tmp_path, report_skyr_path="auto")
    path, error = ex._resolve_skyr_path()
    assert path is None
    assert error is None


def test_auto_uses_bundled_report_when_present(tmp_path: Path) -> None:
    bundled = methods_dir() / "MD_QC_Report.skyr"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_text("<views/>")
    try:
        ex = _extractor(tmp_path, report_skyr_path="auto")
        path, error = ex._resolve_skyr_path()
        assert path == bundled
        assert error is None
    finally:
        bundled.unlink()


def test_explicit_existing_path_is_used(tmp_path: Path) -> None:
    custom = tmp_path / "my_custom_report.skyr"
    custom.write_text("<views/>")
    ex = _extractor(tmp_path, report_skyr_path=str(custom))
    path, error = ex._resolve_skyr_path()
    assert path == custom
    assert error is None


def test_explicit_missing_path_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.skyr"
    ex = _extractor(tmp_path, report_skyr_path=str(missing))
    path, error = ex._resolve_skyr_path()
    assert path is None
    assert error is not None
    assert str(missing) in error


# ─── extract() integration: missing explicit report fails the run ────────────

@pytest.mark.asyncio
async def test_extract_fails_cleanly_on_missing_explicit_skyr(tmp_path: Path) -> None:
    template = tmp_path / "template.sky"
    template.write_text("t")
    raw = tmp_path / "sample.raw"
    raw.write_text("r")
    missing = tmp_path / "nope.skyr"

    ex = _extractor(tmp_path, report_skyr_path=str(missing))

    # run_skyline must never be called — we fail before invoking Skyline.
    with patch("mdqc.extractor.run_skyline", new=AsyncMock()) as mocked:
        result = await ex.extract(template=template, raw_file=raw)

    assert result.status == ExtractionStatus.FAILED
    assert "report_skyr_path" in result.error_message
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_extract_passes_explicit_skyr_to_skyline(tmp_path: Path) -> None:
    template = tmp_path / "template.sky"
    template.write_text("t")
    raw = tmp_path / "sample.raw"
    raw.write_text("r")
    custom = tmp_path / "custom.skyr"
    custom.write_text("<views/>")

    ex = _extractor(tmp_path, report_skyr_path=str(custom))

    async def side_effect(**kwargs):
        kwargs["output_csv"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_csv"].write_text("Peptide,Total Area\nPEP,1000\n")
        return SkylineRunResult(returncode=0, stdout="ok", stderr="", duration_ms=1, version="26")

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)) as mocked:
        result = await ex.extract(template=template, raw_file=raw)

    assert result.status == ExtractionStatus.SUCCESS
    # the resolved custom .skyr was handed to Skyline
    assert mocked.call_args.kwargs["report_skyr"] == custom
