from __future__ import annotations

from mdqc.metrics import compute_chromatography_score, compute_run_metrics
from mdqc.types import TargetMetric


def _t(
    target_id: str,
    *,
    detected: bool = True,
    rt_delta: float | None = 0.0,
    mass_error_ppm: float | None = 0.0,
    peak_width_fwhm: float | None = 0.2,
) -> TargetMetric:
    return TargetMetric(
        target_id=target_id,
        rt_delta=rt_delta,
        mass_error_ppm=mass_error_ppm,
        peak_width_fwhm=peak_width_fwhm,
        peak_area=1000.0,
        detected=detected,
    )


def test_empty_targets_zero_recovery_none_medians() -> None:
    rm = compute_run_metrics([], targets_expected=10)
    assert rm.targets_found == 0
    assert rm.target_recovery_pct == 0.0
    assert rm.median_rt_shift is None
    assert rm.median_mass_error_ppm is None
    assert rm.chromatography_score is None


def test_zero_expected_zero_recovery() -> None:
    rm = compute_run_metrics([], targets_expected=0)
    assert rm.target_recovery_pct == 0.0


def test_perfect_metrics_score_near_one() -> None:
    targets = [
        _t("p1", peak_width_fwhm=0.20, mass_error_ppm=0.0),
        _t("p2", peak_width_fwhm=0.20, mass_error_ppm=0.0),
        _t("p3", peak_width_fwhm=0.20, mass_error_ppm=0.0),
    ]
    score = compute_chromatography_score(targets)
    assert score is not None
    assert score >= 0.99


def test_full_recovery_pct() -> None:
    targets = [_t(f"p{i}") for i in range(5)]
    rm = compute_run_metrics(targets, targets_expected=5)
    assert rm.targets_found == 5
    assert rm.target_recovery_pct == 100.0


def test_undetected_targets_drop_recovery() -> None:
    targets = [
        _t("p1", detected=True),
        _t("p2", detected=False),
        _t("p3", detected=True),
        _t("p4", detected=False),
    ]
    rm = compute_run_metrics(targets, targets_expected=4)
    assert rm.targets_found == 2
    assert rm.target_recovery_pct == 50.0


def test_high_mass_error_drops_score() -> None:
    good = [_t(f"g{i}", mass_error_ppm=0.0) for i in range(3)]
    bad = [_t(f"b{i}", mass_error_ppm=20.0) for i in range(3)]
    score_good = compute_chromatography_score(good)
    score_bad = compute_chromatography_score(bad)
    assert score_good is not None and score_bad is not None
    assert score_bad < score_good


def test_median_rt_shift() -> None:
    targets = [
        _t("p1", rt_delta=-0.1),
        _t("p2", rt_delta=0.0),
        _t("p3", rt_delta=0.2),
    ]
    rm = compute_run_metrics(targets, targets_expected=3)
    assert rm.median_rt_shift == 0.0


def test_median_ignores_none() -> None:
    targets = [
        _t("p1", rt_delta=None),
        _t("p2", rt_delta=0.5),
        _t("p3", rt_delta=1.5),
    ]
    rm = compute_run_metrics(targets, targets_expected=3)
    assert rm.median_rt_shift == 1.0


def test_score_clamped_with_huge_mass_error() -> None:
    targets = [_t(f"p{i}", mass_error_ppm=100.0) for i in range(3)]
    score = compute_chromatography_score(targets)
    assert score is not None
    # detection=1 (0.5), width≈1 (0.25), mass=0 → 0.75
    assert 0.0 <= score <= 1.0
    assert abs(score - 0.75) < 1e-6
