from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from mdqc.spool import Spool
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


def _make_classification(instrument_id: str | None = "EXPLORIS01") -> RunClassification:
    return RunClassification(
        control_type=ControlType.QC_A,
        well_position=WellPosition(row="A", column=1),
        instrument_id=instrument_id,
        plate_id=None,
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
    )


def _make_extraction(raw_file: Path, template: Path) -> ExtractionResult:
    targets = [
        TargetMetric(
            target_id=f"T{i}",
            peptide_sequence=f"PEPTIDEK{i}",
            precursor_mz=500.0 + i,
            retention_time=10.0 + i,
            rt_expected=10.0 + i,
            rt_delta=0.0,
            peak_area=1000.0 * (i + 1),
            detected=True,
        )
        for i in range(3)
    ]
    return ExtractionResult(
        run_id=uuid4(),
        raw_file_path=raw_file,
        template_path=template,
        backend="skyline",
        backend_version="24.1.0.198",
        extraction_time_ms=12345,
        status=ExtractionStatus.SUCCESS,
        target_metrics=targets,
        run_metrics=RunMetrics(
            targets_found=3,
            targets_expected=3,
            target_recovery_pct=100.0,
        ),
        template_hash="t" * 64,
        raw_file_hash="r" * 64,
    )


@pytest.fixture
def thermo_raw(tmp_path: Path) -> Path:
    raw = tmp_path / "EXPLORIS01_QCA_A1_2026-01-27.raw"
    raw.write_bytes(b"x" * 4096)
    return raw


@pytest.fixture
def template_file(tmp_path: Path) -> Path:
    template = tmp_path / "QC_Method.sky"
    template.write_bytes(b"<fake-template/>")
    return template


def test_payload_schema_version_is_1_1(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    classification = _make_classification()
    extraction = _make_extraction(thermo_raw, template_file)

    spool_path = spool.enqueue(classification, extraction)
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.1"


def test_payload_run_uses_raw_file_name_not_path(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["run"]["raw_file_name"] == "EXPLORIS01_QCA_A1_2026-01-27.raw"


def test_payload_extraction_uses_template_name_not_path(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["extraction"]["template_name"] == "QC_Method.sky"


def test_payload_run_vendor_thermo_for_raw_file(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["run"]["vendor"] == "thermo"


def test_payload_run_vendor_waters_for_raw_directory(
    tmp_data_dir: Path, tmp_path: Path, template_file: Path
) -> None:
    raw_dir = tmp_path / "WATERS01_QCA_A1.raw"
    raw_dir.mkdir()
    (raw_dir / "_FUNC001.DAT").write_bytes(b"data")

    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(raw_dir, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["run"]["vendor"] == "waters"


def test_payload_run_vendor_bruker_for_d_directory(
    tmp_data_dir: Path, tmp_path: Path, template_file: Path
) -> None:
    raw_dir = tmp_path / "TIMSTOF01_QCB_A3.d"
    raw_dir.mkdir()
    (raw_dir / "analysis.tdf").write_bytes(b"data")

    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(raw_dir, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert payload["run"]["vendor"] == "bruker"


def test_payload_run_acquisition_time_is_iso8601(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    acq = payload["run"]["acquisition_time"]
    assert isinstance(acq, str)
    # Must parse as ISO8601 without raising.
    from datetime import datetime as _dt

    parsed = _dt.fromisoformat(acq)
    assert parsed.tzinfo is not None


def test_payload_run_method_name_and_column_info_present_as_keys(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert "method_name" in payload["run"]
    assert "column_info" in payload["run"]
    assert payload["run"]["method_name"] is None
    assert payload["run"]["column_info"] is None


def test_payload_target_metrics_is_list(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert isinstance(payload["target_metrics"], list)
    assert len(payload["target_metrics"]) == 3


def test_payload_run_does_not_use_old_raw_file_path_field(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    # The Rust 1.1 schema does NOT include `raw_file_path` at the top level.
    # Python-only debug metadata is allowed but must use underscore prefix.
    assert "raw_file_path" not in payload["run"]
    assert "_raw_file_path" in payload["run"]


def test_payload_extraction_does_not_use_old_template_path_field(
    tmp_data_dir: Path, thermo_raw: Path, template_file: Path
) -> None:
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    spool_path = spool.enqueue(
        _make_classification(), _make_extraction(thermo_raw, template_file)
    )
    payload = json.loads(spool_path.read_text(encoding="utf-8"))

    assert "template_path" not in payload["extraction"]
    assert "_template_path" in payload["extraction"]
