from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mdqc.config import paths
from mdqc.config.defaults import MAX_PENDING_MB, PAYLOAD_SCHEMA_VERSION
from mdqc.types import ExtractionResult, QcPayload, RunClassification, Vendor

log = logging.getLogger(__name__)


class SpoolError(Exception):
    pass


class SpoolFull(SpoolError):
    def __init__(self, current_mb: float, limit_mb: int) -> None:
        super().__init__(
            f"Spool pending dir is full: {current_mb:.1f} MB >= {limit_mb} MB limit"
        )
        self.current_mb = current_mb
        self.limit_mb = limit_mb


def _json_default(obj: Any) -> Any:
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _payload_to_dict(payload: QcPayload) -> dict[str, Any]:
    return {
        "schema_version": payload.schema_version,
        "payload_id": str(payload.payload_id),
        "correlation_id": payload.correlation_id,
        "agent_id": payload.agent_id,
        "agent_version": payload.agent_version,
        "timestamp": payload.timestamp.isoformat(),
        "run": payload.run,
        "extraction": payload.extraction,
        "target_metrics": payload.target_metrics,
        "run_metrics": payload.run_metrics,
        "baseline_context": payload.baseline_context,
        "comparison_metrics": payload.comparison_metrics,
    }


def _target_metric_to_dict(tm: Any) -> dict[str, Any]:
    if isinstance(tm, dict):
        return tm
    return asdict(tm)


def _run_metrics_to_dict(rm: Any) -> dict[str, Any] | None:
    if rm is None:
        return None
    if isinstance(rm, dict):
        return rm
    return asdict(rm)


