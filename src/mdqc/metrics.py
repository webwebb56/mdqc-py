"""Run-level metric aggregation."""

from __future__ import annotations

from mdqc.types import RunMetrics, TargetMetric


def compute_run_metrics(
    targets: list[TargetMetric], targets_expected: int
) -> RunMetrics:
    found = sum(1 for t in targets if t.detected)
    recovery = (found / targets_expected) * 100 if targets_expected > 0 else 0.0
    median_rt = _median([t.rt_delta for t in targets if t.rt_delta is not None])
    median_mass = _median(
        [t.mass_error_ppm for t in targets if t.mass_error_ppm is not None]
    )
    chrom = compute_chromatography_score(targets)
    return RunMetrics(
        targets_found=found,
        targets_expected=targets_expected,
        target_recovery_pct=recovery,
        median_rt_shift=median_rt,
        median_mass_error_ppm=median_mass,
        chromatography_score=chrom,
    )


def compute_chromatography_score(targets: list[TargetMetric]) -> float | None:
    # score = 0.5 * detection_rate
    #       + 0.25 * (1 - peak_width_cv)
    #       + 0.25 * (1 - clamp(|median_mass_error_ppm| / 10, 0, 1))
    # Components with insufficient data (<2 FWHM samples, no mass-error samples)
    # contribute 0; weights are kept fixed so the score is comparable across runs.
    from statistics import mean, pstdev

    if not targets:
        return None
    detection_rate = sum(1 for t in targets if t.detected) / len(targets)

    fwhm = [t.peak_width_fwhm for t in targets if t.peak_width_fwhm is not None]
    if len(fwhm) >= 2 and mean(fwhm) > 0:
        cv = pstdev(fwhm) / mean(fwhm)
        width_term = max(0.0, min(1.0, 1.0 - cv))
    else:
        width_term = 0.0

    mass_errs = [
        abs(t.mass_error_ppm) for t in targets if t.mass_error_ppm is not None
    ]
    if mass_errs:
        med_mass = _median(mass_errs)
        assert med_mass is not None
        mass_term = max(0.0, min(1.0, 1.0 - med_mass / 10.0))
    else:
        mass_term = 0.0

    return 0.5 * detection_rate + 0.25 * width_term + 0.25 * mass_term


def _median(values: list[float]) -> float | None:
    from statistics import median

    if not values:
        return None
    return float(median(values))
