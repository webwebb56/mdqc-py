"""Tests for transition -> peptide collapse (v0.5.0).

Skyline reports with rowsource="Transition" emit 3-8 rows per peptide. The
collapse folds them to one row per peptide: numeric fields are averaged (mean
of N identical values = that value; per-fragment Skewness/Kurtosis average
correctly, skipping NaN), strings take first-non-null, detected is OR-ed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mdqc.extractor.report import collapse_transitions_to_peptides, parse_skyline_csv
from mdqc.types import TargetMetric

FIXTURES = Path(__file__).parent.parent / "fixtures"
TRANSITION_REPORT = FIXTURES / "skyline_transition_report.csv"


def _make(seq: str, protein: str, **kw) -> TargetMetric:
    return TargetMetric(target_id=seq, peptide_sequence=seq, protein_name=protein, **kw)


# ─── Real fixture (MD_QC_Report_20260723.csv) ────────────────────────────────

def test_real_transition_report_collapses_to_eight_peptides() -> None:
    rows = parse_skyline_csv(TRANSITION_REPORT)
    assert len(rows) == 66  # raw transition rows

    peptides = collapse_transitions_to_peptides(rows)
    assert len(peptides) == 8  # one per peptide

    seqs = {p.peptide_sequence for p in peptides}
    assert "RISGLIYEETR" in seqs      # miss-cleaved
    assert "ISGLIYEETR" in seqs       # cleaved
    assert "IGGIGTVPVGR" in seqs      # a non-reactive target

    proteins = {p.protein_name for p in peptides}
    assert proteins == {"Miss-clevage_pair", "Non_reactive_Targets"}


def test_total_area_is_not_summed_across_transitions() -> None:
    # RISGLIYEETR has Total Area 1418427 repeated across 9 rows. The collapse
    # must average (=> 1418427), not sum (=> 9x), otherwise recovery inflates.
    peptides = collapse_transitions_to_peptides(parse_skyline_csv(TRANSITION_REPORT))
    ris = next(p for p in peptides if p.peptide_sequence == "RISGLIYEETR")
    assert ris.peak_area == pytest.approx(1418427.0)


def test_per_fragment_skewness_is_averaged_skipping_nan() -> None:
    # RISGLIYEETR Skewness = [-0.454, -0.372, -0.49, NaN x6]. NaN parses to
    # None (via _NA_TOKENS) and is skipped; mean of the three real values.
    peptides = collapse_transitions_to_peptides(parse_skyline_csv(TRANSITION_REPORT))
    ris = next(p for p in peptides if p.peptide_sequence == "RISGLIYEETR")
    expected = (-0.454 + -0.372 + -0.49) / 3
    assert ris.extra_metrics["Skewness"] == pytest.approx(expected)


def test_repeated_metric_survives_collapse_unchanged() -> None:
    # Average Mass Error PPM repeats per transition; mean == the value.
    peptides = collapse_transitions_to_peptides(parse_skyline_csv(TRANSITION_REPORT))
    ris = next(p for p in peptides if p.peptide_sequence == "RISGLIYEETR")
    assert ris.mass_error_ppm == pytest.approx(2.1)


# ─── Synthetic unit cases ────────────────────────────────────────────────────

def test_collapse_averages_per_fragment_values() -> None:
    group = [
        _make("PEP", "Prot", peak_area=1000.0, extra_metrics={"Skewness": 0.2}),
        _make("PEP", "Prot", peak_area=1000.0, extra_metrics={"Skewness": 0.4}),
        _make("PEP", "Prot", peak_area=1000.0, extra_metrics={"Skewness": 0.6}),
    ]
    [out] = collapse_transitions_to_peptides(group)
    assert out.peak_area == pytest.approx(1000.0)          # repeated -> unchanged
    assert out.extra_metrics["Skewness"] == pytest.approx(0.4)  # per-fragment -> mean


def test_collapse_is_idempotent_on_single_rows() -> None:
    metrics = [
        _make("PEPA", "Prot", peak_area=100.0),
        _make("PEPB", "Prot", peak_area=200.0),
    ]
    out = collapse_transitions_to_peptides(metrics)
    assert len(out) == 2
    assert {m.peptide_sequence for m in out} == {"PEPA", "PEPB"}


def test_collapse_detected_is_ored() -> None:
    group = [
        _make("PEP", "Prot", peak_area=0.0, detected=False),
        _make("PEP", "Prot", peak_area=500.0, detected=True),
    ]
    [out] = collapse_transitions_to_peptides(group)
    assert out.detected is True


def test_collapse_groups_by_protein_and_sequence() -> None:
    # Same sequence string under two proteins stays two peptides.
    metrics = [
        _make("PEP", "ProtA", peak_area=100.0),
        _make("PEP", "ProtB", peak_area=200.0),
    ]
    out = collapse_transitions_to_peptides(metrics)
    assert len(out) == 2


def test_collapse_preserves_first_appearance_order() -> None:
    metrics = [
        _make("ZZZ", "Prot", peak_area=1.0),
        _make("ZZZ", "Prot", peak_area=1.0),
        _make("AAA", "Prot", peak_area=1.0),
    ]
    out = collapse_transitions_to_peptides(metrics)
    assert [m.peptide_sequence for m in out] == ["ZZZ", "AAA"]


def test_collapse_empty_list() -> None:
    assert collapse_transitions_to_peptides([]) == []


def test_collapse_drops_all_null_extra_metric() -> None:
    group = [
        _make("PEP", "Prot", peak_area=100.0, extra_metrics={}),
        _make("PEP", "Prot", peak_area=100.0, extra_metrics={}),
    ]
    [out] = collapse_transitions_to_peptides(group)
    assert out.extra_metrics == {}
