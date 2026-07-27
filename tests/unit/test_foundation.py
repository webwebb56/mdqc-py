"""Smoke tests for the foundation: types, config, paths, log."""

from __future__ import annotations

from pathlib import Path

import pytest

from mdqc import __version__
from mdqc.config import Config, ConfigError, defaults, load_config, paths
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    FinalizationState,
    Vendor,
    WellPosition,
)


def test_version_string() -> None:
    assert isinstance(__version__, str)
    assert __version__ != ""


# ─── Types ──────────────────────────────────────────────────────────────────


def test_control_type_is_qc() -> None:
    assert ControlType.SSC0.is_qc()
    assert ControlType.QC_A.is_qc()
    assert ControlType.QC_B.is_qc()
    assert ControlType.BLANK.is_qc()
    assert not ControlType.SAMPLE.is_qc()


def test_control_type_serialization_format() -> None:
    """Schema contract: serialize as SCREAMING_SNAKE."""
    assert ControlType.QC_A.value == "QC_A"
    assert ControlType.SSC0.value == "SSC0"


def test_vendor_is_directory_artifact() -> None:
    assert Vendor.BRUKER.is_directory_artifact
    assert Vendor.WATERS.is_directory_artifact
    assert Vendor.AGILENT.is_directory_artifact
    assert not Vendor.THERMO.is_directory_artifact
    assert not Vendor.SCIEX.is_directory_artifact


def test_well_position_lenient_parser() -> None:
    assert WellPosition.parse("A1") == WellPosition("A", 1)
    assert WellPosition.parse("a1") == WellPosition("A", 1)
    assert WellPosition.parse("A01") == WellPosition("A", 1)
    assert WellPosition.parse("H12") == WellPosition("H", 12)


def test_well_position_rejects_out_of_range() -> None:
    assert WellPosition.parse("I1") is None  # Row beyond H
    assert WellPosition.parse("A13") is None  # Col beyond 12
    assert WellPosition.parse("A0") is None  # Col below 1
    assert WellPosition.parse("") is None
    assert WellPosition.parse("X") is None


def test_finalization_state_values() -> None:
    """Names must exactly match Rust state machine."""
    expected = {"DETECTED", "STABILIZING", "READY", "PROCESSING", "DONE", "FAILED"}
    assert {s.value for s in FinalizationState} == expected


def test_classification_source_values() -> None:
    expected = {"FILENAME", "METADATA", "POSITION", "DEFAULT"}
    assert {s.value for s in ClassificationSource} == expected


def test_confidence_values() -> None:
    assert {c.value for c in Confidence} == {"HIGH", "MEDIUM", "LOW"}


# ─── Defaults ───────────────────────────────────────────────────────────────


def test_upload_retry_sleeps_has_4_entries_not_5() -> None:
    """Critical: docs/AGENT_NOTES § Uploader Tenacity off-by-one trap.

    A 5-entry list shifts every delay by one position. Must be exactly 4 entries
    paired with UPLOAD_TOTAL_ATTEMPTS=5.
    """
    assert len(defaults.UPLOAD_RETRY_SLEEPS) == 4
    assert defaults.UPLOAD_TOTAL_ATTEMPTS == 5


def test_upload_retry_sleeps_match_rust_schedule() -> None:
    """Cross-check the exact ranges against the Rust agent."""
    assert defaults.UPLOAD_RETRY_SLEEPS == [
        (20, 40),       # 30s ± 10s
        (90, 150),      # 2m ± 30s
        (480, 720),     # 10m ± 2m
        (3000, 4200),   # 1h ± 10m
    ]


def test_aumid_matches_installer_contract() -> None:
    assert defaults.AUMID == "MassDynamics.QCAgent"


def test_critical_timeouts_match_spec() -> None:
    assert defaults.SKYLINE_TIMEOUT_S == 900
    assert defaults.STABILITY_WINDOW_S == 60
    assert defaults.STABILIZATION_TIMEOUT_S == 600
    assert defaults.PROCESSING_TIMEOUT_S == 1800
    assert defaults.BRUKER_STABILITY_WINDOW_S == 90
    assert defaults.MAX_PENDING_MB == 1000
    assert defaults.MAX_AGE_DAYS == 30
    # Raised 10 -> 200 in v0.5.6. 10 could not survive a single night of
    # acquisition; see COMPLETED_RETENTION_COUNT and the local-only branch in
    # prune_spool for why this is a data-loss boundary, not just a disk cap.
    assert defaults.COMPLETED_RETENTION_COUNT == 200
    assert defaults.FAILED_FILES_MAX == 100


# ─── Paths ──────────────────────────────────────────────────────────────────


