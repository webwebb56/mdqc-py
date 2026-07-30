"""Tests for local gold-standard (SSC0) baseline storage and computation."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from mdqc import gold_standards as gs
from mdqc.config.schema import PeptideClassRule
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    ExtractionResult,
    RunClassification,
    RunMetrics,
    TargetMetric,
)


def _classification(
    control_type: ControlType = ControlType.SSC0,
    instrument_id: str | None = "Astral_0001",
    spd: int | None = 200,
) -> RunClassification:
    return RunClassification(
        control_type=control_type,
        well_position=None,
        instrument_id=instrument_id,
        plate_id=None,
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
        spd=spd,
    )


def _extraction(metrics: list[TargetMetric], targets_found: int | None = None) -> ExtractionResult:
    run_metrics = (
        RunMetrics(
            targets_found=targets_found if targets_found is not None else len(metrics),
            targets_expected=len(metrics),
            target_recovery_pct=100.0,
        )
        if metrics or targets_found is not None
        else None
    )
    return ExtractionResult(run_id=uuid4(), target_metrics=metrics, run_metrics=run_metrics)


def _pep(
    seq: str, protein: str = "Non_reactive_Targets", area: float = 1000.0, rt: float = 5.0
) -> TargetMetric:
    return TargetMetric(
        target_id=seq, peptide_sequence=seq, protein_name=protein, peak_area=area, retention_time=rt
    )


# ─── record_ssc0_run / list_ssc0_runs ──────────────────────────────────────


def test_record_ssc0_run_appends_snapshot(tmp_data_dir: Path) -> None:
    metrics = [_pep("PEPA", area=1000.0, rt=5.0), _pep("PEPB", area=2000.0, rt=8.0)]
    extraction = _extraction(metrics)
    gs.record_ssc0_run(_classification(), extraction)

    runs = gs.list_ssc0_runs("Astral_0001", 200)
    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == str(extraction.run_id)
    assert run["instrument_id"] == "Astral_0001"
    assert run["spd"] == 200
    assert run["targets_found"] == 2
    key = "Non_reactive_Targets|PEPA"
    assert run["peptides"][key]["peak_area"] == 1000.0
    assert run["peptides"][key]["retention_time"] == 5.0


def test_record_ssc0_run_ignores_non_ssc0(tmp_data_dir: Path) -> None:
    gs.record_ssc0_run(_classification(control_type=ControlType.QC_A), _extraction([_pep("A")]))
    assert gs.list_ssc0_runs("Astral_0001", 200) == []


def test_list_ssc0_runs_scoped_by_instrument_and_spd(tmp_data_dir: Path) -> None:
    gs.record_ssc0_run(
        _classification(instrument_id="Astral_0001", spd=200), _extraction([_pep("A")])
    )
    gs.record_ssc0_run(
        _classification(instrument_id="Astral_0001", spd=500), _extraction([_pep("A")])
    )
    gs.record_ssc0_run(
        _classification(instrument_id="Exploris01", spd=200), _extraction([_pep("A")])
    )
    assert len(gs.list_ssc0_runs("Astral_0001", 200)) == 1
    assert len(gs.list_ssc0_runs("Astral_0001", 500)) == 1
    assert len(gs.list_ssc0_runs("Exploris01", 200)) == 1
    assert gs.list_ssc0_runs("Astral_0001", 300) == []


def test_list_available_spds(tmp_data_dir: Path) -> None:
    gs.record_ssc0_run(
        _classification(instrument_id="Astral_0001", spd=500), _extraction([_pep("A")])
    )
    gs.record_ssc0_run(
        _classification(instrument_id="Astral_0001", spd=200), _extraction([_pep("A")])
    )
    assert gs.list_available_spds("Astral_0001") == [200, 500]
    assert gs.list_available_spds("Unknown_Instrument") == []


def test_record_ssc0_run_handles_unknown_instrument_and_spd(tmp_data_dir: Path) -> None:
    gs.record_ssc0_run(
        _classification(instrument_id=None, spd=None), _extraction([_pep("A")])
    )
    assert len(gs.list_ssc0_runs(None, None)) == 1


def test_record_ssc0_run_caps_bucket_size(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gs, "_MAX_RUNS_PER_BUCKET", 3)
    run_ids = []
    for _ in range(4):
        extraction = _extraction([_pep("A")])
        run_ids.append(str(extraction.run_id))
        gs.record_ssc0_run(_classification(), extraction)
    runs = gs.list_ssc0_runs("Astral_0001", 200)
    assert len(runs) == 3
    # Oldest (first recorded) was evicted; newest 3 remain, in order.
    assert [r["run_id"] for r in runs] == run_ids[1:]


# ─── compute_baseline_preview ──────────────────────────────────────────────


def _run_dict(run_id: str, area: float, rt: float, peptide_class: str | None = None) -> dict:
    return {
        "run_id": run_id,
        "peptides": {
            "Prot|PEP": {
                "protein_name": "Prot",
                "peptide_sequence": "PEP",
                "peptide_class": peptide_class,
                "peptide_class_purpose": None,
                "retention_time": rt,
                "peak_area": area,
            }
        },
    }


def test_compute_baseline_preview_median_and_cv() -> None:
    runs = [
        _run_dict("r1", 100.0, 5.0),
        _run_dict("r2", 110.0, 5.1),
        _run_dict("r3", 120.0, 4.9),
    ]
    stats = gs.compute_baseline_preview(runs, {"r1", "r2", "r3"})
    stat = stats["Prot|PEP"]
    # mean=110, pstdev=sqrt(((100-110)**2+(110-110)**2+(120-110)**2)/3)=8.1650
    assert stat.peak_area_median == 110.0
    assert stat.peak_area_cv_pct == pytest.approx(7.4229, abs=0.001)
    assert stat.retention_time_median == 5.0
    assert stat.n == 3


def test_compute_baseline_preview_excludes_unchecked_runs() -> None:
    runs = [
        _run_dict("r1", 100.0, 5.0),
        _run_dict("r2", 999.0, 9.0),  # not selected — must not affect stats
    ]
    stats = gs.compute_baseline_preview(runs, {"r1"})
    assert stats["Prot|PEP"].peak_area_median == 100.0
    assert stats["Prot|PEP"].n == 1


def test_compute_baseline_preview_empty_checked_set_returns_empty() -> None:
    runs = [_run_dict("r1", 100.0, 5.0)]
    assert gs.compute_baseline_preview(runs, set()) == {}


def test_compute_baseline_preview_excludes_flagged_peptide_class() -> None:
    runs = [
        _run_dict("r1", 100.0, 5.0, peptide_class="Digest"),
    ]
    rules = [
        PeptideClassRule(
            protein_name="Prot", label="Digest", purpose="digest_efficiency",
            exclude_from_baseline=True,
        )
    ]
    stats = gs.compute_baseline_preview(runs, {"r1"}, rules)
    assert stats == {}


def test_compute_baseline_preview_keeps_unflagged_class() -> None:
    runs = [_run_dict("r1", 100.0, 5.0, peptide_class="Recovery")]
    rules = [PeptideClassRule(protein_name="Prot", label="Recovery", purpose="recovery")]
    stats = gs.compute_baseline_preview(runs, {"r1"}, rules)
    assert "Prot|PEP" in stats


# ─── save_baseline / get_active_baseline / list_baselines ─────────────────


def _seed_runs(n: int, area: float = 1000.0) -> list[str]:
    ids = []
    for _ in range(n):
        extraction = _extraction([_pep("PEPA", area=area, rt=5.0)])
        ids.append(str(extraction.run_id))
        gs.record_ssc0_run(_classification(), extraction)
    return ids


def test_save_baseline_persists_and_activates(tmp_data_dir: Path) -> None:
    run_ids = _seed_runs(3)
    record = gs.save_baseline("Astral_0001", 200, run_ids, label="Install baseline")

    assert record["label"] == "Install baseline"
    assert sorted(record["source_run_ids"]) == sorted(run_ids)
    assert record["per_peptide"]["Non_reactive_Targets|PEPA"]["peak_area_median"] == 1000.0

    active = gs.get_active_baseline("Astral_0001", 200)
    assert active is not None
    assert active["baseline_id"] == record["baseline_id"]


def test_save_baseline_label_defaults_when_blank(tmp_data_dir: Path) -> None:
    run_ids = _seed_runs(1)
    record = gs.save_baseline("Astral_0001", 200, run_ids, label="   ")
    assert record["label"].startswith("Baseline ")


def test_save_baseline_versions_history(tmp_data_dir: Path) -> None:
    run_ids = _seed_runs(2, area=1000.0)
    first = gs.save_baseline("Astral_0001", 200, run_ids[:1], label="First")
    second = gs.save_baseline("Astral_0001", 200, run_ids, label="Second")

    assert first["baseline_id"] != second["baseline_id"]
    active = gs.get_active_baseline("Astral_0001", 200)
    assert active is not None
    assert active["baseline_id"] == second["baseline_id"]

    history = gs.list_baselines("Astral_0001", 200)
    assert {r["baseline_id"] for r in history} == {first["baseline_id"], second["baseline_id"]}
    assert history[0]["baseline_id"] == second["baseline_id"]  # newest first


def test_get_active_baseline_none_when_unset(tmp_data_dir: Path) -> None:
    assert gs.get_active_baseline("Astral_0001", 200) is None


def test_list_baselines_empty_when_unset(tmp_data_dir: Path) -> None:
    assert gs.list_baselines("Astral_0001", 200) == []


# ─── comparison_metrics (Evosep decision matrix, 2026-07-28) ───────────────


def _baseline_with(area: float, rt: float, idotp: float = 0.95, dotp: float = 0.90) -> dict:
    return {
        "baseline_id": "bl-1",
        "per_peptide": {
            "Prot|PEP": {
                "peak_area_median": area,
                "retention_time_median": rt,
                "isotope_dot_product_median": idotp,
                "library_dot_product_median": dotp,
            }
        },
    }


def _target(
    area: float | None = None,
    rt: float | None = None,
    idotp: float | None = None,
    dotp: float | None = None,
) -> TargetMetric:
    return TargetMetric(
        target_id="PEP", peptide_sequence="PEP", protein_name="Prot",
        peak_area=area, retention_time=rt,
        isotope_dot_product=idotp, library_dot_product=dotp,
    )


def test_comparison_healthy_run_is_close_to_baseline() -> None:
    """QC B at full load reads ~100% of SSC0 (Evosep measured 101-105%)."""
    baseline = _baseline_with(area=1000.0, rt=5.0)
    targets = [_target(area=1020.0, rt=5.01, idotp=0.95, dotp=0.90)]
    m = gs.compute_comparison_metrics(targets, baseline)

    pep = m["per_peptide"]["Prot|PEP"]
    assert pep["peak_area_ratio_to_baseline"] == pytest.approx(1.02)
    assert pep["peak_area_deviation_pct"] == pytest.approx(2.0)
    assert pep["rt_deviation_pct"] == pytest.approx(0.2)
    assert pep["target_extraction_suspect"] is False
    assert m["peak_area_verdict"] == "ok"
    assert m["targets_extraction_suspect"] == 0
    assert m["baseline_id"] == "bl-1"


def test_comparison_flags_mis_extracted_target() -> None:
    """Reproduces the RISGLIYEETR signature Evosep found at 500 SPD.

    Wrong peak picked: retention time far off the SSC0 median, dot products
    depressed, peak area collapsed. Must be flagged as an extraction problem
    rather than read as an LC-MS performance drop.
    """
    baseline = _baseline_with(area=1000.0, rt=5.0, idotp=0.95, dotp=0.90)
    targets = [_target(area=350.0, rt=6.5, idotp=0.59, dotp=0.60)]
    m = gs.compute_comparison_metrics(targets, baseline)

    pep = m["per_peptide"]["Prot|PEP"]
    assert pep["rt_outside_threshold"] is True
    assert pep["target_extraction_suspect"] is True
    assert m["targets_extraction_suspect"] == 1


def test_comparison_dot_product_alone_does_not_flag() -> None:
    """Evosep saw a wrongly-extracted peak at idotp 0.92 — dot products alone
    are not diagnostic, so a low one with stable RT must not be flagged."""
    baseline = _baseline_with(area=1000.0, rt=5.0, idotp=0.95, dotp=0.90)
    targets = [_target(area=980.0, rt=5.005, idotp=0.70, dotp=0.60)]
    m = gs.compute_comparison_metrics(targets, baseline)
    assert m["per_peptide"]["Prot|PEP"]["target_extraction_suspect"] is False


def test_comparison_rt_drift_alone_does_not_flag() -> None:
    """RT drift with healthy area and dot products is an LC shift, not a
    mis-extraction — the rule is deliberately a conjunction."""
    baseline = _baseline_with(area=1000.0, rt=5.0)
    targets = [_target(area=1000.0, rt=5.5, idotp=0.95, dotp=0.90)]
    m = gs.compute_comparison_metrics(targets, baseline)
    pep = m["per_peptide"]["Prot|PEP"]
    assert pep["rt_outside_threshold"] is True
    assert pep["target_extraction_suspect"] is False


@pytest.mark.parametrize(
    ("area", "expected"),
    [(1000.0, "ok"), (895.0, "warn"), (740.0, "fail")],
)
def test_comparison_peak_area_verdict_bands(area: float, expected: str) -> None:
    """10% below SSC0 warns (~25% Evotip load loss); 25% fails (~50% loss)."""
    m = gs.compute_comparison_metrics(
        [_target(area=area, rt=5.0)], _baseline_with(area=1000.0, rt=5.0)
    )
    assert m["peak_area_verdict"] == expected


def test_comparison_thresholds_are_configurable_and_recorded() -> None:
    from mdqc.config.schema import QcThresholdsConfig

    baseline = _baseline_with(area=1000.0, rt=5.0)
    targets = [_target(area=1000.0, rt=5.15)]  # 3% RT deviation

    default = gs.compute_comparison_metrics(targets, baseline)
    assert default["per_peptide"]["Prot|PEP"]["rt_outside_threshold"] is True

    relaxed = gs.compute_comparison_metrics(
        targets, baseline, QcThresholdsConfig(rt_deviation_pct_max=5.0)
    )
    assert relaxed["per_peptide"]["Prot|PEP"]["rt_outside_threshold"] is False
    # The applied thresholds travel with the payload so the platform can tell
    # which numbers produced a verdict.
    assert relaxed["thresholds_applied"]["rt_deviation_pct_max"] == 5.0


def test_comparison_skips_peptides_absent_from_baseline() -> None:
    baseline = _baseline_with(area=1000.0, rt=5.0)
    other = TargetMetric(
        target_id="X", peptide_sequence="OTHER", protein_name="Other",
        peak_area=500.0, retention_time=9.0,
    )
    m = gs.compute_comparison_metrics([other], baseline)
    assert m["targets_compared"] == 0
    assert m["per_peptide"] == {}


def test_comparison_tolerates_baseline_without_dot_products() -> None:
    """Baselines recorded before v0.5.8 have no dot-product medians."""
    baseline = {
        "baseline_id": "bl-old",
        "per_peptide": {
            "Prot|PEP": {"peak_area_median": 1000.0, "retention_time_median": 5.0}
        },
    }
    m = gs.compute_comparison_metrics([_target(area=1000.0, rt=5.0, idotp=0.9)], baseline)
    pep = m["per_peptide"]["Prot|PEP"]
    assert pep["isotope_dot_product_deviation_pct"] is None
    assert pep["target_extraction_suspect"] is False


def test_comparison_handles_missing_measurements() -> None:
    m = gs.compute_comparison_metrics([_target()], _baseline_with(area=1000.0, rt=5.0))
    pep = m["per_peptide"]["Prot|PEP"]
    assert pep["peak_area_ratio_to_baseline"] is None
    assert pep["rt_deviation_pct"] is None
    assert m["median_peak_area_ratio"] is None
    assert m["peak_area_verdict"] is None


def test_comparison_against_evosep_measured_200spd_ratios() -> None:
    """Replays Evosep's own measured SSC0 area ratios (2026-07-28, slide 70).

    Documents a live calibration finding: the 75% QC B condition medians at
    -9.5%, which clears the stated 10% warn threshold by half a point and so
    reads "ok" — the very case the threshold was chosen to catch. Kept at
    Evosep's number rather than silently retuned; see QcThresholdsConfig.
    """
    peps = [
        "RISGLIYEETR", "ISGLIYEETR", "IGGIGTVPVGR", "GALQNIIPASTGAAK",
        "TTPSYVAFTDTER", "VSFELFADK", "YISPDQLADLYK", "YRPGTVALR",
    ]
    observed = {
        "100": ([101, 102, 101, 102, 105, 103, 104, 103], "ok"),
        "75": ([88, 91, 87, 90, 91, 95, 97, 86], "ok"),
        "50": ([66, 75, 61, 72, 73, 76, 81, 67], "fail"),
    }
    baseline = {
        "baseline_id": "bl",
        "per_peptide": {
            f"P|{p}": {"peak_area_median": 1000.0, "retention_time_median": 5.0}
            for p in peps
        },
    }
    for cond, (vals, expected) in observed.items():
        targets = [
            TargetMetric(
                target_id=p, peptide_sequence=p, protein_name="P",
                peak_area=10.0 * v, retention_time=5.0,
            )
            for p, v in zip(peps, vals, strict=True)
        ]
        m = gs.compute_comparison_metrics(targets, baseline)
        assert m["peak_area_verdict"] == expected, f"{cond}% condition"

    m75 = gs.compute_comparison_metrics(
        [
            TargetMetric(
                target_id=p, peptide_sequence=p, protein_name="P",
                peak_area=10.0 * v, retention_time=5.0,
            )
            for p, v in zip(peps, observed["75"][0], strict=True)
        ],
        baseline,
    )
    assert m75["median_peak_area_deviation_pct"] == pytest.approx(-9.5)


# ─── build_payload_comparison ──────────────────────────────────────────────


def test_build_payload_comparison_returns_none_without_baseline(
    tmp_data_dir: Path,
) -> None:
    assert gs.build_payload_comparison(
        _classification(), _extraction([_pep("PEPA")])
    ) == (None, None)


def test_build_payload_comparison_populates_both_fields(tmp_data_dir: Path) -> None:
    run_ids = _seed_runs(3, area=1000.0)
    gs.save_baseline("Astral_0001", 200, run_ids, label="Install")

    context, metrics = gs.build_payload_comparison(
        _classification(control_type=ControlType.QC_B),
        _extraction([_pep("PEPA", area=1020.0, rt=5.0)]),
    )

    assert context is not None and metrics is not None
    assert context["label"] == "Install"
    assert context["source_run_count"] == 3
    assert "Non_reactive_Targets|PEPA" in context["per_peptide"]
    assert metrics["baseline_id"] == context["baseline_id"]
    assert metrics["median_peak_area_ratio"] == pytest.approx(1.02)


def test_build_payload_comparison_matches_baseline_by_spd(tmp_data_dir: Path) -> None:
    """A 200 SPD run must not be compared against a 500 SPD baseline."""
    run_ids = _seed_runs(2, area=1000.0)
    gs.save_baseline("Astral_0001", 200, run_ids, label="200 only")

    context, metrics = gs.build_payload_comparison(
        _classification(spd=500), _extraction([_pep("PEPA", area=1000.0)])
    )
    assert (context, metrics) == (None, None)
