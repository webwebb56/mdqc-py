from __future__ import annotations

from pathlib import Path

import pytest

from mdqc.classifier import classify_file, classify_filename
from mdqc.config.schema import ClassifierRule
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    WellPosition,
)

CORPUS_PATH = Path(__file__).parent.parent / "fixtures" / "classifier_corpus.txt"


def _load_corpus() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for raw in CORPUS_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        assert len(parts) == 4, f"bad fixture row: {raw!r}"
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


@pytest.mark.parametrize(
    ("filename", "expected_control", "expected_well", "expected_confidence"),
    _load_corpus(),
)
def test_classifier_corpus(
    filename: str,
    expected_control: str,
    expected_well: str,
    expected_confidence: str,
) -> None:
    result = classify_filename(filename)
    assert result.control_type.value == expected_control, filename
    if expected_well:
        assert result.well_position is not None, filename
        assert str(result.well_position) == expected_well, filename
    else:
        assert result.well_position is None, filename
    assert result.confidence.value == expected_confidence, filename


@pytest.mark.parametrize("ext", [".raw", ".d", ".wiff"])
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("SSC0", ControlType.SSC0),
        ("QCA", ControlType.QC_A),
        ("QCB", ControlType.QC_B),
        ("BLANK", ControlType.BLANK),
    ],
)
def test_each_control_each_extension(
    token: str, expected: ControlType, ext: str
) -> None:
    name = f"INST01_{token}_A1_2026-01-27{ext}"
    result = classify_filename(name)
    assert result.control_type is expected
    assert result.source is ClassificationSource.FILENAME


def test_mixed_delimiters_underscore_dash_dot() -> None:
    result = classify_filename("EXPLORIS01_QCB-A3.raw")
    assert result.control_type is ControlType.QC_B
    assert result.well_position == WellPosition(row="A", column=3)
    assert result.confidence is Confidence.HIGH


def test_dot_delimited_token() -> None:
    result = classify_filename("TIMSTOF01.SSC0_A1.d")
    assert result.control_type is ControlType.SSC0
    assert result.well_position == WellPosition(row="A", column=1)


@pytest.mark.parametrize("variant", ["qca", "QCA", "QcA", "qC_a", "Qc-A"])
def test_case_insensitive_qca(variant: str) -> None:
    result = classify_filename(f"INST_{variant}_A1.raw")
    assert result.control_type is ControlType.QC_A


def test_well_position_inference_qca() -> None:
    result = classify_filename("EXPLORIS_A1_2026-01-27.raw")
    assert result.control_type is ControlType.QC_A
    assert result.source is ClassificationSource.POSITION
    assert result.confidence is Confidence.MEDIUM


def test_well_position_inference_qcb() -> None:
    result = classify_filename("EXPLORIS_A3_2026-01-27.raw")
    assert result.control_type is ControlType.QC_B
    assert result.source is ClassificationSource.POSITION


def test_well_position_other_remains_sample() -> None:
    result = classify_filename("EXPLORIS_B5_2026-01-27.raw")
    assert result.control_type is ControlType.SAMPLE
    assert result.well_position == WellPosition(row="B", column=5)
    assert result.confidence is Confidence.LOW


def test_plate_id_extraction_plate_word() -> None:
    result = classify_filename("EXPLORIS_QCA_A1_plate1_run.raw")
    assert result.plate_id is not None
    assert "plate1" in result.plate_id.lower()


def test_plate_id_extraction_p_prefix() -> None:
    result = classify_filename("EXPLORIS_QCA_A1_P001.raw")
    assert result.plate_id == "P001"


def test_plate_id_extraction_plt_prefix() -> None:
    result = classify_filename("WATERS_SSC0_A1_plt-2.raw")
    assert result.plate_id is not None
    assert result.plate_id.lower().startswith("plt")


def test_no_plate_id() -> None:
    result = classify_filename("INST_QCA_A1.raw")
    assert result.plate_id is None


