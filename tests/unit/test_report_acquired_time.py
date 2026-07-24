"""Tests for real acquisition-timestamp extraction (v0.5.0, §3.3).

Skyline's AcquiredTime/ModifiedTime columns carry the real acquisition time
read from the raw-file header. mdqc pulls these into the payload instead of
using the file mtime, so runs order correctly and the date-range filter works.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mdqc.config.schema import SkylineConfig
from mdqc.extractor import Extractor
from mdqc.extractor.report import _parse_skyline_time, parse_skyline_run_metadata
from mdqc.extractor.skyline import SkylineRunResult
from mdqc.types import ExtractionStatus

FIXTURES = Path(__file__).parent.parent / "fixtures"
TRANSITION_REPORT = FIXTURES / "skyline_transition_report.csv"


# ─── Real fixture ────────────────────────────────────────────────────────────

def test_real_report_acquired_and_modified_time() -> None:
    acquired, modified = parse_skyline_run_metadata(TRANSITION_REPORT)
    # Wall clock is preserved; an offset is appended (varies by test machine).
    assert acquired is not None and acquired.startswith("2026-07-22T02:44:30")
    assert modified is not None and modified.startswith("2026-07-22T02:50:54")
    # Offset-aware.
    assert datetime.fromisoformat(acquired).tzinfo is not None


# ─── _parse_skyline_time ─────────────────────────────────────────────────────

def test_parse_us_12h() -> None:
    out = _parse_skyline_time("7/22/2026 2:44:30 AM")
    assert out is not None and out.startswith("2026-07-22T02:44:30")


def test_parse_us_12h_pm() -> None:
    out = _parse_skyline_time("7/22/2026 2:44:30 PM")
    assert out is not None and out.startswith("2026-07-22T14:44:30")


def test_parse_us_24h() -> None:
    out = _parse_skyline_time("7/22/2026 14:05:00")
    assert out is not None and out.startswith("2026-07-22T14:05:00")


def test_parse_eu_24h() -> None:
    # 22/07/2026 is unambiguously D/M (no 22nd month), so the EU format wins.
    out = _parse_skyline_time("22/07/2026 14:05:00")
    assert out is not None and out.startswith("2026-07-22T14:05:00")


def test_parse_iso() -> None:
    out = _parse_skyline_time("2026-07-22T02:44:30")
    assert out is not None and out.startswith("2026-07-22T02:44:30")
    assert datetime.fromisoformat(out).tzinfo is not None


def test_parse_empty_returns_none() -> None:
    assert _parse_skyline_time("") is None


def test_parse_na_token_returns_none() -> None:
    assert _parse_skyline_time("NaN") is None
    assert _parse_skyline_time("#N/A") is None


def test_parse_garbage_returns_none() -> None:
    assert _parse_skyline_time("not a date") is None


# ─── parse_skyline_run_metadata edge cases ───────────────────────────────────

def test_missing_columns_returns_none_pair(tmp_path: Path) -> None:
    csv = tmp_path / "no_time.csv"
    csv.write_text("Peptide,Total Area\nPEP,1000\n")
    assert parse_skyline_run_metadata(csv) == (None, None)


def test_only_acquired_column(tmp_path: Path) -> None:
    csv = tmp_path / "acq_only.csv"
    csv.write_text("Acquired Time,Peptide\n7/22/2026 2:44:30 AM,PEP\n")
    acquired, modified = parse_skyline_run_metadata(csv)
    assert acquired is not None and acquired.startswith("2026-07-22T02:44:30")
    assert modified is None


def test_empty_file_returns_none_pair(tmp_path: Path) -> None:
    csv = tmp_path / "empty.csv"
    csv.write_text("")
    assert parse_skyline_run_metadata(csv) == (None, None)


def test_header_only_returns_none_pair(tmp_path: Path) -> None:
    csv = tmp_path / "header_only.csv"
    csv.write_text("Acquired Time,Modified Time,Peptide\n")
    assert parse_skyline_run_metadata(csv) == (None, None)


# ─── extract() integration: acquired_time reaches the result ─────────────────

@pytest.mark.asyncio
async def test_extract_populates_acquired_time(tmp_path: Path) -> None:
    exe = tmp_path / "SkylineCmd.exe"
    exe.write_text("fake")
    template = tmp_path / "template.sky"
    template.write_text("t")
    raw = tmp_path / "sample.raw"
    raw.write_text("r")
    ex = Extractor(SkylineConfig(path=str(exe), timeout_seconds=30), work_dir=tmp_path / "work")

    async def side_effect(**kwargs):
        out = kwargs["output_csv"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "Acquired Time,Modified Time,Peptide,Total Area\n"
            "7/22/2026 2:44:30 AM,7/22/2026 2:50:54 AM,PEP,1000\n"
        )
        return SkylineRunResult(returncode=0, stdout="ok", stderr="", duration_ms=1, version="26")

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await ex.extract(template=template, raw_file=raw)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.acquired_time is not None
    assert result.acquired_time.startswith("2026-07-22T02:44:30")
    assert result.modified_time is not None
    assert result.modified_time.startswith("2026-07-22T02:50:54")
