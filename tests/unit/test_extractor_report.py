from __future__ import annotations

from pathlib import Path

import pytest

from mdqc.extractor.report import parse_skyline_csv

FIXTURES = Path(__file__).parent.parent / "fixtures"
REPORT = FIXTURES / "skyline_report.csv"


def test_parses_fixture_rows() -> None:
    rows = parse_skyline_csv(REPORT)
    assert len(rows) == 5
    first = rows[0]
    assert first.peptide_sequence == "LGGNEQVTR"
    assert first.precursor_mz == pytest.approx(487.7568)
    assert first.retention_time == pytest.approx(12.45)
    assert first.rt_expected == pytest.approx(12.30)
    assert first.rt_delta == pytest.approx(12.45 - 12.30)
    assert first.peak_area == pytest.approx(1234567.0)
    assert first.peak_height == pytest.approx(98765.0)
    assert first.peak_width_fwhm == pytest.approx(0.21)
    assert first.mass_error_ppm == pytest.approx(1.4)
    assert first.isotope_dot_product == pytest.approx(0.95)
    assert first.library_dot_product == pytest.approx(0.89)
    assert first.detected is True


def test_target_id_stable_across_calls() -> None:
    first = parse_skyline_csv(REPORT)
    second = parse_skyline_csv(REPORT)
    assert [t.target_id for t in first] == [t.target_id for t in second]
    assert all(t.target_id for t in first)


def test_column_alias_normalisation(tmp_path: Path) -> None:
    variants = [
        "Peptide Sequence,PrecursorMz,RT,total_area,Max Height,fwhm,Mass Error PPM\n",
        "peptidesequence,precursor_mz,retention_time,TotalArea,maxheight,Max Fwhm,ppm error\n",
        "Peptide,Precursor M/Z,Peptide Retention Time,Sum Area,Peak Height,Peak Width FWHM,averagemasserrorppm\n",
    ]
    body = "PEPTIDEK,500.25,15.0,1000.0,100.0,0.2,1.5\n"
    for i, header in enumerate(variants):
        path = tmp_path / f"variant_{i}.csv"
        path.write_text(header + body)
        rows = parse_skyline_csv(path)
        assert len(rows) == 1
        r = rows[0]
        assert r.peptide_sequence == "PEPTIDEK"
        assert r.precursor_mz == pytest.approx(500.25)
        assert r.retention_time == pytest.approx(15.0)
        assert r.peak_area == pytest.approx(1000.0)
        assert r.peak_height == pytest.approx(100.0)
        assert r.peak_width_fwhm == pytest.approx(0.2)
        assert r.mass_error_ppm == pytest.approx(1.5)


def test_rt_delta_auto_computed(tmp_path: Path) -> None:
    path = tmp_path / "rt_delta.csv"
    path.write_text(
        "Peptide Sequence,Retention Time,Expected RT\n"
        "PEPTIDEA,10.0,9.5\n"
        "PEPTIDEB,20.0,21.0\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].rt_delta == pytest.approx(0.5)
    assert rows[1].rt_delta == pytest.approx(-1.0)


def test_explicit_rt_delta_wins_over_computed(tmp_path: Path) -> None:
    path = tmp_path / "rt_delta_explicit.csv"
    path.write_text(
        "Peptide,Retention Time,Expected RT,RT Delta\n"
        "PEP,10.0,9.0,0.25\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].rt_delta == pytest.approx(0.25)


def test_extra_metrics_passthrough_numeric(tmp_path: Path) -> None:
    path = tmp_path / "extra.csv"
    path.write_text(
        "Peptide,Precursor Mz,Total Area,My Custom Score\n"
        "PEP,500.0,1000.0,0.42\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].extra_metrics == {"My Custom Score": pytest.approx(0.42)}


def test_extra_non_numeric_dropped_silently(tmp_path: Path) -> None:
    path = tmp_path / "extra_nonnumeric.csv"
    path.write_text(
        "Peptide,Total Area,Replicate Name,Some Tag\n"
        "PEP,1000.0,Run01,annotation\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].extra_metrics == {}


def test_fixture_extras_drop_non_numeric() -> None:
    rows = parse_skyline_csv(REPORT)
    third = rows[3]
    assert "Custom Score" not in third.extra_metrics
    first = rows[0]
    assert first.extra_metrics.get("Custom Score") == pytest.approx(0.77)
    for r in rows:
        assert "Replicate Name" not in r.extra_metrics


def test_split_total_area_sums_ms1_and_fragment(tmp_path: Path) -> None:
    """DIA reports (Evosep / Astral) split intensity into 'Total Area MS1'
    + 'Total Area Fragment' instead of providing a single 'Total Area'.
    The parser must sum these into peak_area so `detected` is computed
    correctly — without this, a working DIA QC report shows 0/N recovery."""
    path = tmp_path / "dia_split.csv"
    path.write_text(
        "Peptide Sequence,Mz,Peptide Retention Time,Total Area MS1,Total Area Fragment,Mass Error PPM\n"
        "PEPTIDEK,500.0,12.3,1000000,2500000,1.4\n"
    )
    rows = parse_skyline_csv(path)
    assert len(rows) == 1
    assert rows[0].peak_area == pytest.approx(3500000.0)
    assert rows[0].detected is True


def test_split_total_area_with_only_fragment(tmp_path: Path) -> None:
    """If only one of MS1 / Fragment is present, use that value alone."""
    path = tmp_path / "dia_fragment_only.csv"
    path.write_text(
        "Peptide,Mz,RT,Total Area Fragment\n"
        "PEP,500.0,12.0,7500000\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].peak_area == pytest.approx(7500000.0)
    assert rows[0].detected is True


def test_explicit_total_area_wins_over_split_columns(tmp_path: Path) -> None:
    """If a row has both 'Total Area' and the split columns, the canonical
    'Total Area' wins (matches what Skyline does in the GUI summary)."""
    path = tmp_path / "both.csv"
    path.write_text(
        "Peptide,Mz,RT,Total Area,Total Area MS1,Total Area Fragment\n"
        "PEP,500.0,12.0,9999,1000,2000\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].peak_area == pytest.approx(9999.0)


def test_split_columns_zero_areas_still_yield_zero_peak_area(tmp_path: Path) -> None:
    """Both MS1 and Fragment present and zero → peak_area=0, not detected."""
    path = tmp_path / "zero.csv"
    path.write_text(
        "Peptide,Mz,Total Area MS1,Total Area Fragment\n"
        "PEP,500.0,0,0\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].peak_area == 0.0
    assert rows[0].detected is False


def test_empty_csv_returns_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("")
    assert parse_skyline_csv(path) == []


def test_headers_only_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "headers_only.csv"
    path.write_text("Peptide Sequence,Total Area\n")
    assert parse_skyline_csv(path) == []


def test_undetected_when_zero_area(tmp_path: Path) -> None:
    path = tmp_path / "no_area.csv"
    path.write_text(
        "Peptide,Total Area\n"
        "PEP,0\n"
        "PEP2,\n"
    )
    rows = parse_skyline_csv(path)
    assert rows[0].detected is False
    assert rows[1].detected is False
    assert rows[1].peak_area is None
