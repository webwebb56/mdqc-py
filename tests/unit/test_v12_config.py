"""v1.2 config: per-SPD thresholds, default peptide classes, strict naming."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mdqc.classifier import classify_filename
from mdqc.config import load_config
from mdqc.config.schema import ClassifierRule, Config
from mdqc.peptide_classes import assign_peptide_classes
from mdqc.types import ControlType, TargetMetric

# ── per-SPD thresholds ───────────────────────────────────────────────────────


def test_overrides_apply_only_at_their_spd() -> None:
    cfg = Config(qc_thresholds_by_spd={500: {"rt_deviation_pct_max": 1.0}})
    assert cfg.thresholds_for(500).rt_deviation_pct_max == 1.0
    assert cfg.thresholds_for(200).rt_deviation_pct_max == 2.0
    assert cfg.thresholds_for(None).rt_deviation_pct_max == 2.0
    # Everything not overridden is inherited.
    assert cfg.thresholds_for(500).peak_area_deviation_pct_fail == 25.0


def test_an_override_reports_its_thresholds_as_custom() -> None:
    cfg = Config(qc_thresholds_by_spd={500: {"rt_deviation_pct_max": 1.0}})
    assert not cfg.thresholds_for(500).is_default()
    assert cfg.thresholds_for(200).is_default()


def test_unknown_threshold_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown threshold"):
        Config(qc_thresholds_by_spd={500: {"rt_tolerance": 1.0}})


def test_an_override_that_breaks_warn_below_fail_is_rejected_at_load() -> None:
    with pytest.raises(ValidationError):
        Config(qc_thresholds_by_spd={500: {"peak_area_deviation_pct_warn": 40.0}})


def test_per_spd_thresholds_load_from_toml(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[qc_thresholds_by_spd.500]\nrt_deviation_pct_max = 1.0\n", encoding="utf-8")
    assert load_config(p).thresholds_for(500).rt_deviation_pct_max == 1.0


def test_settings_save_carries_the_new_fields_forward() -> None:
    """The Settings form rebuilds Config field by field; a field it doesn't
    pass on is reset to its default — the same trap as the cloud token."""
    import inspect

    from mdqc.webui import settings

    src = inspect.getsource(settings)
    assert "qc_thresholds_by_spd=prev.qc_thresholds_by_spd" in src
    assert "classification_mode=prev.classification_mode" in src


# ── default peptide classes ──────────────────────────────────────────────────


@pytest.mark.parametrize("saved", [None, []], ids=["absent", "empty-list"])
def test_panel_classes_apply_when_none_are_configured(saved: list | None) -> None:
    """Every Settings save before 1.2 wrote `peptide_classes = []`."""
    cfg = Config() if saved is None else Config(peptide_classes=saved)
    purposes = {r.protein_name: r.purpose for r in cfg.peptide_classes}
    assert purposes == {
        "Non_reactive_Targets": "recovery",
        "Miss-cleavage_pair": "digest_efficiency",
        "Miss-clevage_pair": "digest_efficiency",
    }


def test_configured_classes_replace_the_defaults() -> None:
    cfg = Config(peptide_classes=[{"protein_name": "MyTargets", "purpose": "recovery"}])
    assert [r.protein_name for r in cfg.peptide_classes] == ["MyTargets"]


@pytest.mark.parametrize("protein", ["Miss-cleavage_pair", "Miss-clevage_pair"])
def test_the_miss_cleavage_pair_gets_the_digest_purpose(protein: str) -> None:
    """Both spellings occur in real templates: the vendor's current report and
    an earlier one (tests/fixtures/skyline_transition_report.csv)."""
    metrics = [TargetMetric(target_id="a", protein_name=protein)]
    assign_peptide_classes(metrics, Config().peptide_classes)
    assert metrics[0].peptide_class_purpose == "digest_efficiency"


# ── strict naming ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "strict", "lenient"),
    [
        ("PCD96_S00038_A12345678910_S1A1_L00001-123456.raw", ControlType.PCD, ControlType.PCD),
        ("PCD384_S00038_A12345678911_S1A1_L00001-123457.raw", ControlType.PCD, ControlType.PCD),
        ("SSC_S00038_A12345678910_S1A1_L00001-123458.raw", ControlType.SSC, ControlType.SSC),
        ("SSCGOLD_S00038_A12345678910_S1A1_L00001-1.raw", ControlType.SSC0, ControlType.SSC0),
        ("SSCBASE_S00038_A12345678910_S1A1_L00001-2.raw", ControlType.SSC0, ControlType.SSC0),
        # A longer word that starts the same is not the marker.
        ("SSCBASELINE_study.raw", ControlType.SAMPLE, ControlType.SAMPLE),
        ("L00001-123459.raw", ControlType.SAMPLE, ControlType.SAMPLE),
        # Customer samples that lenient mode would take for QC runs.
        ("PC_cells_rep1.raw", ControlType.SAMPLE, ControlType.PCD),
        ("mystudy_SSC_ctrl.raw", ControlType.SAMPLE, ControlType.SSC),
        ("patient7_A1_run.raw", ControlType.SAMPLE, ControlType.PCD),
    ],
)
def test_strict_mode_reads_only_the_first_part(
    name: str, strict: ControlType, lenient: ControlType
) -> None:
    assert classify_filename(name, mode="strict").control_type is strict
    assert classify_filename(name).control_type is lenient


def test_strict_mode_keeps_the_kit() -> None:
    assert classify_filename("PCD384_S00038_x.raw", mode="strict").kit_format == 384


def test_strict_mode_ignores_the_shipped_substring_rules() -> None:
    """QCB, BLK and the rest would bring the false positives straight back."""
    rules = Config().classifier_rules
    assert classify_filename("cust_QCB_sample.raw", rules=rules, mode="strict").control_type is (
        ControlType.SAMPLE
    )
    assert classify_filename("cust_QCB_sample.raw", rules=rules).control_type is ControlType.SSC


def test_strict_mode_still_applies_an_operators_own_rule() -> None:
    rules = [ClassifierRule(pattern="MYQC", control_type="SSC")]
    assert classify_filename("lab_MYQC_run.raw", rules=rules, mode="strict").control_type is (
        ControlType.SSC
    )