def _file_acquisition_time(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _vendor_from_extension(path: Path | None) -> str | None:
    if path is None:
        return None
    suffix = path.suffix.lower()
    if suffix == ".raw":
        if path.is_dir():
            return Vendor.WATERS.value
        return Vendor.THERMO.value
    if suffix == ".d":
        return Vendor.BRUKER.value
    if suffix == ".wiff":
        return Vendor.SCIEX.value
    return None


class Spool:
    def __init__(
        self,
        agent_id: str,
        agent_version: str,
        root: Path | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_version = agent_version
        self.root = root if root is not None else paths.spool_dir()
        self.pending_dir = self.root / "pending"
        self.uploading_dir = self.root / "uploading"
        self.completed_dir = self.root / "completed"
        self.failed_dir = self.root / "failed"
        for d in (
            self.root,
            self.pending_dir,
            self.uploading_dir,
            self.completed_dir,
            self.failed_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def correlation_id_for(self, run_id: UUID) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        rand_hex = secrets.token_hex(4)
        return f"{self.agent_id}-{timestamp}-{rand_hex}"

    def pending_size_mb(self) -> float:
        return _dir_size_bytes(self.pending_dir) / (1024 * 1024)

    def pending_count(self) -> int:
        if not self.pending_dir.exists():
            return 0
        return sum(1 for p in self.pending_dir.iterdir() if p.is_file() and p.suffix == ".json")

    def enqueue(
        self,
        classification: RunClassification,
        extraction: ExtractionResult,
        baseline_context: dict[str, Any] | None = None,
        max_pending_mb: int = MAX_PENDING_MB,
    ) -> Path:
        size_mb = self.pending_size_mb()
        if size_mb >= max_pending_mb:
            raise SpoolFull(size_mb, max_pending_mb)

        run_id = extraction.run_id
        correlation_id = self.correlation_id_for(run_id)

        raw_file_path = extraction.raw_file_path
        template_path = extraction.template_path

        run_info: dict[str, Any] = {
            "run_id": str(run_id),
            "raw_file_name": raw_file_path.name if raw_file_path else None,
            "raw_file_hash": extraction.raw_file_hash,
            # Prefer the real acquisition time Skyline read from the raw-file
            # header; fall back to the file mtime when the report has no
            # AcquiredTime column (older .skyr).
            "acquisition_time": (
                extraction.acquired_time or _file_acquisition_time(raw_file_path)
            ),
            "instrument_id": classification.instrument_id,
            "vendor": _vendor_from_extension(raw_file_path),
            "control_type": classification.control_type.value,
            "well_position": (
                str(classification.well_position) if classification.well_position else None
            ),
            "plate_id": classification.plate_id,
            "spd": classification.spd,
            "dilution_pct": classification.dilution_pct,
            "classification_confidence": classification.confidence.value,
            "classification_source": classification.source.value,
            "method_name": None,
            "column_info": None,
            # Underscore-prefixed = Python-only debug metadata, not part of cross-language schema.
            "_raw_file_path": str(raw_file_path) if raw_file_path else None,
        }

        extraction_info: dict[str, Any] = {
            "backend": extraction.backend,
            "backend_version": extraction.backend_version,
            "template_name": template_path.name if template_path else None,
            "template_hash": extraction.template_hash,
            "extraction_time_ms": extraction.extraction_time_ms,
            "status": extraction.status.value,
            "error_message": extraction.error_message,
            # Underscore-prefixed = Python-only debug metadata, not part of cross-language schema.
            "_template_path": str(template_path) if template_path else None,
        }

        target_metrics = [_target_metric_to_dict(tm) for tm in extraction.target_metrics]
        run_metrics = _run_metrics_to_dict(extraction.run_metrics) or {}

        payload = QcPayload(
            schema_version=PAYLOAD_SCHEMA_VERSION,
            payload_id=uuid4(),
            correlation_id=correlation_id,
            agent_id=self.agent_id,
            agent_version=self.agent_version,
            timestamp=datetime.now(UTC),
            run=run_info,
            extraction=extraction_info,
            target_metrics=target_metrics,
            run_metrics=run_metrics,
            baseline_context=baseline_context,
            comparison_metrics=None,
        )

        filename = f"{run_id}_payload.json"
        final_path = self.pending_dir / filename
        tmp_path = self.pending_dir / f".{filename}.tmp"

        json_bytes = json.dumps(_payload_to_dict(payload), indent=2).encode("utf-8")
        with open(tmp_path, "wb") as f:
            f.write(json_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)

        log.info(
            "Payload spooled",
            extra={
                "run_id": str(run_id),
                "correlation_id": correlation_id,
                "path": str(final_path),
            },
        )
        return final_path

    def claim_next(self) -> tuple[Path, dict[str, Any]] | None:
        candidates: list[tuple[float, Path]] = []
        for p in self.pending_dir.iterdir():
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, p))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        _, src = candidates[0]
        dst = self.uploading_dir / src.name
        os.replace(src, dst)
        with open(dst, "rb") as f:
            payload = json.loads(f.read().decode("utf-8"))
        return dst, payload

    def mark_completed(self, uploading_path: Path) -> None:
        dst = self.completed_dir / uploading_path.name
        os.replace(uploading_path, dst)

    def mark_failed(self, uploading_path: Path, reason: str) -> None:
        sidecar = uploading_path.with_name(uploading_path.stem + "_failure.json")
        sidecar_data = {
            "reason": reason,
            "failed_at": datetime.now(UTC).isoformat(),
            "payload_filename": uploading_path.name,
        }
        sidecar_tmp = sidecar.with_name(f".{sidecar.name}.tmp")
        with open(sidecar_tmp, "wb") as f:
            f.write(json.dumps(sidecar_data, indent=2).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(sidecar_tmp, sidecar)

        dst_payload = self.failed_dir / uploading_path.name
        os.replace(uploading_path, dst_payload)
        if sidecar.exists():
            dst_sidecar = self.failed_dir / sidecar.name
            os.replace(sidecar, dst_sidecar)

    def recover_uploading_to_pending(self) -> int:
        if not self.uploading_dir.exists():
            return 0
        count = 0
        for p in self.uploading_dir.iterdir():
            if not p.is_file():
                continue
            dst = self.pending_dir / p.name
            try:
                os.replace(p, dst)
                count += 1
            except OSError as e:
                log.error(
                    "Failed to recover uploading payload",
                    extra={"path": str(p), "error": str(e)},
                )
        return count

    def write_manifest(
        self,
        template_name: str,
        instrument_id: str,
        target_ids: list[str],
        known_metrics: list[str],
        extra_metrics: list[str],
    ) -> None:
        manifest = {
            "template_name": template_name,
            "instrument_id": instrument_id,
            "target_ids": list(target_ids),
            "known_metrics": list(known_metrics),
            "extra_metrics": list(extra_metrics),
            "last_updated": datetime.now(UTC).isoformat(),
        }
        manifest_path = self.root / "manifest.json"
        tmp_path = self.root / ".manifest.json.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(json.dumps(manifest, indent=2).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, manifest_path)
        except OSError as e:
            log.warning(
                "Failed to write spool manifest",
                extra={"path": str(manifest_path), "error": str(e)},
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.iterdir():
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total
