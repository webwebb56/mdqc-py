"""Tests for Extractor.extract() exit-code / success-detection logic.

Skyline 26.x exits with code 2 even on success.  The authoritative signal is
whether the output CSV was produced, not the process exit code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mdqc.config.schema import SkylineConfig
from mdqc.extractor import Extractor
from mdqc.extractor.skyline import SkylineRunResult
from mdqc.types import ExtractionStatus

_MINIMAL_CSV = "Peptide Sequence,Precursor Mz,Best Retention Time\nPEPTIDE,500.0,12.3\n"

_SKYLINE_26_STDOUT = (
    "Success! Imported Reports from MD_QC_Report.skyr\r\n"
    "Opening file...\r\n"
    "File template.sky opened.\r\n"
    "Reading retention times from lib.blib\r\n"
    "100%\r\n"
    "Adding results...\r\n"
    "1. C:\\Users\\StoyanStoychev\\Downloads\\WorkTempFiles\\P087\r\n"
)


def _make_extractor(tmp_path: Path, skyline_exe: Path) -> Extractor:
    cfg = SkylineConfig(path=str(skyline_exe), timeout_seconds=30)
    return Extractor(cfg, work_dir=tmp_path / "work")


def _fake_result(returncode: int, stdout: str = "", stderr: str = "") -> SkylineRunResult:
    return SkylineRunResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1234,
        version="26.1.0.057",
    )


async def _run_and_write_csv(output_csv: Path, returncode: int, stdout: str) -> SkylineRunResult:
    """Side-effect helper: writes the CSV then returns the mocked result."""
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_csv.write_text(_MINIMAL_CSV)
    return _fake_result(returncode, stdout)


@pytest.fixture()
def fake_skyline_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "SkylineCmd.exe"
    exe.write_text("fake")
    return exe


@pytest.fixture()
def template(tmp_path: Path) -> Path:
    t = tmp_path / "template.sky"
    t.write_text("template")
    return t


@pytest.fixture()
def raw_file(tmp_path: Path) -> Path:
    r = tmp_path / "sample.raw"
    r.write_text("raw")
    return r


# ---------------------------------------------------------------------------
# exit code 2 with CSV written  →  should succeed (Skyline 26.x behaviour)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_exitcode2_with_csv_succeeds(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    async def side_effect(**kwargs):
        output_csv: Path = kwargs["output_csv"]
        return await _run_and_write_csv(output_csv, returncode=2, stdout=_SKYLINE_26_STDOUT)

    with patch(
        "mdqc.extractor.run_skyline",
        new=AsyncMock(side_effect=side_effect),
    ):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.status == ExtractionStatus.SUCCESS, result.error_message
    assert result.target_metrics is not None
    assert len(result.target_metrics) == 1
    assert result.target_metrics[0].peptide_sequence == "PEPTIDE"


@pytest.mark.asyncio
async def test_extract_exitcode2_with_csv_records_version(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    async def side_effect(**kwargs):
        output_csv: Path = kwargs["output_csv"]
        return await _run_and_write_csv(output_csv, returncode=2, stdout=_SKYLINE_26_STDOUT)

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.backend_version == "26.1.0.057"


# ---------------------------------------------------------------------------
# exit code 0 with CSV  →  normal success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_exitcode0_with_csv_succeeds(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    async def side_effect(**kwargs):
        return await _run_and_write_csv(kwargs["output_csv"], returncode=0, stdout="Skyline 24.1")

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.status == ExtractionStatus.SUCCESS


# ---------------------------------------------------------------------------
# exit code 1 with NO CSV and error marker  →  should fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_exitcode1_no_csv_fails(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    with patch(
        "mdqc.extractor.run_skyline",
        new=AsyncMock(return_value=_fake_result(1, stderr="Error: import failed")),
    ):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.status == ExtractionStatus.FAILED
    assert "1" in result.error_message  # returncode in message


# ---------------------------------------------------------------------------
# exit code 2 with CSV but an explicit error marker  →  should fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_exitcode2_error_marker_fails_even_with_csv(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    async def side_effect(**kwargs):
        output_csv: Path = kwargs["output_csv"]
        return await _run_and_write_csv(
            output_csv,
            returncode=2,
            stdout="Error: memory corruption detected",
        )

    with patch("mdqc.extractor.run_skyline", new=AsyncMock(side_effect=side_effect)):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.status == ExtractionStatus.FAILED


# ---------------------------------------------------------------------------
# exit code 0 with NO CSV and no error  →  should fail with clear message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_exitcode0_no_csv_fails(
    tmp_path: Path, fake_skyline_exe: Path, template: Path, raw_file: Path
) -> None:
    extractor = _make_extractor(tmp_path, fake_skyline_exe)

    with patch(
        "mdqc.extractor.run_skyline",
        new=AsyncMock(return_value=_fake_result(0, stdout="Skyline 24.1")),
    ):
        result = await extractor.extract(template=template, raw_file=raw_file)

    assert result.status == ExtractionStatus.FAILED
    assert "no report file" in result.error_message.lower()
