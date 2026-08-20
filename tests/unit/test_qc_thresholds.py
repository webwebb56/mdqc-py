"""Tests for the QC threshold configuration.

Thresholds decide whether a run is flagged, and Evosep asked for them to be
adjustable in the field. That makes two things load-bearing: an ordering that
cannot silently disable a band, and a way to tell a tuned instrument from a
stock one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mdqc.config.schema import QC_THRESHOLD_FIELDS, Config, QcThresholdsConfig


def test_defaults_match_the_evosep_decision_matrix() -> None:
    """Values published by Evosep, 2026-07-28. Changing these changes verdicts."""
    t = QcThresholdsConfig()
    assert t.rt_deviation_pct_max == 2.0
    assert t.dot_product_deviation_pct_max == 5.0
    assert t.dot_product_deviation_pct_suspect == 10.0
    assert t.peak_area_deviation_pct_suspect == 30.0
    assert t.peak_area_deviation_pct_warn == 10.0
    assert t.peak_area_deviation_pct_fail == 25.0


def test_field_list_covers_every_tunable() -> None:
    """QC_THRESHOLD_FIELDS drives form parsing, so it must not drift."""
    declared = set(QcThresholdsConfig.model_fields)
    assert set(QC_THRESHOLD_FIELDS) == declared


# ─── ordering ──────────────────────────────────────────────────────────────


def test_warn_above_fail_is_rejected() -> None:
    """The verdict tests fail before warn, so warn > fail makes warn unreachable.

    Left unvalidated this looks exactly like everything passing.
    """
    with pytest.raises(ValidationError) as exc:
        QcThresholdsConfig(
            peak_area_deviation_pct_warn=30.0, peak_area_deviation_pct_fail=25.0
        )
    assert "never be reached" in str(exc.value)


def test_warn_equal_to_fail_is_allowed() -> None:
    """Collapsing warn into fail is a deliberate choice, not an error."""
    t = QcThresholdsConfig(
        peak_area_deviation_pct_warn=25.0, peak_area_deviation_pct_fail=25.0
    )
    assert t.peak_area_deviation_pct_warn == 25.0


def test_dot_product_normal_above_suspect_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QcThresholdsConfig(dot_product_deviation_pct_max=15.0)


def test_negative_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        QcThresholdsConfig(rt_deviation_pct_max=-1.0)


def test_ordering_is_enforced_on_config_load_too() -> None:
    """A hand-edited config.toml must be caught, not just the web form."""
    with pytest.raises(ValidationError):
        Config(
            qc_thresholds=QcThresholdsConfig.model_construct(
                peak_area_deviation_pct_warn=99.0, peak_area_deviation_pct_fail=1.0
            ).model_dump()
        )


# ─── is_default ────────────────────────────────────────────────────────────


def test_is_default_true_for_shipped_values() -> None:
    assert QcThresholdsConfig().is_default() is True


@pytest.mark.parametrize("field", QC_THRESHOLD_FIELDS)
def test_is_default_false_when_any_field_changed(field: str) -> None:
    """Every tunable must register as customised — not just the obvious ones."""
    shipped = QcThresholdsConfig()
    bumped = getattr(shipped, field)
    # Move each field in the direction that keeps the ordering valid.
    if field in ("peak_area_deviation_pct_warn", "dot_product_deviation_pct_max"):
        bumped -= 1.0
    else:
        bumped += 1.0
    assert QcThresholdsConfig(**{field: bumped}).is_default() is False


def test_is_default_survives_a_round_trip_through_config() -> None:
    cfg = Config(qc_thresholds=QcThresholdsConfig(peak_area_deviation_pct_warn=8.0))
    assert cfg.qc_thresholds.is_default() is False
    assert Config().qc_thresholds.is_default() is True
