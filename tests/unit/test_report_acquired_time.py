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
from mdqc.extractor import Extractor, report
from mdqc.extractor.report import (
    _parse_skyline_time,
    infer_day_first,
    parse_skyline_run_metadata,
)
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


# ─── Date order comes from the file, not the host ────────────────────────────
#
# Skyline renders the timestamp in the exporting PC's locale. Deciding the
# order from the machine mdqc happens to run on misreads every date whose day
# is 12 or less while getting days 13+ right — wrong in a way that looks fine.


def test_infer_day_first_from_an_unambiguous_day() -> None:
    assert infer_day_first(["22/07/2026 14:05:00"]) is True


def test_infer_month_first_from_an_unambiguous_month() -> None:
    assert infer_day_first(["7/22/2026 5:09:14 AM"]) is False


def test_infer_settles_the_whole_file_from_one_value() -> None:
    # The ambiguous value alone proves nothing; its neighbour decides.
    assert infer_day_first(["9/1/2026 3:58:34 AM", "7/22/2026 5:09:14 AM"]) is False


def test_infer_undecided_when_every_value_is_ambiguous() -> None:
    assert infer_day_first(["9/1/2026 3:58:34 AM", "2/3/2026 1:00:00 AM"]) is None


def test_infer_undecided_when_the_file_contradicts_itself() -> None:
    assert infer_day_first(["22/07/2026 14:05:00", "7/22/2026 5:09:14 AM"]) is None


def test_infer_ignores_non_dates() -> None:
    assert infer_day_first(["", "NaN", "not a date"]) is None


def test_month_first_file_parses_on_a_day_first_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a site's export read 2026-01-09 instead of 2026-09-01.

    The run is named for the day it started, 31 Aug, so 9/1/2026 can only be
    1 September. The modified column carries the value that proves the order.
    """
    monkeypatch.setattr(report, "locale_prefers_day_first", lambda: True)
    csv = tmp_path / "month_first.csv"
    csv.write_text(
        "Acquired Time,Modified Time,Replicate Name\n"
        "9/1/2026 3:58:34 AM,7/22/2026 5:09:14 AM,20260831_QCB_4\n"
    )
    acquired, modified = parse_skyline_run_metadata(csv)
    assert acquired is not None and acquired.startswith("2026-09-01T03:58:34")
    assert modified is not None and modified.startswith("2026-07-22T05:09:14")


def test_day_first_file_parses_on_a_month_first_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report, "locale_prefers_day_first", lambda: False)
    csv = tmp_path / "day_first.csv"
    csv.write_text(
        "Acquired Time,Modified Time,Peptide\n"
        "3/02/2026 12:40:31 AM,22/07/2026 14:05:00,PEP\n"
    )
    acquired, modified = parse_skyline_run_metadata(csv)
    assert acquired is not None and acquired.startswith("2026-02-03T00:40:31")
    assert modified is not None and modified.startswith("2026-07-22T14:05:00")


@pytest.mark.parametrize(
    ("host_day_first", "expected"),
    [(True, "2026-01-09T03:58:34"), (False, "2026-09-01T03:58:34")],
)
def test_ambiguous_file_falls_back_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_day_first: bool, expected: str
) -> None:
    """With nothing in the file to go on, the machine's own setting is all there is."""
    monkeypatch.setattr(report, "locale_prefers_day_first", lambda: host_day_first)
    csv = tmp_path / "ambiguous.csv"
    csv.write_text("Acquired Time,Peptide\n9/1/2026 3:58:34 AM,PEP\n")
    acquired, _ = parse_skyline_run_metadata(csv)
    assert acquired is not None and acquired.startswith(expected)


def test_order_is_inferred_from_later_rows_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-replicate export settles the order even when row 1 is ambiguous."""
    monkeypatch.setattr(report, "locale_prefers_day_first", lambda: True)
    csv = tmp_path / "multi.csv"
    csv.write_text(
        "Acquired Time,Replicate Name\n"
        "9/1/2026 3:58:34 AM,run_a\n"
        "9/1/2026 3:58:34 AM,run_a\n"
        "7/22/2026 5:09:14 AM,run_b\n"
    )
    acquired, _ = parse_skyline_run_metadata(csv)
    assert acquired is not None and acquired.startswith("2026-09-01T03:58:34")


# ─── A .skyr path that doesn't resolve ────────────────────────────────────────
#
# Skyline doesn't reject a report whose column path is wrong; it writes this
# marker into every row. A site's log showed it read as a bad date, followed by
# a quiet fall back to the file mtime — and the site believed it was working.

_NOT_FOUND = "#COLUMN Results!*.Value.PeptideResult.ResultFile.AcquiredTime NOT FOUND#"


def test_column_not_found_is_reported_as_a_report_problem(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    csv = tmp_path / "not_found.csv"
    csv.write_text(f"Peptide,Acquired Time\nPEP_A,{_NOT_FOUND}\nPEP_B,{_NOT_FOUND}\n")
    with caplog.at_level("INFO"):
        acquired, _ = parse_skyline_run_metadata(csv)
    assert acquired is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert message.startswith("skyr_column_not_found")
    assert "Results!*.Value.PeptideResult.ResultFile.AcquiredTime" in message
    assert "row source" in message
    # Not the old, misleading reading of it as an unparseable date.
    assert not [r for r in caplog.records if "could not parse" in r.getMessage()]


@pytest.mark.asyncio
async def test_extraction_marks_a_not_found_column_as_a_file_mtime_fallback(
    tmp_path: Path,
) -> None:
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
        out.write_text(f"Acquired Time,Peptide,Total Area\n{_NOT_FOUND},PEP,1000\n")
        return SkylineRunResult(returncode=0, stdout="ok", stderr="", duration_ms=1, version="26")

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await ex.extract(template=template, raw_file=raw)

    assert result.status == ExtractionStatus.SUCCESS
    assert result.acquired_time is not None          # the mtime, not nothing
    assert result.acquired_time_source == "file_mtime"


@pytest.mark.asyncio
async def test_extraction_marks_a_reported_time_as_instrument(tmp_path: Path) -> None:
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
        out.write_text("Acquired Time,Peptide,Total Area\n7/22/2026 2:44:30 AM,PEP,1000\n")
        return SkylineRunResult(returncode=0, stdout="ok", stderr="", duration_ms=1, version="26")

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await ex.extract(template=template, raw_file=raw)

    assert result.acquired_time_source == "instrument"
