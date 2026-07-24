"""Local gold-standard (SSC0) baseline storage and computation.

Agent-local, per-(instrument, SPD) baselines used to express QC_A/QC_B runs
as ratios against a "local optimum" rather than absolute values — see
docs/PLAN_2026-07-24.md §5.2 (decided: the agent, not the platform, owns
baseline recording).

This module owns two durable, JSON-backed stores under
``paths.gold_standards_dir()``:

- ``ssc0_runs.json`` — a per-(instrument, SPD) index of SSC0 run snapshots
  (peptide-level RT + peak area). This is intentionally separate from
  spool/completed, which only retains the newest
  ``COMPLETED_RETENTION_COUNT`` (10) payloads — SSC0 runs recorded at
  install (15-20 of them) would otherwise be pruned away before an
  engineer gets to the Gold Standards page to review them.
- ``baselines.json`` — versioned baseline records computed from an
  operator-selected subset of those runs, with an active-baseline pointer
  per (instrument, SPD).

Not yet wired into the extraction pipeline: ``QcPayload.baseline_context`` /
``comparison_metrics`` stay null. That wiring needs the ratio set and
re-baseline semantics confirmed first (see PLAN §7 open decisions).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mdqc.config import paths
from mdqc.peptide_classes import filter_for_baseline
from mdqc.types import ControlType, ExtractionResult, RunClassification, TargetMetric

if TYPE_CHECKING:
    from mdqc.config.schema import PeptideClassRule

log = logging.getLogger(__name__)

_MAX_RUNS_PER_BUCKET = 300
_UNKNOWN = "unknown"


def _bucket_key(instrument_id: str | None, spd: int | None) -> str:
    inst = instrument_id or _UNKNOWN
    spd_part = str(spd) if spd is not None else _UNKNOWN
    return f"{inst}::{spd_part}"


def _peptide_key(m: TargetMetric) -> str:
    return f"{m.protein_name or ''}|{m.peptide_sequence or m.target_id}"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("gold_standards_read_failed", extra={"path": str(path), "error": str(exc)})
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def record_ssc0_run(classification: RunClassification, extraction: ExtractionResult) -> None:
    """Append a snapshot of an SSC0 run to the durable per-(instrument, SPD) index.

    Best-effort: never raises. Called after the run has already been
    successfully classified, extracted, and spooled — an indexing hiccup
    here must not turn an otherwise-successful run into a failure.
    """
    if classification.control_type is not ControlType.SSC0:
        return
    try:
        _record_ssc0_run_inner(classification, extraction)
    except Exception as exc:  # best-effort side index, see docstring
        log.warning("gold_standards_record_failed", extra={"error": str(exc)})


def _record_ssc0_run_inner(
    classification: RunClassification, extraction: ExtractionResult
) -> None:
    peptides: dict[str, Any] = {}
    for m in extraction.target_metrics:
        peptides[_peptide_key(m)] = {
            "protein_name": m.protein_name,
            "peptide_sequence": m.peptide_sequence,
            "peptide_class": m.peptide_class,
            "peptide_class_purpose": m.peptide_class_purpose,
            "retention_time": m.retention_time,
            "peak_area": m.peak_area,
        }

    run_metrics = extraction.run_metrics
    snapshot = {
        "run_id": str(extraction.run_id),
        "instrument_id": classification.instrument_id,
        "spd": classification.spd,
        "raw_file_name": extraction.raw_file_path.name if extraction.raw_file_path else None,
        "acquisition_time": extraction.acquired_time,
        "recorded_at": datetime.now(UTC).isoformat(),
        "targets_found": run_metrics.targets_found if run_metrics else None,
        "targets_expected": run_metrics.targets_expected if run_metrics else None,
        "peptides": peptides,
    }

    path = paths.ssc0_runs_path()
    data = _read_json(path)
    key = _bucket_key(classification.instrument_id, classification.spd)
    bucket: list[dict[str, Any]] = data.get(key, [])
    bucket.append(snapshot)
    if len(bucket) > _MAX_RUNS_PER_BUCKET:
        bucket = bucket[-_MAX_RUNS_PER_BUCKET:]
    data[key] = bucket
    _write_json(path, data)


def list_ssc0_runs(instrument_id: str | None, spd: int | None) -> list[dict[str, Any]]:
    """Oldest-first list of recorded SSC0 run snapshots for (instrument, spd)."""
    data = _read_json(paths.ssc0_runs_path())
    return list(data.get(_bucket_key(instrument_id, spd), []))


def list_available_spds(instrument_id: str | None) -> list[int]:
    """Distinct SPD values with at least one recorded SSC0 run, sorted ascending.

    SPD-less runs (spd could not be parsed from the filename) are not
    represented here — see ``list_ssc0_runs(instrument_id, None)`` for those.
    """
    data = _read_json(paths.ssc0_runs_path())
    prefix = f"{instrument_id or _UNKNOWN}::"
    spds: list[int] = []
    for key, runs in data.items():
        if not key.startswith(prefix) or not runs:
            continue
        spd_part = key[len(prefix) :]
        if spd_part.isdigit():
            spds.append(int(spd_part))
    return sorted(spds)


@dataclass
class BaselinePeptideStat:
    protein_name: str | None
    peptide_sequence: str | None
    peptide_class: str | None
    peptide_class_purpose: str | None
    peak_area_median: float | None
    peak_area_sd: float | None
    peak_area_cv_pct: float | None
    retention_time_median: float | None
    retention_time_sd: float | None
    n: int


def _sd(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    return statistics.pstdev(values)


def _cv_pct(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return None
    return statistics.pstdev(values) / mean * 100.0


def compute_baseline_preview(
    runs: list[dict[str, Any]],
    checked_run_ids: set[str],
    rules: list[PeptideClassRule] | None = None,
) -> dict[str, BaselinePeptideStat]:
    """Compute per-peptide median/CV/RT-sd stats from the checked subset of runs.

    Pure function, no I/O — the server-side source of truth for what a save
    would persist, and reusable for a first-paint preview before client JS
    takes over live recompute. Peptides whose class has
    ``exclude_from_baseline`` set are dropped entirely.
    """
    selected = [r for r in runs if r["run_id"] in checked_run_ids]

    all_peps: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, list[float]]] = {}
    for run in selected:
        for key, pep in run.get("peptides", {}).items():
            all_peps.setdefault(key, pep)
            slot = values.setdefault(key, {"area": [], "rt": []})
            if pep.get("peak_area") is not None:
                slot["area"].append(float(pep["peak_area"]))
            if pep.get("retention_time") is not None:
                slot["rt"].append(float(pep["retention_time"]))

    if rules:
        stand_ins = []
        for key, pep in all_peps.items():
            tm = TargetMetric(
                target_id=key,
                peptide_sequence=pep.get("peptide_sequence"),
                protein_name=pep.get("protein_name"),
            )
            tm.peptide_class = pep.get("peptide_class")
            stand_ins.append(tm)
        kept_keys = {m.target_id for m in filter_for_baseline(stand_ins, rules)}
    else:
        kept_keys = set(all_peps.keys())

    result: dict[str, BaselinePeptideStat] = {}
    for key in kept_keys:
        pep = all_peps[key]
        areas = values[key]["area"]
        rts = values[key]["rt"]
        result[key] = BaselinePeptideStat(
            protein_name=pep.get("protein_name"),
            peptide_sequence=pep.get("peptide_sequence"),
            peptide_class=pep.get("peptide_class"),
            peptide_class_purpose=pep.get("peptide_class_purpose"),
            peak_area_median=statistics.median(areas) if areas else None,
            peak_area_sd=_sd(areas),
            peak_area_cv_pct=_cv_pct(areas),
            retention_time_median=statistics.median(rts) if rts else None,
            retention_time_sd=_sd(rts),
            n=len(areas),
        )
    return result


def save_baseline(
    instrument_id: str | None,
    spd: int | None,
    run_ids: list[str],
    label: str,
    rules: list[PeptideClassRule] | None = None,
) -> dict[str, Any]:
    """Compute + persist a new versioned baseline from the given run ids.

    Writes the record, keeps it alongside prior versions, and flips the
    active pointer for this (instrument, spd) to the new baseline_id.
    """
    runs = list_ssc0_runs(instrument_id, spd)
    checked = set(run_ids)
    stats = compute_baseline_preview(runs, checked, rules)

    record = {
        "baseline_id": str(uuid4()),
        "instrument_id": instrument_id,
        "spd": spd,
        "label": label.strip() or f"Baseline {datetime.now(UTC).strftime('%Y-%m-%d')}",
        "created_at": datetime.now(UTC).isoformat(),
        "source_run_ids": sorted(checked),
        "per_peptide": {k: asdict(v) for k, v in stats.items()},
    }

    path = paths.baselines_path()
    data = _read_json(path)
    key = _bucket_key(instrument_id, spd)
    entry = data.get(key) or {"active_baseline_id": None, "baselines": {}}
    entry["baselines"][record["baseline_id"]] = record
    entry["active_baseline_id"] = record["baseline_id"]
    data[key] = entry
    _write_json(path, data)

    log.info(
        "gold_standard_baseline_saved",
        extra={
            "instrument_id": instrument_id,
            "spd": spd,
            "baseline_id": record["baseline_id"],
            "n_runs": len(checked),
        },
    )
    return record


def list_baselines(instrument_id: str | None, spd: int | None) -> list[dict[str, Any]]:
    """All versioned baseline records for (instrument, spd), newest first."""
    data = _read_json(paths.baselines_path())
    entry = data.get(_bucket_key(instrument_id, spd))
    if not entry:
        return []
    records: list[dict[str, Any]] = list(entry.get("baselines", {}).values())
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records


def get_active_baseline(instrument_id: str | None, spd: int | None) -> dict[str, Any] | None:
    data = _read_json(paths.baselines_path())
    entry = data.get(_bucket_key(instrument_id, spd))
    if not entry or not entry.get("active_baseline_id"):
        return None
    baseline: dict[str, Any] | None = entry["baselines"].get(entry["active_baseline_id"])
    return baseline


__all__ = [
    "BaselinePeptideStat",
    "compute_baseline_preview",
    "get_active_baseline",
    "list_available_spds",
    "list_baselines",
    "list_ssc0_runs",
    "record_ssc0_run",
    "save_baseline",
]