def test_instrument_id_extraction() -> None:
    result = classify_filename("EXPLORIS01_QCA_A1_2026-01-27.raw")
    assert result.instrument_id == "EXPLORIS01"


def test_instrument_id_when_no_token() -> None:
    result = classify_filename("INST_2026-01-27.raw")
    assert result.instrument_id == "INST"


# ─── SPD (Evosep samples-per-day) ────────────────────────────────────────────

def test_spd_extracted_lowercase() -> None:
    result = classify_filename(
        "2026-04-28_astral_p087_200spd_k562_50ng_QCB.raw"
    )
    assert result.spd == 200


def test_spd_extracted_uppercase() -> None:
    result = classify_filename("astral_500SPD_k562_QCA.raw")
    assert result.spd == 500


def test_spd_extracted_with_dash_separator() -> None:
    result = classify_filename("astral_100-SPD_QCB.raw")
    assert result.spd == 100


def test_spd_absent_returns_none() -> None:
    result = classify_filename("EXPLORIS_QCA_A1_2026-01-27.raw")
    assert result.spd is None


def test_spd_ignored_inside_other_word() -> None:
    # `200spdx` should NOT match — delimiters required after the digits+SPD.
    result = classify_filename("astral_200spdx_k562_QCA.raw")
    assert result.spd is None


# ─── Dilution level (threshold-calibration series) ───────────────────────────

def test_dilution_extracted_perc() -> None:
    result = classify_filename(
        "QCB_75perc_2026-07-24_ss_50ng_Manual_200spd_k562_S1-H5_1.d"
    )
    assert result.dilution_pct == 75
    assert result.control_type is ControlType.QC_B
    assert result.spd == 200


def test_dilution_extracted_pct_and_percent_and_symbol() -> None:
    assert classify_filename("QCB_50pct_200spd.raw").dilution_pct == 50
    assert classify_filename("QCB_25percent_200spd.raw").dilution_pct == 25
    assert classify_filename("QCB_100%_200spd.raw").dilution_pct == 100


def test_dilution_absent_returns_none() -> None:
    result = classify_filename("SSC0_2026-07-24_ss_50ng_Manual_200spd_k562_S1-B1_1.d")
    assert result.dilution_pct is None


def test_dilution_out_of_range_rejected() -> None:
    # A concentration token above 100% isn't a dilution — don't invent one.
    assert classify_filename("QCB_150perc_200spd.raw").dilution_pct is None
    assert classify_filename("QCB_0perc_200spd.raw").dilution_pct is None


def test_dilution_does_not_collide_with_spd() -> None:
    """`200spd` must not be read as a dilution, nor `75perc` as an SPD."""
    result = classify_filename("QCB_75perc_500spd_k562.raw")
    assert result.dilution_pct == 75
    assert result.spd == 500


def test_dilution_survives_custom_classifier_rule() -> None:
    """The custom-rule early-return path must populate dilution too."""
    rules = [ClassifierRule(pattern="QCB", control_type="QC_B")]
    result = classify_filename("QCB_50perc_300spd_k562.raw", rules=rules)
    assert result.control_type is ControlType.QC_B
    assert result.dilution_pct == 50
    assert result.spd == 300


def test_priority_ssc0_before_qca() -> None:
    # Both SSC0 and QCA tokens present; SSC0 wins by priority.
    result = classify_filename("INST_SSC0_QCA_A1.raw")
    assert result.control_type is ControlType.SSC0


def test_classify_file_uses_basename(tmp_path: Path) -> None:
    p = tmp_path / "EXPLORIS01_QCA_A1_2026-01-27.raw"
    p.write_bytes(b"")
    result = classify_file(p)
    assert result.control_type is ControlType.QC_A
    assert result.well_position == WellPosition(row="A", column=1)


def test_default_sample_no_pattern() -> None:
    result = classify_filename("totally_random_name.raw")
    assert result.control_type is ControlType.SAMPLE
    assert result.source is ClassificationSource.DEFAULT
    assert result.confidence is Confidence.LOW
    assert result.well_position is None


