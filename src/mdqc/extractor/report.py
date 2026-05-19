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


ColumnOverrides = dict[str, "str | list[str] | None"]


def _resolve_overrides(
    headers: list[str],
    overrides: ColumnOverrides | None,
) -> tuple[dict[str, int], dict[str, list[int]], set[int]]:
    """Resolve user-supplied column overrides to header indices.

    Returns ``(single_idx, sum_idx, consumed)`` where:
      - ``single_idx`` maps canonical → header index for single-column overrides
      - ``sum_idx`` maps canonical → [indices] for sum-list overrides (the
        parser sums these at row time, used by DIA reports that split
        intensity into MS1 + fragment columns)
      - ``consumed`` is the set of indices already claimed — used to skip
        the alias-fallback for those headers so they don't double-map.
    """
    single_idx: dict[str, int] = {}
    sum_idx: dict[str, list[int]] = {}
    consumed: set[int] = set()
    if not overrides:
        return single_idx, sum_idx, consumed

    header_lookup: dict[str, int] = {}
    for idx, header in enumerate(headers):
        if header is None:
            continue
        header_lookup[_normalise(header)] = idx

    for canonical, override in overrides.items():
        if override is None:
            continue
        names = [override] if isinstance(override, str) else list(override)
        indices: list[int] = []
        for name in names:
            idx = header_lookup.get(_normalise(name))
            if idx is None:
                logger.warning(
                    "Column override %s=%r not found in CSV headers; falling back to defaults",
                    canonical, name,
                )
                indices = []
                break
            indices.append(idx)
        if not indices:
            continue
        if len(indices) == 1 and not isinstance(override, list):
            single_idx[canonical] = indices[0]
        else:
            sum_idx[canonical] = indices
        consumed.update(indices)
    return single_idx, sum_idx, consumed


def _map_columns(
    headers: list[str],
    overrides: ColumnOverrides | None = None,
) -> tuple[dict[str, int], dict[str, list[int]], list[tuple[str, int]]]:
    """Map CSV headers to canonical metric names.

    Resolution order per canonical:
      1. Explicit override from ``overrides`` (single column or sum list)
      2. Built-in alias table (``_ALIASES``)
      3. Otherwise passed through as ``extra_metrics``

    Returns ``(single_idx, sum_idx, extra)``.
    """
    single_idx, sum_idx, consumed = _resolve_overrides(headers, overrides)
    extra: list[tuple[str, int]] = []
    for idx, header in enumerate(headers):
        if header is None or idx in consumed:
            continue
        normalised = _normalise(header)
        canonical = _ALIAS_INDEX.get(normalised)
        if canonical is not None and canonical not in single_idx and canonical not in sum_idx:
            single_idx[canonical] = idx
        else:
            name = header.strip()
            if name:
                extra.append((name, idx))
    return single_idx, sum_idx, extra


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


def _sum_indices(row: list[str], indices: list[int]) -> float | None:
    """Sum the numeric values at the given column indices for one row.

    Returns ``None`` if every cell is non-numeric (so callers can fall back
    to alternative resolution). Missing components are treated as zero so
    long as at least one value is present.
    """
    values: list[float] = []
    for idx in indices:
        v = _get_float(row, idx)
        if v is not None:
            values.append(v)
    if not values:
        return None
    return sum(values)


def _resolve_value(
    row: list[str],
    canonical: str,
    single_idx: dict[str, int],
    sum_idx: dict[str, list[int]],
) -> float | None:
    """Look up a numeric canonical column, preferring sum-list overrides."""
    if canonical in sum_idx:
        return _sum_indices(row, sum_idx[canonical])
    return _get_float(row, single_idx.get(canonical))


def parse_skyline_csv(
    path: Path,
    column_overrides: ColumnOverrides | None = None,
) -> list[TargetMetric]:
    """Parse a Skyline-produced CSV into ``TargetMetric`` rows.

    ``column_overrides`` lets the caller declare the mapping from CSV header
    names to canonical metrics (``peak_area``, ``mass_error_ppm``, ...) — used
    when a deployment's ``.skyr`` exports columns whose names don't match
    mdqc's built-in alias dictionary. Each value is either a single column
    name or a list of column names to sum. Anything left out falls back to
    the built-in aliases.
    """
    metrics: list[TargetMetric] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            headers = next(reader)
        except StopIteration:
            return metrics

        single_idx, sum_idx, extra = _map_columns(headers, column_overrides)

        for row_idx, row in enumerate(reader):
            if not row or all(not cell.strip() for cell in row):
                continue

            peptide_seq = _get_str(row, single_idx.get("peptide_sequence"))
            precursor_mz = _resolve_value(row, "precursor_mz", single_idx, sum_idx)
            retention_time = _resolve_value(row, "retention_time", single_idx, sum_idx)
            rt_expected = _resolve_value(row, "rt_expected", single_idx, sum_idx)
            rt_delta = _resolve_value(row, "rt_delta", single_idx, sum_idx)
            if rt_delta is None and retention_time is not None and rt_expected is not None:
                rt_delta = retention_time - rt_expected

            peak_area = _resolve_value(row, "peak_area", single_idx, sum_idx)
            if peak_area is None:
                # Legacy fallback: DIA reports often expose MS1 + fragment as
                # the only intensity columns. Sum them if neither an explicit
                # peak_area column nor an override is present.
                ms1 = _get_float(row, single_idx.get("peak_area_ms1"))
                frag = _get_float(row, single_idx.get("peak_area_fragment"))
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
                peak_height=_resolve_value(row, "peak_height", single_idx, sum_idx),
                peak_width_fwhm=_resolve_value(row, "peak_width_fwhm", single_idx, sum_idx),
                peak_symmetry=_resolve_value(row, "peak_symmetry", single_idx, sum_idx),
                mass_error_ppm=_resolve_value(row, "mass_error_ppm", single_idx, sum_idx),
                isotope_dot_product=_resolve_value(row, "isotope_dot_product", single_idx, sum_idx),
                library_dot_product=_resolve_value(row, "library_dot_product", single_idx, sum_idx),
                detected=peak_area is not None and peak_area > 0,
                extra_metrics=extra_metrics,
            )
            metrics.append(target)

    return metrics
