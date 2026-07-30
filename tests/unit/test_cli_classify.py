"""Tests for `mdqc classify` — the filename-convention preview.

The SOP (docs/sop, §A5) instructs a commissioning engineer to verify a site's
naming convention with this command before relying on it, so its output has to
carry every field that classification actually drives.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mdqc.cli import app

runner = CliRunner()


def _run(name: str) -> str:
    result = runner.invoke(app, ["classify", name])
    assert result.exit_code == 0, result.output
    return result.output


def test_classify_reports_control_type_and_spd() -> None:
    out = _run("SSC0_2026-08-04_ss_50ng_200spd_k562_S1-B1_53131.d")
    assert "Control Type: SSC0" in out
    assert "SPD: 200" in out
    assert "Well Position: B1" in out
    assert "Confidence: HIGH" in out


def test_classify_reports_dilution() -> None:
    out = _run("QCB_75perc_2026-07-24_ss_50ng_200spd_k562_S1-F8_53137.d")
    assert "Control Type: QC_B" in out
    assert "Dilution: 75%" in out
    assert "SPD: 200" in out


def test_classify_shows_dash_when_markers_absent() -> None:
    """A neat control at an unstated SPD must read as absent, not as zero."""
    out = _run("QCA_2026-07-24_k562_A1.raw")
    assert "SPD: -" in out
    assert "Dilution: -" in out


def test_classify_flags_an_unrecognised_name_as_a_sample() -> None:
    """The silent-skip case the SOP warns about: no control-type marker."""
    out = _run("mystudy_batch7_injection12.raw")
    assert "Control Type: SAMPLE" in out


def test_classify_accepts_a_path() -> None:
    out = _run(str(Path("D:/Data/Astral/QC/SSC0_200spd_A1.raw")))
    assert "Control Type: SSC0" in out
