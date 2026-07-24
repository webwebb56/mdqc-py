"""Tests for the peptide-class assignment + class-aware rollups."""

from __future__ import annotations

import pytest

from mdqc.config.schema import PeptideClassRule
from mdqc.peptide_classes import (
    assign_peptide_classes,
    compute_digest_efficiency,
    filter_for_recovery,
)
from mdqc.types import TargetMetric


def _make(seq: str, protein: str | None = None, area: float | None = 1000.0) -> TargetMetric:
    return TargetMetric(
        target_id=seq,
        peptide_sequence=seq,
        protein_name=protein,
        peak_area=area,
        detected=area is not None and area > 0,
    )


def test_assign_peptide_classes_basic() -> None:
    metrics = [
        _make("PEPA", "Non_reactive_Targets"),
        _make("PEPB", "Miss-cleavage_pair"),
        _make("PEPC", "Non_reactive_Targets"),
    ]
    rules = [
        PeptideClassRule(protein_name="Non_reactive_Targets", purpose="recovery"),
        PeptideClassRule(
            protein_name="Miss-cleavage_pair", purpose="digest_efficiency",
            exclude_from_recovery=True,
        ),
    ]
    assign_peptide_classes(metrics, rules)
    assert metrics[0].peptide_class == "Non_reactive_Targets"
    assert metrics[0].peptide_class_purpose == "recovery"
    assert metrics[1].peptide_class_purpose == "digest_efficiency"
    assert metrics[2].peptide_class == "Non_reactive_Targets"


def test_assign_uses_label_when_provided() -> None:
    metrics = [_make("PEP", "Non_reactive_Targets")]
    rules = [PeptideClassRule(
        protein_name="Non_reactive_Targets",
        label="Non-reactive",
        purpose="recovery",
    )]
    assign_peptide_classes(metrics, rules)
    assert metrics[0].peptide_class == "Non-reactive"


def test_assign_no_rules_is_noop() -> None:
    metrics = [_make("PEP", "Some_Protein")]
    assign_peptide_classes(metrics, [])
    assert metrics[0].peptide_class is None


def test_assign_substring_match_case_insensitive() -> None:
    metrics = [_make("PEP", "NON_REACTIVE_targets_extra_suffix")]
    rules = [PeptideClassRule(protein_name="non_reactive_targets")]
    assign_peptide_classes(metrics, rules)
    assert metrics[0].peptide_class == "non_reactive_targets"


def test_filter_for_recovery_excludes_digest_efficiency() -> None:
    rules = [
        PeptideClassRule(protein_name="Targets", purpose="recovery"),
        PeptideClassRule(protein_name="MissCleavage", purpose="digest_efficiency"),
    ]
    metrics = [
        _make("A", "Targets"),
        _make("B", "MissCleavage"),
        _make("C", "Targets"),
    ]
    assign_peptide_classes(metrics, rules)
    kept = filter_for_recovery(metrics, rules)
    assert {m.peptide_sequence for m in kept} == {"A", "C"}


def test_filter_for_recovery_respects_exclude_flag() -> None:
    rules = [
        PeptideClassRule(
            protein_name="HouseKeeping", purpose="recovery",
            exclude_from_recovery=True,
        ),
    ]
    metrics = [_make("A", "HouseKeeping"), _make("B", None)]
    assign_peptide_classes(metrics, rules)
    kept = filter_for_recovery(metrics, rules)
    assert [m.peptide_sequence for m in kept] == ["B"]


def test_filter_for_recovery_keeps_unclassified() -> None:
    rules = [PeptideClassRule(protein_name="Targets", purpose="recovery")]
    metrics = [_make("A", "UnknownProtein")]
    assign_peptide_classes(metrics, rules)
    assert filter_for_recovery(metrics, rules) == metrics


def test_compute_digest_efficiency_returns_ratio() -> None:
    # 0-miss = shorter sequence (3000), 1-miss = longer (1000)
    # ratio = 3000 / (3000 + 1000) = 0.75
    metrics = [
        _make("LONGERPEPTIDE", "MissCleavage", area=1000.0),
        _make("PEPTIDE", "MissCleavage", area=3000.0),
    ]
    rules = [PeptideClassRule(protein_name="MissCleavage", purpose="digest_efficiency")]
    assign_peptide_classes(metrics, rules)
    ratio = compute_digest_efficiency(metrics)
    assert ratio == pytest.approx(0.75)


def test_compute_digest_efficiency_none_when_no_class() -> None:
    metrics = [_make("PEP", "Targets")]
    assert compute_digest_efficiency(metrics) is None


def test_compute_digest_efficiency_none_when_both_zero() -> None:
    metrics = [
        _make("A", "MissCleavage", area=0.0),
        _make("BB", "MissCleavage", area=0.0),
    ]
    rules = [PeptideClassRule(protein_name="MissCleavage", purpose="digest_efficiency")]
    assign_peptide_classes(metrics, rules)
    assert compute_digest_efficiency(metrics) is None


# ─── _run_metrics emits digest_efficiency_pct (v0.5.0 §3.6) ───────────────────

def test_run_metrics_emits_digest_efficiency_pct() -> None:
    from mdqc.extractor import _run_metrics

    # Cleaved (shorter) ISGLIYEETR area 4.2e6; miss-cleaved RISGLIYEETR 0.87e6.
    # digest efficiency = 4.2 / (4.2 + 0.87) = 82.84%.
    metrics = [
        _make("ISGLIYEETR", "Miss-clevage_pair", area=4_200_000.0),
        _make("RISGLIYEETR", "Miss-clevage_pair", area=870_000.0),
        _make("IGGIGTVPVGR", "Non_reactive_Targets", area=5_000_000.0),
    ]
    rules = [
        PeptideClassRule(protein_name="Non_reactive_Targets", purpose="recovery"),
        PeptideClassRule(
            protein_name="Miss-clevage_pair",
            purpose="digest_efficiency",
            exclude_from_recovery=True,
        ),
    ]
    assign_peptide_classes(metrics, rules)
    rm = _run_metrics(metrics, rules)

    assert rm.digest_efficiency_pct == pytest.approx(82.84, abs=0.01)
    # The miss-cleavage pair is excluded from recovery: only the 1 recovery
    # peptide counts, not all 3.
    assert rm.targets_expected == 1
    assert rm.targets_found == 1


def test_run_metrics_digest_efficiency_none_without_class() -> None:
    from mdqc.extractor import _run_metrics

    metrics = [_make("PEP", "Targets", area=1000.0)]
    rm = _run_metrics(metrics, None)
    assert rm.digest_efficiency_pct is None
