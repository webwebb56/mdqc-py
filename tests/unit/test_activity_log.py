from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mdqc.activity_log import ActivityEntry, ActivityLog
from mdqc.types import ExtractionStatus


def _entry(
    path: str = "/data/run.raw",
    *,
    minutes_ago: int = 0,
    result: ExtractionStatus = ExtractionStatus.SUCCESS,
    targets_found: int | None = 10,
    targets_expected: int | None = 10,
    extraction_time_ms: int | None = 1234,
    error: str | None = None,
) -> ActivityEntry:
    base = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    return ActivityEntry(
        path=path,
        instrument_id="INSTR1",
        timestamp=base - timedelta(minutes=minutes_ago),
        result=result,
        targets_found=targets_found,
        targets_expected=targets_expected,
        extraction_time_ms=extraction_time_ms,
        error=error,
    )


def _log_path(tmp_data_dir: Path) -> Path:
    return tmp_data_dir / "activity_log.json"


def test_record_and_recent_returns_newest_first(tmp_data_dir: Path) -> None:
    log = ActivityLog.load()
    for i in range(7):
        log.record(_entry(path=f"/data/run{i}.raw", minutes_ago=7 - i))

    recent = log.recent(5)
    assert len(recent) == 5
    assert [e.path for e in recent] == [
        "/data/run6.raw",
        "/data/run5.raw",
        "/data/run4.raw",
        "/data/run3.raw",
        "/data/run2.raw",
    ]


def test_trim_at_max_entries(tmp_data_dir: Path) -> None:
    log = ActivityLog(max_entries=3)
    for i in range(5):
        log.record(_entry(path=f"/data/run{i}.raw", minutes_ago=5 - i))

    assert len(log) == 3
    paths = [e.path for e in log.recent(10)]
    assert paths == ["/data/run4.raw", "/data/run3.raw", "/data/run2.raw"]


def test_persistence_round_trip(tmp_data_dir: Path) -> None:
    log = ActivityLog.load()
    log.record(
        _entry(
            path="/data/success.raw",
            result=ExtractionStatus.SUCCESS,
            targets_found=42,
            targets_expected=50,
            extraction_time_ms=9999,
        )
    )
    log.record(
        _entry(
            path="/data/fail.raw",
            result=ExtractionStatus.FAILED,
            targets_found=None,
            targets_expected=None,
            extraction_time_ms=None,
            error="skyline crashed",
        )
    )

    assert _log_path(tmp_data_dir).exists()

    reloaded = ActivityLog.load()
    recent = reloaded.recent(10)
    assert len(recent) == 2
    assert recent[0].path == "/data/fail.raw"
    assert recent[0].result is ExtractionStatus.FAILED
    assert recent[0].error == "skyline crashed"
    assert recent[1].path == "/data/success.raw"
    assert recent[1].targets_found == 42


def test_disk_failure_does_not_raise(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = ActivityLog.load()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with caplog.at_level("WARNING", logger="mdqc.activity_log"):
        log.record(_entry())

    assert len(log) == 1
    assert any("persistence" in rec.message for rec in caplog.records)


def test_load_missing_file_returns_empty(tmp_data_dir: Path) -> None:
    assert not _log_path(tmp_data_dir).exists()
    log = ActivityLog.load()
    assert len(log) == 0


def test_load_corrupt_json_returns_empty(
    tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = _log_path(tmp_data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not json at all", encoding="utf-8")

    with caplog.at_level("WARNING", logger="mdqc.activity_log"):
        log = ActivityLog.load()

    assert len(log) == 0
    assert any("load failed" in rec.message for rec in caplog.records)


def test_recent_zero_returns_empty_list(tmp_data_dir: Path) -> None:
    log = ActivityLog.load()
    log.record(_entry())
    assert log.recent(0) == []


def test_record_skipped_with_error_message(tmp_data_dir: Path) -> None:
    log = ActivityLog.load()
    log.record(
        _entry(
            path="/data/skipped.raw",
            result=ExtractionStatus.SKIPPED,
            targets_found=None,
            targets_expected=None,
            extraction_time_ms=None,
            error="not a QC run",
        )
    )

    serialized = json.loads(_log_path(tmp_data_dir).read_text())
    assert serialized[0]["result"] == "SKIPPED"
    assert serialized[0]["error"] == "not a QC run"