def test_paths_redirected_by_env(tmp_data_dir: Path) -> None:
    assert paths.data_dir() == tmp_data_dir
    assert paths.spool_pending() == tmp_data_dir / "spool" / "pending"
    assert paths.runtime_file() == tmp_data_dir / "runtime.json"


def test_ensure_dirs_creates_layout(tmp_data_dir: Path) -> None:
    paths.ensure_dirs()
    assert (tmp_data_dir / "spool" / "pending").is_dir()
    assert (tmp_data_dir / "spool" / "uploading").is_dir()
    assert (tmp_data_dir / "spool" / "completed").is_dir()
    assert (tmp_data_dir / "spool" / "failed").is_dir()
    assert (tmp_data_dir / "logs").is_dir()
    assert (tmp_data_dir / "methods").is_dir()


# ─── Config ─────────────────────────────────────────────────────────────────


def _write(tmp: Path, body: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(body)
    return p


def test_load_config_minimal(tmp_data_dir: Path) -> None:
    body = """
[agent]
log_level = "info"

[cloud]
api_token = "test-token"

[[instruments]]
id = "EXPLORIS01"
vendor = "thermo"
watch_path = "D:\\\\Data\\\\Exploris"
file_pattern = "*.raw"
template = "QC_Method.sky"
"""
    cfg = load_config(_write(tmp_data_dir, body))
    assert cfg.cloud.api_token == "test-token"
    assert len(cfg.instruments) == 1
    assert cfg.instruments[0].id == "EXPLORIS01"
    assert cfg.instruments[0].vendor == Vendor.THERMO
    assert not cfg.is_local_only()


def test_load_config_local_only_when_no_auth(tmp_data_dir: Path) -> None:
    body = """
[[instruments]]
id = "X"
vendor = "thermo"
watch_path = "D:\\\\Data"
file_pattern = "*.raw"
template = "QC_Method.sky"
"""
    cfg = load_config(_write(tmp_data_dir, body))
    assert cfg.is_local_only()


def test_load_config_cert_without_token_fails_loud(tmp_data_dir: Path) -> None:
    """Critical: docs/AGENT_NOTES § Uploader Auth-config decision matrix."""
    body = """
[cloud]
certificate_thumbprint = "A" * 40

[[instruments]]
id = "X"
vendor = "thermo"
watch_path = "D:\\\\Data"
file_pattern = "*.raw"
template = "QC_Method.sky"
"""
    body = body.replace('"A" * 40', '"' + "A" * 40 + '"')
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_data_dir, body))
    assert "certificate_thumbprint" in str(exc_info.value)
    assert "v1.1" in str(exc_info.value)


def test_load_config_cert_with_token_warns_but_loads(tmp_data_dir: Path) -> None:
    """Token wins; thumbprint is allowed to coexist (warn-only path)."""
    body = (
        '[cloud]\n'
        'api_token = "tok"\n'
        f'certificate_thumbprint = "{"A" * 40}"\n'
        '[[instruments]]\n'
        'id = "X"\n'
        'vendor = "thermo"\n'
        'watch_path = "D:\\\\Data"\n'
        'file_pattern = "*.raw"\n'
        'template = "QC_Method.sky"\n'
    )
    cfg = load_config(_write(tmp_data_dir, body))
    assert cfg.cloud.api_token == "tok"
    assert cfg.cloud.certificate_thumbprint == "A" * 40


def test_load_config_thumbprint_validation(tmp_data_dir: Path) -> None:
    body = (
        '[cloud]\n'
        'api_token = "t"\n'
        'certificate_thumbprint = "not-hex"\n'
        '[[instruments]]\n'
        'id = "X"\n'
        'vendor = "thermo"\n'
        'watch_path = "D:\\\\Data"\n'
        'file_pattern = "*.raw"\n'
        'template = "QC_Method.sky"\n'
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_data_dir, body))


def test_load_config_missing_file_raises(tmp_data_dir: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_data_dir / "does_not_exist.toml")


def test_load_config_bad_toml_raises(tmp_data_dir: Path) -> None:
    p = _write(tmp_data_dir, "this is not = valid = toml { ")
    with pytest.raises(ConfigError):
        load_config(p)


def test_default_endpoint() -> None:
    cfg = Config()
    assert cfg.cloud.endpoint == "https://dev.massdynamics.com/api/evosep_qcs"


def test_dev_and_prod_endpoint_constants_distinct() -> None:
    from mdqc.config import defaults

    assert defaults.ENDPOINT_DEV == "https://dev.massdynamics.com/api/evosep_qcs"
    assert defaults.ENDPOINT_PROD == "https://app.massdynamics.com/api/evosep_qcs"
    assert defaults.ENDPOINT_DEV != defaults.ENDPOINT_PROD
    assert defaults.DEFAULT_ENDPOINT == defaults.ENDPOINT_DEV
