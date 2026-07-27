from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from freezegun import freeze_time

from mdqc.spool import Spool, SpoolFull, prune_spool, recover_orphans
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    ExtractionResult,
    ExtractionStatus,
    RunClassification,
    RunMetrics,
    TargetMetric,
    WellPosition,
)

CORRELATION_ID_RE = re.compile(r"^[\w-]+-\d{14}-[0-9a-f]{8}$")


def _classification(instrument_id: str = "INSTR1") -> RunClassification:
    return RunClassification(
        control_type=ControlType.QC_A,
        well_position=WellPosition("A", 1),
        instrument_id=instrument_id,
        plate_id="P01",
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
    )


def _extraction(*, raw_path: str = "/data/run.raw") -> ExtractionResult:
    return ExtractionResult(
        run_id=uuid4(),
        raw_file_path=Path(raw_path),
        raw_file_hash="deadbeef",
        template_path=Path("/methods/QC_Method.sky"),
        template_hash="cafef00d",
        backend="skyline",
        backend_version="22.2.0",
        extraction_time_ms=12345,
        status=ExtractionStatus.SUCCESS,
        target_metrics=[
            TargetMetric(
                target_id="PEPTIDE_1",
                peptide_sequence="ABCDEFK",
                precursor_mz=500.25,
                retention_time=10.1,
                peak_area=1234.5,
            ),
        ],
        run_metrics=RunMetrics(
            targets_found=1,
            targets_expected=1,
            target_recovery_pct=100.0,
        ),
    )


def _make_spool(root: Path) -> Spool:
    return Spool(agent_id="agent-test", agent_version="0.1.0", root=root)


