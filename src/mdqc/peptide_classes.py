"""Peptide-class assignment + class-aware run-level rollups.

Operators declare ``[[peptide_classes]]`` rules in ``config.toml`` that map
Skyline ``Protein`` column values to a class with a purpose
(recovery / digest_efficiency / oxidation / alkylation / custom). At
extraction time each ``TargetMetric`` is annotated with the matched class;
downstream rollups (run-level recovery, miss-cleavage ratio, ...) consult
the purpose to decide how to aggregate.

The classifier deliberately uses case-insensitive substring matching to
mirror the existing ``ClassifierRule`` pattern semantics — easy for
operators to reason about, no regex literacy required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mdqc.config.schema import PeptideClassRule
    from mdqc.types import TargetMetric


def assign_peptide_classes(
    metrics: list[TargetMetric],
    rules: list[PeptideClassRule] | None,
) -> None:
    """Annotate each metric in-place with peptide_class / peptide_class_purpose.

    No-op if no rules are configured or no metric has a protein_name.
    """
    if not rules:
        return
    for m in metrics:
        if not m.protein_name:
            continue
        protein_lower = m.protein_name.lower()
        for rule in rules:
            if rule.protein_name.lower() in protein_lower:
                m.peptide_class = rule.label or rule.protein_name
                m.peptide_class_purpose = rule.purpose
                break


def filter_for_recovery(
    metrics: list[TargetMetric],
    rules: list[PeptideClassRule] | None,
) -> list[TargetMetric]:
    """Return only metrics that should count toward target recovery.

    A metric is excluded when its assigned class has ``exclude_from_recovery``
    set to True, or its purpose is ``digest_efficiency`` (since the
    miss-cleavage pair is reported separately and including it would
    artificially lower recovery — the 1-miss peptide is often missing).
    """
    if not rules:
        return metrics
    rules_by_label: dict[str, PeptideClassRule] = {}
    for r in rules:
        rules_by_label[(r.label or r.protein_name)] = r

    def _keep(m: TargetMetric) -> bool:
        if m.peptide_class is None:
            return True
        rule = rules_by_label.get(m.peptide_class)
        if rule is None:
            return True
        if rule.exclude_from_recovery:
            return False
        return rule.purpose != "digest_efficiency"

    return [m for m in metrics if _keep(m)]


def compute_digest_efficiency(
    metrics: list[TargetMetric],
) -> float | None:
    """Compute the 0-miss / (0-miss + 1-miss) digest-efficiency ratio.

    Operates on the subset of metrics whose ``peptide_class_purpose`` is
    ``digest_efficiency``. The convention (from Evosep's QC method) is that
    such a class contains exactly **two** peptides — a 0-miss peptide and
    its 1-miss partner — with the 0-miss appearing first when sorted by
    peptide sequence length (the 1-miss is longer by the missed-cleavage
    residue).

    Returns ``None`` if no digest_efficiency class is present, or if
    neither peptide had a detected peak area.
    """
    digest_peps = [m for m in metrics if m.peptide_class_purpose == "digest_efficiency"]
    if not digest_peps:
        return None
    # Heuristic: shorter sequence is the 0-miss form (the 1-miss adds a
    # cleavage site residue → longer sequence).
    by_length = sorted(
        digest_peps,
        key=lambda m: len(m.peptide_sequence or ""),
    )
    if len(by_length) < 2:
        return None
    zero_miss = by_length[0]
    one_miss = by_length[1]
    zero_area = zero_miss.peak_area or 0.0
    one_area = one_miss.peak_area or 0.0
    total = zero_area + one_area
    if total <= 0:
        return None
    return zero_area / total


__all__ = [
    "assign_peptide_classes",
    "compute_digest_efficiency",
    "filter_for_recovery",
]
