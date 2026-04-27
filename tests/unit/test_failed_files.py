from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from freezegun import freeze_time

from mdqc.failed_files import FailedFileEntry, FailedFilesStore


def _store_path(tmp_data_dir: Path) -> Path:
    return tmp_data_dir / "failed_files.json"


def test_add_and_find_persists_across_load(tmp_data_dir: Path) -> None:
    store = FailedFilesStore.load()
    store.add("/data/run1.raw", "INSTR1", "timeout")

    entry = store.find("/data/run1.raw")
    assert entry is not None
    assert entry.path == "/data/run1.raw"
    assert entry.instrument_id == "INSTR1"
    assert entry.reason == "timeout"
    assert entry.retry_count == 0

    assert _store_path(tmp_data_dir).exists()

    reloaded = FailedFilesStore.load()
    found = reloaded.find("/data/run1.raw")
    assert found is not None
    assert found.path == "/data/run1.raw"
    assert found.instrument_id == "INSTR1"
    assert found.reason == "timeout"


def test_add_same_path_updates_in_place(tmp_data_dir: Path) -> None:
    # The Python port updates the existing entry: increments retry_count and
    # refreshes reason/failed_at/seq. Rust's HashMap-insert replaces and resets
    # retry_count to 0; this is the documented improvement.
    store = FailedFilesStore.load()
    store.add("/data/run1.raw", "INSTR1", "timeout")
    store.add("/data/run1.raw", "INSTR1", "skyline error")

    assert len(store) == 1
    entry = store.find("/data/run1.raw")
    assert entry is not None
    assert entry.retry_count == 1
    assert entry.reason == "skyline error"

    store.add("/data/run1.raw", "INSTR1", "another error")
    entry2 = store.find("/data/run1.raw")
    assert entry2 is not None
    assert entry2.retry_count == 2


def test_eviction_drops_oldest_when_over_max(tmp_data_dir: Path) -> None:
    store = FailedFilesStore(max_entries=3)
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(5):
        with freeze_time(base.replace(minute=i)):
            store.add(f"/data/run{i}.raw", "INSTR1", "boom")

    assert len(store) == 3
    paths = {e.path for e in store.all()}
    assert paths == {"/data/run2.raw", "/data/run3.raw", "/data/run4.raw"}


def test_eviction_tiebreaker_uses_seq(tmp_data_dir: Path) -> None:
    store = FailedFilesStore(max_entries=2)
    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    with freeze_time(frozen):
        store.add("/data/a.raw", "INSTR1", "boom")
        store.add("/data/b.raw", "INSTR1", "boom")
        store.add("/data/c.raw", "INSTR1", "boom")

    remaining = sorted(e.path for e in store.all())
    assert remaining == ["/data/b.raw", "/data/c.raw"]
    assert store.find("/data/a.raw") is None


def test_remove_returns_true_when_present(tmp_data_dir: Path) -> None:
    store = FailedFilesStore.load()
    store.add("/data/x.raw", "INSTR1", "boom")
    assert store.remove("/data/x.raw") is True
    assert store.find("/data/x.raw") is None
    assert store.remove("/data/missing.raw") is False


def test_increment_retry_bumps_counter(tmp_data_dir: Path) -> None:
    store = FailedFilesStore.load()
    store.add("/data/x.raw", "INSTR1", "boom")
    store.increment_retry("/data/x.raw")
    store.increment_retry("/data/x.raw")
    entry = store.find("/data/x.raw")
    assert entry is not None
    assert entry.retry_count == 2


def test_clear_empties_and_persists(tmp_data_dir: Path) -> None:
    store = FailedFilesStore.load()
    store.add("/data/x.raw", "INSTR1", "boom")
    store.add("/data/y.raw", "INSTR1", "boom")
    store.clear()
    assert len(store) == 0

    target = _store_path(tmp_data_dir)
    assert target.exists()
    assert json.loads(target.read_text()) == []


def test_disk_failure_does_not_raise(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FailedFilesStore.load()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with caplog.at_level("WARNING", logger="mdqc.failed_files"):
        store.add("/data/x.raw", "INSTR1", "extraction failed")

    assert store.find("/data/x.raw") is not None
    assert any("persistence" in rec.message for rec in caplog.records)


def test_load_missing_file_returns_empty(tmp_data_dir: Path) -> None:
    target = _store_path(tmp_data_dir)
    assert not target.exists()
    store = FailedFilesStore.load()
    assert len(store) == 0


def test_load_corrupt_json_returns_empty(
    tmp_data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = _store_path(tmp_data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="mdqc.failed_files"):
        store = FailedFilesStore.load()

    assert len(store) == 0
    assert any("load failed" in rec.message for rec in caplog.records)


def test_round_trip_preserves_seq_for_tiebreaker(tmp_data_dir: Path) -> None:
    frozen = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
    with freeze_time(frozen):
        store = FailedFilesStore.load()
        store.add("/data/a.raw", "INSTR1", "boom")
        store.add("/data/b.raw", "INSTR1", "boom")

    reloaded = FailedFilesStore.load(max_entries=1)
    reloaded._save_locked = lambda: None  # type: ignore[method-assign]
    reloaded._trim_locked()  # type: ignore[attr-defined]
    remaining = [e.path for e in reloaded.entries]
    assert remaining == ["/data/b.raw"]


def test_entry_serialization_round_trip() -> None:
    entry = FailedFileEntry(
        path="/data/run.raw",
        instrument_id="INSTR1",
        reason="timeout",
        failed_at=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        retry_count=3,
        seq=7,
    )
    restored = FailedFileEntry.from_dict(entry.to_dict())
    assert restored == entry