def test_round_trip_enqueue_claim_complete(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    classification = _classification()
    extraction = _extraction()

    pending_path = spool.enqueue(classification, extraction)
    assert pending_path.exists()
    assert pending_path.parent == spool.pending_dir
    assert spool.pending_count() == 1

    claimed = spool.claim_next()
    assert claimed is not None
    uploading_path, payload = claimed
    assert uploading_path.parent == spool.uploading_dir
    assert not pending_path.exists()
    assert payload["agent_id"] == "agent-test"
    assert payload["agent_version"] == "0.1.0"
    assert payload["run"]["run_id"] == str(extraction.run_id)
    assert payload["run"]["raw_file_hash"] == "deadbeef"
    assert payload["run"]["control_type"] == "QC_A"
    assert payload["extraction"]["template_hash"] == "cafef00d"
    assert payload["target_metrics"][0]["target_id"] == "PEPTIDE_1"
    assert payload["run_metrics"]["targets_found"] == 1

    spool.mark_completed(uploading_path)
    assert not uploading_path.exists()
    assert (spool.completed_dir / uploading_path.name).exists()


def test_atomic_write_canary(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    extraction = _extraction()
    classification = _classification()
    final_name = f"{extraction.run_id}_payload.json"

    real_replace = os.replace

    def fail_replace(src: str, dst: str) -> None:
        if str(dst).endswith(final_name):
            raise OSError("simulated mid-replace crash")
        real_replace(src, dst)

    with (
        patch("mdqc.spool.store.os.replace", side_effect=fail_replace),
        pytest.raises(OSError, match="simulated"),
    ):
        spool.enqueue(classification, extraction)

    final_path = spool.pending_dir / final_name
    tmp_path = spool.pending_dir / f".{final_name}.tmp"
    assert not final_path.exists(), "no half-written final file should be visible"
    assert tmp_path.exists(), "tmp file should remain after a failed replace"


def test_crash_recovery_uploading_to_pending(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    fake = spool.uploading_dir / "abc_payload.json"
    fake.write_text("{}")

    moved = spool.recover_uploading_to_pending()
    assert moved == 1
    assert not fake.exists()
    assert (spool.pending_dir / "abc_payload.json").exists()


def test_size_cap_raises_spool_full(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    classification = _classification()

    big_payload = b"x" * (256 * 1024)
    for i in range(5):
        p = spool.pending_dir / f"prefill_{i}.json"
        p.write_bytes(big_payload)

    enqueued = 0
    with pytest.raises(SpoolFull):
        for _ in range(20):
            spool.enqueue(classification, _extraction(), max_pending_mb=1)
            enqueued += 1

    assert enqueued < 20


def test_pruning_removes_aged_pending(tmp_data_dir: Path) -> None:
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    pending = spool_root / "pending"
    failed = spool_root / "failed"

    old_pending = pending / "old_payload.json"
    old_pending.write_text("{}")
    fresh_pending = pending / "fresh_payload.json"
    fresh_pending.write_text("{}")
    old_failed = failed / "old_failed_payload.json"
    old_failed.write_text("{}")

    way_old = time.time() - (40 * 86400)
    os.utime(old_pending, (way_old, way_old))
    os.utime(old_failed, (way_old, way_old))

    counts = prune_spool(spool_root, max_age_days=30, completed_retention=10)
    assert counts["pending_aged_out"] == 1
    assert counts["failed_aged_out"] == 1
    assert not old_pending.exists()
    assert fresh_pending.exists()
    assert not old_failed.exists()


def test_completed_retention_keeps_n_most_recent(tmp_data_dir: Path) -> None:
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    completed = spool_root / "completed"

    base = time.time() - 1000
    paths = []
    for i in range(15):
        p = completed / f"completed_{i:02d}.json"
        p.write_text("{}")
        ts = base + i
        os.utime(p, (ts, ts))
        paths.append((ts, p))

    counts = prune_spool(spool_root, completed_retention=10)
    assert counts["completed_pruned"] == 5

    remaining = sorted(completed.iterdir())
    assert len(remaining) == 10
    paths.sort(key=lambda t: t[0], reverse=True)
    expected_kept = {p.name for _, p in paths[:10]}
    actual = {p.name for p in remaining}
    assert actual == expected_kept


def test_local_only_does_not_count_prune_completed(tmp_data_dir: Path) -> None:
    """Regression: Evosep timsTOF HT, 2026-07-24.

    In local-only mode the uploader moves pending -> completed without an
    upload, so completed/ holds the ONLY copy of each payload. Count-based
    pruning destroyed 166 of 176 overnight stress-test payloads. Recent
    payloads must survive regardless of how low completed_retention is.
    """
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    completed = spool_root / "completed"

    for i in range(176):
        (completed / f"completed_{i:03d}.json").write_text("{}")

    counts = prune_spool(spool_root, completed_retention=10, local_only=True)

    assert counts["completed_pruned"] == 0
    assert len(list(completed.iterdir())) == 176


def test_local_only_still_ages_out_completed(tmp_data_dir: Path) -> None:
    """Local-only completed/ is bounded by age, not left to grow forever."""
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    completed = spool_root / "completed"

    old = completed / "old_payload.json"
    old.write_text("{}")
    fresh = completed / "fresh_payload.json"
    fresh.write_text("{}")
    way_old = time.time() - (40 * 86400)
    os.utime(old, (way_old, way_old))

    counts = prune_spool(spool_root, max_age_days=30, local_only=True)

    assert counts["completed_pruned"] == 1
    assert not old.exists()
    assert fresh.exists()


def test_cloud_mode_still_count_prunes_completed(tmp_data_dir: Path) -> None:
    """Cloud mode is unchanged — completed/ is a receipt trail, capped by count."""
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    completed = spool_root / "completed"

    base = time.time() - 1000
    for i in range(15):
        p = completed / f"completed_{i:02d}.json"
        p.write_text("{}")
        os.utime(p, (base + i, base + i))

    counts = prune_spool(spool_root, completed_retention=10, local_only=False)

    assert counts["completed_pruned"] == 5
    assert len(list(completed.iterdir())) == 10


def test_default_completed_retention_survives_an_overnight_batch() -> None:
    """The old default of 10 could not hold one night of Evosep acquisition."""
    from mdqc.config.defaults import COMPLETED_RETENTION_COUNT

    assert COMPLETED_RETENTION_COUNT >= 176


def test_orphan_tmp_cleanup(tmp_data_dir: Path) -> None:
    spool_root = tmp_data_dir / "spool"
    _make_spool(spool_root)
    pending = spool_root / "pending"

    orphan = pending / ".abc_payload.json.tmp"
    orphan.write_bytes(b"partial")
    keeper = pending / "real_payload.json"
    keeper.write_text("{}")

    removed = recover_orphans(spool_root)
    assert removed == 1
    assert not orphan.exists()
    assert keeper.exists()


def test_correlation_id_format(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    cid = spool.correlation_id_for(uuid4())
    assert CORRELATION_ID_RE.match(cid), f"correlation_id {cid!r} does not match regex"


def test_correlation_id_uses_utc(tmp_data_dir: Path) -> None:
    """Bug-fix vs Rust: the correlation ID must be in UTC, not local time.

    See docs/AGENT_NOTES § Things that ARE bugs.
    """
    frozen = datetime(2026, 7, 4, 13, 30, 45, tzinfo=UTC)
    spool = _make_spool(tmp_data_dir / "spool")
    with freeze_time(frozen):
        cid = spool.correlation_id_for(uuid4())

    parts = cid.rsplit("-", 2)
    assert len(parts) == 3
    timestamp_str = parts[1]
    assert timestamp_str == "20260704133045", (
        f"correlation_id timestamp {timestamp_str} should be UTC strftime of frozen time"
    )


def test_mark_failed_writes_sidecar(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    extraction = _extraction()
    pending_path = spool.enqueue(_classification(), extraction)
    claimed = spool.claim_next()
    assert claimed is not None
    uploading_path, _ = claimed

    spool.mark_failed(uploading_path, reason="HTTP 401 unauthorised")
    assert not uploading_path.exists()
    failed_payload = spool.failed_dir / uploading_path.name
    assert failed_payload.exists()
    sidecar = spool.failed_dir / (uploading_path.stem + "_failure.json")
    assert sidecar.exists()
    sidecar_data = json.loads(sidecar.read_text())
    assert sidecar_data["reason"] == "HTTP 401 unauthorised"
    assert sidecar_data["payload_filename"] == uploading_path.name
    assert pending_path  # silence unused


def test_write_manifest_best_effort(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    spool.write_manifest(
        template_name="QC_Method.sky",
        instrument_id="INSTR1",
        target_ids=["PEP1", "PEP2"],
        known_metrics=["peak_area", "retention_time"],
        extra_metrics=["custom_x"],
    )
    manifest_path = spool.root / "manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["template_name"] == "QC_Method.sky"
    assert data["target_ids"] == ["PEP1", "PEP2"]
    assert data["extra_metrics"] == ["custom_x"]


def test_write_manifest_failure_does_not_raise(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    with patch("mdqc.spool.store.os.replace", side_effect=OSError("disk full")):
        spool.write_manifest(
            template_name="t",
            instrument_id="i",
            target_ids=[],
            known_metrics=[],
            extra_metrics=[],
        )


def test_claim_next_returns_none_when_empty(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    assert spool.claim_next() is None


def test_claim_next_picks_oldest(tmp_data_dir: Path) -> None:
    spool = _make_spool(tmp_data_dir / "spool")
    classification = _classification()
    e1 = _extraction(raw_path="/data/run1.raw")
    e2 = _extraction(raw_path="/data/run2.raw")

    p1 = spool.enqueue(classification, e1)
    p2 = spool.enqueue(classification, e2)

    older = time.time() - 3600
    os.utime(p1, (older, older))

    claimed = spool.claim_next()
    assert claimed is not None
    uploading_path, payload = claimed
    assert payload["run"]["run_id"] == str(e1.run_id)
    assert uploading_path.name == p1.name
    assert p2.exists()
