"""Skyline CSV report parser.

Column matching is case-insensitive and ignores spaces/underscores. Aliases
are taken verbatim from the Rust extractor (see docs/AGENT_NOTES § Extractor).
"""

from __future__ import annotations

import csv
import hashlib
import logging
from pathlib import Path

from mdqc.types import TargetMetric

logger = logging.getLogger(__name__)

_ALIASES: dict[str, list[str]] = {
    "peptide_sequence": [
        "peptide sequence",
        "peptidesequence",
        "peptide",
        "modified sequence",
    ],
    "precursor_mz": [
        "precursor mz",
        "precursormz",
        "mz",
        "precursor m/z",
    ],
    "retention_time": [
        "best retention time",
        "peptide retention time",
        "retention time",
        "rt",
        "peptideretentiontime",
    ],
    "rt_start": [
        "min start time",
        "minstarttime",
    ],
    "rt_end": [
        "max end time",
        "maxendtime",
    ],
    "rt_expected": [
        "expected rt",
        "expected retention time",
        "rt expected",
    ],
    "rt_delta": [
        "rt delta",
        "delta rt",
        "retention time delta",
    ],
    "peak_area": [
        "total area",
        "totalarea",
        "sum area",
        "peak area",
        "area",
    ],
    # DIA reports (e.g. Evosep / Astral) split intensity into MS1 + fragment
    # columns instead of providing a single "Total Area". When both are
    # present we sum them into peak_area below.
    "peak_area_ms1": [
        "total area ms1",
        "totalareams1",
    ],
    "peak_area_fragment": [
        "total area fragment",
        "totalareafragment",
    ],
    "peak_height": [
        "max height",
        "maxheight",
        "peak height",
        "height",
    ],
    "peak_width_fwhm": [
        "max fwhm",
        "maxfwhm",
        "fwhm",
        "peak width fwhm",
    ],
    "peak_symmetry": [
        "peak symmetry",
        "asymmetry",
        "asymmetry factor",
    ],
    "mass_error_ppm": [
        "average mass error ppm",
        "mass error ppm",
        "ppm error",
        "averagemasserrorppm",
    ],
    "isotope_dot_product": [
        "isotope dot product",
        "isotopedotp",
        "isotope dotp",
    ],
    "library_dot_product": [
        "library dot product",
        "librarydotp",
        "library dotp",
    ],
}


def _normalise(s: str) -> str:
    return s.strip().lower().replace(" ", "").replace("_", "")


def _build_alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            index[_normalise(alias)] = canonical
    return index


_ALIAS_INDEX = _build_alias_index()


def _map_columns(headers: list[str]) -> tuple[dict[str, int], list[tuple[str, int]]]:
    known: dict[str, int] = {}
    extra: list[tuple[str, int]] = []
    for idx, header in enumerate(headers):
        if header is None:
            continue
        normalised = _normalise(header)
        canonical = _ALIAS_INDEX.get(normalised)
        if canonical is not None and canonical not in known:
            known[canonical] = idx
        else:
            name = header.strip()
            if name:
                extra.append((name, idx))
    return known, extra


_NA_TOKENS = frozenset({"#n/a", "n/a", "na", "nan", "null", "none", ""})


def _get_float(row: list[str], idx: int | None) -> float | None:
    if idx is None or idx >= len(row):
        return None
    raw = row[idx].strip()
    if raw.lower() in _NA_TOKENS:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _get_str(row: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    raw = row[idx].strip()
    return raw or None


def _hash_target_id(seq: str | None, mz: float | None, fallback_idx: int) -> str:
    if seq:
        h = hashlib.sha1()
        h.update(seq.encode("utf-8"))
        if mz is not None:
            h.update(f"|{mz:.6f}".encode())
        return h.hexdigest()[:16]
    return str(fallback_idx)


def parse_skyline_csv(path: Path) -> list[TargetMetric]:
    metrics: list[TargetMetric] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            headers = next(reader)
        except StopIteration:
            return metrics

        known, extra = _map_columns(headers)

        for row_idx, row in enumerate(reader):
            if not row or all(not cell.strip() for cell in row):
                continue

            peptide_seq = _get_str(row, known.get("peptide_sequence"))
            precursor_mz = _get_float(row, known.get("precursor_mz"))
            retention_time = _get_float(row, known.get("retention_time"))
            rt_expected = _get_float(row, known.get("rt_expected"))
            rt_delta = _get_float(row, known.get("rt_delta"))
            if rt_delta is None and retention_time is not None and rt_expected is not None:
                rt_delta = retention_time - rt_expected

            peak_area = _get_float(row, known.get("peak_area"))
            if peak_area is None:
                # Fall back to MS1 + fragment split, used by DIA QC reports
                # (Evosep / Astral). Either component may be absent.
                ms1 = _get_float(row, known.get("peak_area_ms1"))
                frag = _get_float(row, known.get("peak_area_fragment"))
                if ms1 is not None or frag is not None:
                    peak_area = (ms1 or 0.0) + (frag or 0.0)

            extra_metrics: dict[str, float] = {}
            for name, idx in extra:
                if idx >= len(row):
                    continue
                raw = row[idx].strip()
                if not raw:
                    continue
                try:
                    extra_metrics[name] = float(raw)
                except ValueError:
                    logger.debug("dropping non-numeric extra column %s=%r", name, raw)

            target = TargetMetric(
                target_id=_hash_target_id(peptide_seq, precursor_mz, row_idx),
                peptide_sequence=peptide_seq,
                precursor_mz=precursor_mz,
                retention_time=retention_time,
                rt_expected=rt_expected,
                rt_delta=rt_delta,
                peak_area=peak_area,
                peak_height=_get_float(row, known.get("peak_height")),
                peak_width_fwhm=_get_float(row, known.get("peak_width_fwhm")),
                peak_symmetry=_get_float(row, known.get("peak_symmetry")),
                mass_error_ppm=_get_float(row, known.get("mass_error_ppm")),
                isotope_dot_product=_get_float(row, known.get("isotope_dot_product")),
                library_dot_product=_get_float(row, known.get("library_dot_product")),
                detected=peak_area is not None and peak_area > 0,
                extra_metrics=extra_metrics,
            )
            metrics.append(target)

    return metrics
