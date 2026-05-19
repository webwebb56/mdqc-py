"""Shared enums and dataclasses used across the agent.

Serialization formats here are part of the payload schema contract — see
docs/AGENT_NOTES § Cross-cutting principles. Do not change values without
coordinating with the cloud ingest team.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


class ControlType(StrEnum):
    SSC0 = "SSC0"
    QC_A = "QC_A"
    QC_B = "QC_B"
    BLANK = "BLANK"
    SAMPLE = "SAMPLE"

    def is_qc(self) -> bool:
        return self is not ControlType.SAMPLE


class Vendor(StrEnum):
    THERMO = "thermo"
    BRUKER = "bruker"
    SCIEX = "sciex"
    WATERS = "waters"
    AGILENT = "agilent"

    @property
    def is_directory_artifact(self) -> bool:
        """Bruker .d, Waters .raw, Agilent .d are directories. Thermo .raw, Sciex .wiff are files."""
        return self in (Vendor.BRUKER, Vendor.WATERS, Vendor.AGILENT)


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ClassificationSource(StrEnum):
    FILENAME = "FILENAME"
    METADATA = "METADATA"
    POSITION = "POSITION"
    DEFAULT = "DEFAULT"


class FinalizationState(StrEnum):
    DETECTED = "DETECTED"
    STABILIZING = "STABILIZING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class ExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class WellPosition:
    """Plate well like A1, A12, H8.

    Row is A-H, column is 1-12. Constructor returns None for out-of-range values.
    """

    row: str
    column: int

    @classmethod
    def parse(cls, raw: str) -> WellPosition | None:
        """Lenient parser: accepts a1, A1, A01."""
        if not raw or len(raw) < 2:
            return None
        row = raw[0].upper()
        try:
            col = int(raw[1:])
        except ValueError:
            return None
        if row not in "ABCDEFGH" or not 1 <= col <= 12:
            return None
        return cls(row=row, column=col)

    def __str__(self) -> str:
        return f"{self.row}{self.column}"


@dataclass
class RunClassification:
    control_type: ControlType
    well_position: WellPosition | None
    instrument_id: str | None
    plate_id: str | None
    confidence: Confidence
    source: ClassificationSource
    # SPD ("samples per day") — Evosep chromatography speed setting parsed
    # from filename markers like `200spd`, `500SPD`, etc. ``None`` if not
    # detectable from the filename. Surfaced as an orthogonal dashboard
    # filter (independent of control_type).
    spd: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_type": self.control_type.value,
            "well_position": str(self.well_position) if self.well_position else None,
            "instrument_id": self.instrument_id,
            "plate_id": self.plate_id,
            "classification_confidence": self.confidence.value,
            "classification_source": self.source.value,
            "spd": self.spd,
        }


@dataclass
class TargetMetric:
    """Per-peptide metric row from a Skyline report."""

    target_id: str
    peptide_sequence: str | None = None
    precursor_mz: float | None = None
    retention_time: float | None = None
    rt_expected: float | None = None
    rt_delta: float | None = None
    peak_area: float | None = None
    peak_height: float | None = None
    peak_width_fwhm: float | None = None
    peak_symmetry: float | None = None
    mass_error_ppm: float | None = None
    isotope_dot_product: float | None = None
    library_dot_product: float | None = None
    detected: bool = True
    extra_metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class RunMetrics:
    """Aggregated run-level metrics."""

    targets_found: int
    targets_expected: int
    target_recovery_pct: float
    median_rt_shift: float | None = None
    median_mass_error_ppm: float | None = None
    chromatography_score: float | None = None


@dataclass
class ExtractionResult:
    """Output of running Skyline against a single raw file."""

    run_id: UUID = field(default_factory=uuid4)
    raw_file_path: Path | None = None
    raw_file_hash: str | None = None
    template_path: Path | None = None
    template_hash: str | None = None
    backend: str = "skyline"
    backend_version: str | None = None
    extraction_time_ms: int = 0
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    error_message: str | None = None
    target_metrics: list[TargetMetric] = field(default_factory=list)
    run_metrics: RunMetrics | None = None
    stdout: str | None = None
    stderr: str | None = None


@dataclass
class QcPayload:
    """The JSON document uploaded to the cloud, written to spool.

    Schema is part of the cross-language contract — match Rust field names exactly.
    See SPEC.md § 18 in the Rust repo.
    """

    schema_version: str
    payload_id: UUID
    correlation_id: str
    agent_id: str
    agent_version: str
    timestamp: datetime
    run: dict[str, Any]
    extraction: dict[str, Any]
    target_metrics: list[dict[str, Any]]
    run_metrics: dict[str, Any]
    baseline_context: dict[str, Any] | None = None
    comparison_metrics: dict[str, Any] | None = None