def test_high_confidence_requires_well_and_token() -> None:
    with_well = classify_filename("INST_QCA_A1.raw")
    without_well = classify_filename("INST_QCA_run.raw")
    assert with_well.confidence is Confidence.HIGH
    assert without_well.confidence is Confidence.MEDIUM


# ── Evosep August 2026 naming: PC / SSC / SSC-gold ──────────────────────────
# QC A -> PC (process control), QC B -> SSC (system suitability control),
# SSC0 -> SSC-gold. Both spellings classify; ControlType wire values are
# deliberately unchanged because the platform already consumes them.


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        # New markers
        ("INST_PC_200SPD", ControlType.QC_A),
        ("INST_SSC_200SPD", ControlType.QC_B),
        ("INST_SSCGOLD_200SPD", ControlType.SSC0),
        ("INST_SSC_GOLD_200SPD", ControlType.SSC0),
        ("INST_SSC-GOLD_200SPD", ControlType.SSC0),
        # Legacy markers still classify
        ("INST_QCA_200SPD", ControlType.QC_A),
        ("INST_QC_A_200SPD", ControlType.QC_A),
        ("INST_QCB_200SPD", ControlType.QC_B),
        ("INST_QC-B_200SPD", ControlType.QC_B),
        ("INST_SSC0_200SPD", ControlType.SSC0),
    ],
)
def test_new_and_legacy_control_markers(stem: str, expected: ControlType) -> None:
    assert classify_filename(f"{stem}.raw").control_type is expected


def test_bare_ssc_now_means_routine_not_gold_standard() -> None:
    """The one deliberate behaviour change in the rename.

    Bare ``SSC`` used to classify as the gold standard. Under the new naming
    SSC *is* the routine system-suitability control and the baseline is
    SSC-gold, so leaving it pointing at the baseline would make the filename
    mean the opposite of the SOP.
    """
    assert classify_filename("INST_SSC_A1.raw").control_type is ControlType.QC_B
    # The gold standard still has unambiguous markers.
    assert classify_filename("INST_SSC0_A1.raw").control_type is ControlType.SSC0
    assert classify_filename("INST_SSCGOLD_A1.raw").control_type is ControlType.SSC0


def test_ssc_gold_wins_over_the_bare_ssc_inside_it() -> None:
    """`SSC_GOLD` contains a delimited `SSC`; gold must be tested first."""
    result = classify_filename("TIMSTOF01_SSC_GOLD_200SPD_S00462.d")
    assert result.control_type is ControlType.SSC0


def test_pc_marker_does_not_match_inside_a_word() -> None:
    """`PC` is short; it must only match as a delimited token."""
    assert classify_filename("my_pcr_sample_200SPD.raw").control_type is ControlType.SAMPLE
    assert classify_filename("hpc_run_200SPD.raw").control_type is ControlType.SAMPLE


# ── Evosep Eno/One serial ───────────────────────────────────────────────────


def test_lc_serial_parsed_from_filename() -> None:
    result = classify_filename("20260818_SSCGOLD_200SPD_S00462_r01.d")
    assert result.lc_serial == "S00462"
    assert result.spd == 200


def test_lc_serial_is_normalised_to_upper_case() -> None:
    assert classify_filename("20260818_SSC_200SPD_s00618.d").lc_serial == "S00618"


def test_lc_serial_absent_is_none() -> None:
    assert classify_filename("20260818_PC_200SPD.d").lc_serial is None


def test_lc_serial_requires_exactly_five_digits() -> None:
    """Evosep serials are S + 5 digits; don't swallow other S-prefixed tokens."""
    assert classify_filename("x_S1234_200SPD.d").lc_serial is None
    assert classify_filename("x_S1234567_200SPD.d").lc_serial is None


def test_lc_serial_reaches_classification_dict() -> None:
    result = classify_filename("20260818_SSC_200SPD_S00462.d")
    assert result.to_dict()["lc_serial"] == "S00462"
