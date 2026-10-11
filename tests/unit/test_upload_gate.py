"""The gold-standard upload gate.

A Sciex 7500 batch of 192 plasma runs uploaded overnight, scored against an
old baseline; a new one was saved the next morning from the batch's first SSC
runs, but the payloads were already on the platform and could not be replaced.
The gate holds runs until the gold standard they should be scored against
exists, then re-scores them before they leave.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mdqc import gold_standards as gs
from mdqc import upload_gate
from mdqc.config.schema import CloudConfig, Config
from mdqc.spool import Spool
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    ExtractionResult,
    RunClassification,
    RunMetrics,
    TargetMetric,
)

INSTRUMENT = "ASTRAL-1"
SPD = 200


def _cfg(*, gate: bool = True, token: str | None = "t") -> Config:
    return Config(cloud=CloudConfig(api_token=token, hold_until_gold_standard=gate))


def _cls(ct: ControlType, spd: int = SPD) -> RunClassification:
    return RunClassification(
        control_type=ct, well_position=None, instrument_id=INSTRUMENT, plate_id=None,
        confidence=Confidence.HIGH, source=ClassificationSource.FILENAME, spd=spd,
    )


def _ext(area: float) -> ExtractionResult:
    metrics = [
        TargetMetric(target_id=s, peptide_sequence=s, protein_name="Non_reactive_Targets",
                     peak_area=area, retention_time=rt)
        for s, rt in (("PEPA", 2.0), ("PEPB", 3.0), ("PEPC", 4.0))
    ]
    return ExtractionResult(
        run_id=uuid4(), raw_file_path=Path("x.raw"), target_metrics=metrics,
        run_metrics=RunMetrics(targets_found=3, targets_expected=3, target_recovery_pct=100.0),
        acquired_time="2026-10-01T10:00:00+02:00", acquired_time_source="instrument",
    )


def _spool_run(cfg: Config, spool: Spool, ct: ControlType, area: float) -> tuple[Path, str]:
    """What lifecycle does for one extracted run."""
    cls, ext = _cls(ct), _ext(area)
    ctx, cmp_ = gs.build_payload_comparison(cls, ext, cfg.thresholds_for(cls.spd))
    held = upload_gate.should_hold(cfg, cls, str(ext.run_id))
    path = spool.enqueue(cls, ext, baseline_context=ctx, comparison_metrics=cmp_, held=held)
    gs.record_ssc0_run(cls, ext)
    return path, str(ext.run_id)


def _save_baseline_from(run_ids: list[str], label: str = "gold") -> None:
    gs.save_baseline(INSTRUMENT, SPD, run_ids, label, None)


# ── holding ──────────────────────────────────────────────────────────────────


def test_runs_are_held_until_a_gold_standard_exists(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    path, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert path.parent == spool.held_dir
    assert spool.claim_next() is None  # the uploader never sees a held run
    assert upload_gate.hold_reason(INSTRUMENT, SPD) == upload_gate.REASON_NO_BASELINE


def test_runs_flow_once_a_baseline_exists(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    _, gold = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    _save_baseline_from([gold])
    upload_gate.release(cfg, spool, INSTRUMENT, SPD)
    path, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert path.parent == spool.pending_dir


def test_gold_standard_runs_hold_the_bucket_again(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    _, first = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    _save_baseline_from([first], "install")
    upload_gate.release(cfg, spool, INSTRUMENT, SPD)

    gold_path, gold_id = _spool_run(cfg, spool, ControlType.SSC0, 2000.0)
    after_path, _ = _spool_run(cfg, spool, ControlType.SSC, 2000.0)

    assert gold_path.parent == spool.held_dir
    assert after_path.parent == spool.held_dir
    assert upload_gate.hold_reason(INSTRUMENT, SPD) == upload_gate.REASON_NEW_GOLD
    assert upload_gate.gold_run_ids(INSTRUMENT, SPD) == [gold_id]


def test_other_spds_are_not_held_by_this_ones_gold_runs(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    for spd in (200, 500):
        cls, ext = _cls(ControlType.SSC, spd), _ext(1000.0)
        spool.enqueue(cls, ext)
        gs.record_ssc0_run(cls, ext)
        gs.save_baseline(INSTRUMENT, spd, [str(ext.run_id)], "b", None)
    _spool_run(cfg, spool, ControlType.SSC0, 1000.0)  # 200 SPD
    assert upload_gate.hold_reason(INSTRUMENT, 200) == upload_gate.REASON_NEW_GOLD
    assert upload_gate.hold_reason(INSTRUMENT, 500) is None


def test_a_manual_pause_holds_until_released(tmp_data_dir: Path) -> None:
    """For gold-standard runs that arrived without their marker."""
    cfg, spool = _cfg(), Spool("a", "t")
    _, first = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    _save_baseline_from([first])
    upload_gate.release(cfg, spool, INSTRUMENT, SPD)
    upload_gate.pause(INSTRUMENT, SPD)
    path, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert path.parent == spool.held_dir
    assert upload_gate.hold_reason(INSTRUMENT, SPD) == upload_gate.REASON_MANUAL


# ── releasing ────────────────────────────────────────────────────────────────


def test_release_rescores_held_runs_against_the_new_baseline(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    _, old_gold = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    _save_baseline_from([old_gold], "old")
    upload_gate.release(cfg, spool, INSTRUMENT, SPD)

    # The instrument changes: new gold runs at twice the old area, then a run.
    _, new_gold = _spool_run(cfg, spool, ControlType.SSC0, 2000.0)
    run_path, _ = _spool_run(cfg, spool, ControlType.SSC, 2000.0)
    import json
    before = json.loads(run_path.read_text(encoding="utf-8"))
    assert before["comparison_metrics"]["peak_area_verdict"] == "fail"  # vs the old one

    _save_baseline_from([new_gold], "new")
    released = upload_gate.release(cfg, spool, INSTRUMENT, SPD, rescore=True)

    assert released == 2
    assert list(spool.held_dir.glob("*.json")) == []
    out = json.loads((spool.pending_dir / run_path.name).read_text(encoding="utf-8"))
    assert out["baseline_context"]["label"] == "new"
    assert out["comparison_metrics"]["peak_area_verdict"] == "ok"
    assert upload_gate.hold_reason(INSTRUMENT, SPD) is None


def test_release_as_is_stops_holding_for_lack_of_a_baseline(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    path, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert upload_gate.release(cfg, spool, INSTRUMENT, SPD, rescore=False) == 1
    assert (spool.pending_dir / path.name).exists()
    # Until a baseline is saved, later runs upload without one.
    later, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert later.parent == spool.pending_dir


def test_release_only_moves_its_own_bucket(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    mine, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    other_cls, other_ext = _cls(ControlType.SSC, 500), _ext(1000.0)
    other = spool.enqueue(other_cls, other_ext, held=True)
    upload_gate.release(cfg, spool, INSTRUMENT, SPD, rescore=False)
    assert (spool.pending_dir / mine.name).exists()
    assert other.exists()


def test_held_summary_explains_each_bucket(tmp_data_dir: Path) -> None:
    cfg, spool = _cfg(), Spool("a", "t")
    _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    _spool_run(cfg, spool, ControlType.PCD, 5000.0)
    [row] = upload_gate.held_summary(spool)
    assert (row["instrument_id"], row["spd"], row["count"]) == (INSTRUMENT, SPD, 2)
    assert row["reason_text"] == "no gold standard has been saved yet"


# ── when the gate does not apply ─────────────────────────────────────────────


@pytest.mark.parametrize("cfg", [_cfg(token=None), _cfg(gate=False)], ids=["local-only", "disabled"])
def test_gate_is_off_when_nothing_uploads_or_it_is_disabled(
    tmp_data_dir: Path, cfg: Config
) -> None:
    spool = Spool("a", "t")
    path, _ = _spool_run(cfg, spool, ControlType.SSC, 1000.0)
    assert path.parent == spool.pending_dir


def test_settings_cloud_save_keeps_the_gate_setting() -> None:
    """The Cloud form rebuilds CloudConfig; it must not reset this flag."""
    import inspect

    from mdqc.webui import settings

    assert "hold_until_gold_standard=prev.cloud.hold_until_gold_standard" in inspect.getsource(
        settings
    )
