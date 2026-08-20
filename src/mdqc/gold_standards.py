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
    from mdqc.config.schema import PeptideClassRule, QcThresholdsConfig

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
            # Dot products are part of the mis-extraction rule (a wrong peak
            # shows RT drift *and* depressed dot products), so the baseline
            # has to carry their reference values too.
            "isotope_dot_product": m.isotope_dot_product,
            "library_dot_product": m.library_dot_product,
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
    # None for baselines built from runs recorded before v0.5.8, which did
    # not store dot products. Consumers must treat these as optional.
    isotope_dot_product_median: float | None = None
    library_dot_product_median: float | None = None


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
            slot = values.setdefault(key, {"area": [], "rt": [], "idotp": [], "dotp": []})
            if pep.get("peak_area") is not None:
                slot["area"].append(float(pep["peak_area"]))
            if pep.get("retention_time") is not None:
                slot["rt"].append(float(pep["retention_time"]))
            if pep.get("isotope_dot_product") is not None:
                slot["idotp"].append(float(pep["isotope_dot_product"]))
            if pep.get("library_dot_product") is not None:
                slot["dotp"].append(float(pep["library_dot_product"]))

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
        idotps = values[key]["idotp"]
        dotps = values[key]["dotp"]
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
            isotope_dot_product_median=statistics.median(idotps) if idotps else None,
            library_dot_product_median=statistics.median(dotps) if dotps else None,
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


def _pct_deviation(value: float | None, reference: float | None) -> float | None:
    """Signed percentage deviation of ``value`` from ``reference``."""
    if value is None or reference is None or reference == 0:
        return None
    return (value - reference) / abs(reference) * 100.0


def compute_comparison_metrics(
    targets: list[TargetMetric],
    baseline: dict[str, Any],
    thresholds: QcThresholdsConfig | None = None,
) -> dict[str, Any]:
    """Express a run's per-peptide measurements against its SSC0 baseline.

    Implements Evosep's first-draft decision matrix (2026-07-28). The
    thresholds are configurable and provisional — see ``QcThresholdsConfig``.

    Both the raw measurements and any derived flag are emitted, so the
    platform can re-derive the judgement under different thresholds rather
    than having to trust the agent's. ``target_extraction_suspect`` is
    deliberately a conjunction: Evosep observed a wrongly-extracted peak at
    idotp 0.92, so a dot product alone does not identify one — it takes RT
    drift together with a dot-product or peak-area anomaly.
    """
    from mdqc.config.schema import QcThresholdsConfig as _T

    th = thresholds if thresholds is not None else _T()
    per_baseline: dict[str, Any] = baseline.get("per_peptide", {})

    per_peptide: dict[str, Any] = {}
    area_ratios: list[float] = []
    rt_devs: list[float] = []
    suspect_count = 0

    for m in targets:
        key = _peptide_key(m)
        ref = per_baseline.get(key)
        if ref is None:
            continue

        area_ref = ref.get("peak_area_median")
        rt_ref = ref.get("retention_time_median")

        area_ratio = (
            m.peak_area / area_ref
            if m.peak_area is not None and area_ref not in (None, 0)
            else None
        )
        area_dev = _pct_deviation(m.peak_area, area_ref)
        rt_delta = (
            m.retention_time - rt_ref
            if m.retention_time is not None and rt_ref is not None
            else None
        )
        rt_dev = _pct_deviation(m.retention_time, rt_ref)
        idotp_dev = _pct_deviation(
            m.isotope_dot_product, ref.get("isotope_dot_product_median")
        )
        dotp_dev = _pct_deviation(
            m.library_dot_product, ref.get("library_dot_product_median")
        )

        rt_off = rt_dev is not None and abs(rt_dev) > th.rt_deviation_pct_max
        dot_off = any(
            d is not None and abs(d) > th.dot_product_deviation_pct_suspect
            for d in (idotp_dev, dotp_dev)
        )
        area_off = (
            area_dev is not None
            and abs(area_dev) > th.peak_area_deviation_pct_suspect
        )
        suspect = bool(rt_off and (dot_off or area_off))
        if suspect:
            suspect_count += 1

        if area_ratio is not None:
            area_ratios.append(area_ratio)
        if rt_dev is not None:
            rt_devs.append(rt_dev)

        per_peptide[key] = {
            "peptide_sequence": m.peptide_sequence,
            "peak_area_ratio_to_baseline": area_ratio,
            "peak_area_deviation_pct": area_dev,
            "rt_delta_from_baseline": rt_delta,
            "rt_deviation_pct": rt_dev,
            "isotope_dot_product_deviation_pct": idotp_dev,
            "library_dot_product_deviation_pct": dotp_dev,
            "rt_outside_threshold": rt_off,
            "target_extraction_suspect": suspect,
        }

    median_area_ratio = statistics.median(area_ratios) if area_ratios else None
    median_area_dev = (
        (median_area_ratio - 1.0) * 100.0 if median_area_ratio is not None else None
    )
    if median_area_dev is None:
        area_verdict = None
    elif abs(median_area_dev) >= th.peak_area_deviation_pct_fail:
        area_verdict = "fail"
    elif abs(median_area_dev) >= th.peak_area_deviation_pct_warn:
        area_verdict = "warn"
    else:
        area_verdict = "ok"

    return {
        "baseline_id": baseline.get("baseline_id"),
        "targets_compared": len(per_peptide),
        "median_peak_area_ratio": median_area_ratio,
        "median_peak_area_deviation_pct": median_area_dev,
        "median_rt_deviation_pct": statistics.median(rt_devs) if rt_devs else None,
        "targets_extraction_suspect": suspect_count,
        "peak_area_verdict": area_verdict,
        # Both the values and whether they are stock. The platform cannot
        # infer "customised" from the values alone without tracking our
        # shipped defaults per agent version, and it needs to know: a warn on
        # a tuned instrument does not mean the same as a warn on a stock one.
        "thresholds_applied": th.model_dump(),
        "thresholds_source": "default" if th.is_default() else "custom",
        "per_peptide": per_peptide,
    }


def build_payload_comparison(
    classification: RunClassification,
    extraction: ExtractionResult,
    thresholds: QcThresholdsConfig | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the active baseline for a run and compute its comparison.

    Returns ``(baseline_context, comparison_metrics)``, both ``None`` when no
    baseline has been recorded for this (instrument, SPD) — which is the
    normal state until an engineer saves one on the Gold Standards page.

    Best-effort: never raises. A comparison is an enrichment of the payload,
    so a failure here must not cost the run its extracted measurements.
    """
    try:
        baseline = get_active_baseline(classification.instrument_id, classification.spd)
        if baseline is None:
            return None, None
        context = {
            "baseline_id": baseline.get("baseline_id"),
            "label": baseline.get("label"),
            "created_at": baseline.get("created_at"),
            "instrument_id": baseline.get("instrument_id"),
            "spd": baseline.get("spd"),
            "source_run_count": len(baseline.get("source_run_ids", [])),
            "per_peptide": baseline.get("per_peptide", {}),
        }
        metrics = compute_comparison_metrics(
            extraction.target_metrics, baseline, thresholds
        )
        return context, metrics
    except Exception as exc:  # enrichment must not fail the run
        log.warning("baseline_comparison_failed", extra={"error": str(exc)})
        return None, None


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
    "build_payload_comparison",
    "compute_baseline_preview",
    "compute_comparison_metrics",
    "get_active_baseline",
    "list_available_spds",
    "list_baselines",
    "list_ssc0_runs",
    "record_ssc0_run",
    "save_baseline",
]
