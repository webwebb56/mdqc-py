"""Pydantic models for config.toml.

Schema must stay compatible with the Rust agent (docs/AGENT_NOTES § Cross-cutting).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mdqc.config import defaults
from mdqc.types import Vendor


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str = "auto"
    log_level: Literal["error", "warn", "info", "debug", "trace"] = "info"
    enable_toast_notifications: bool = True


class CloudConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    endpoint: str = defaults.DEFAULT_ENDPOINT
    api_token: str | None = None
    certificate_thumbprint: str | None = None
    proxy: str | None = None

    @field_validator("certificate_thumbprint")
    @classmethod
    def _normalise_thumbprint(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.replace(" ", "").upper()
        if len(v) != 40 or any(c not in "0123456789ABCDEF" for c in v):
            raise ValueError(
                "certificate_thumbprint must be a 40-character hex SHA-1 fingerprint"
            )
        return v


class ReportColumnsConfig(BaseModel):
    """Per-deployment override of CSV column → canonical-metric mapping.

    Each entry may be a single column name or a list of column names. Lists
    are interpreted as "sum these numeric columns" — useful for DIA reports
    that split intensity into ``Total Area MS1`` + ``Total Area Fragment``.

    Any field left as ``None`` falls back to the built-in alias dictionary in
    ``mdqc.extractor.report``. Operators only need to set the columns whose
    naming diverges from a typical Skyline export.
    """

    model_config = ConfigDict(extra="allow")

    peak_area: str | list[str] | None = None
    peak_height: str | list[str] | None = None
    peak_width_fwhm: str | None = None
    peak_symmetry: str | None = None
    mass_error_ppm: str | None = None
    retention_time: str | None = None
    rt_expected: str | None = None
    rt_delta: str | None = None
    isotope_dot_product: str | None = None
    library_dot_product: str | None = None
    peptide_sequence: str | None = None
    precursor_mz: str | None = None
    protein_name: str | None = None


class SkylineConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = "auto"
    timeout_seconds: int = defaults.SKYLINE_TIMEOUT_S
    process_priority: Literal["normal", "below_normal", "idle"] = "below_normal"
    report_columns: ReportColumnsConfig = ReportColumnsConfig()
    # Report definition (.skyr) path. "auto" uses the bundled
    # methods_dir()/MD_QC_Report.skyr (tolerant — falls back to the template's
    # own report if absent). An explicit path is used verbatim and, if it does
    # not exist, fails the extraction with a clear message rather than silently
    # falling back — a missing configured report is an operator error, not a
    # default to swallow.
    report_skyr_path: str = "auto"
    # Skyline reports with rowsource="Transition" emit one CSV row per
    # transition (3-8 per peptide). When true (default) mdqc collapses these to
    # one row per peptide before emitting the payload. Set false only if a
    # deployment genuinely wants transition-level detail in the payload.
    collapse_transitions_to_peptides: bool = True


class WatcherConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    use_filesystem_events: bool = True
    scan_interval_seconds: int = defaults.SCAN_INTERVAL_S
    stability_window_seconds: int = defaults.STABILITY_WINDOW_S
    stabilization_timeout_seconds: int = defaults.STABILIZATION_TIMEOUT_S


class SpoolConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_pending_mb: int = defaults.MAX_PENDING_MB
    max_age_days: int = defaults.MAX_AGE_DAYS
    completed_retention_count: int = defaults.COMPLETED_RETENTION_COUNT


class WatcherOverrides(BaseModel):
    model_config = ConfigDict(extra="allow")

    stability_window_seconds: int | None = None


CONTROL_TYPE_VALUES = ["QC_A", "QC_B", "SSC0", "BLANK", "SAMPLE"]


class PeptideClassRule(BaseModel):
    """Maps a Skyline ``Peptide.Protein.Name`` value to a peptide class with
    a declared purpose. Lets operators group diagnostic peptides by what
    they're measuring — recovery, digest efficiency, oxidation, alkylation —
    so the dashboard can apply different rollup logic per class.

    Matching is case-insensitive substring against the protein name column.
    First match wins.

    Recognised purposes:
      - ``recovery`` (default) — counted toward the per-run target recovery
        KPI, used in z-score baselines
      - ``digest_efficiency`` — paired peptides (0-miss / 1-miss); excluded
        from recovery and z-score; surfaced as a digest-efficiency ratio
      - ``oxidation`` — Met-containing reporters for oxidation rate
      - ``alkylation`` — Cys-containing reporters for alkylation efficiency
      - ``custom`` — passthrough; surfaced as its own group on the dashboard
        without special aggregation
    """

    model_config = ConfigDict(extra="allow")

    protein_name: str
    label: str = ""
    purpose: Literal[
        "recovery", "digest_efficiency", "oxidation", "alkylation", "custom"
    ] = "recovery"
    exclude_from_recovery: bool = False
    exclude_from_baseline: bool = False
    notes: str = ""

    @field_validator("protein_name")
    @classmethod
    def _nonempty_protein(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("protein_name must not be empty")
        return v


class ClassifierRule(BaseModel):
    """A single filename-pattern → control-type mapping.

    `pattern` is matched as a case-insensitive substring of the raw filename stem.
    Rules are evaluated in order; first match wins.
    """

    model_config = ConfigDict(extra="allow")

    pattern: str
    control_type: str = "QC_A"
    notes: str = ""

    @field_validator("control_type")
    @classmethod
    def _valid_control_type(cls, v: str) -> str:
        if v not in CONTROL_TYPE_VALUES:
            raise ValueError(f"control_type must be one of {CONTROL_TYPE_VALUES}")
        return v

    @field_validator("pattern")
    @classmethod
    def _nonempty_pattern(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pattern must be non-empty")
        return v.strip()


class InstrumentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    vendor: Vendor
    watch_path: Path
    file_pattern: str
    template: str
    watcher_overrides: WatcherOverrides | None = None

    @field_validator("id")
    @classmethod
    def _id_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("instrument id must be non-empty")
        return v


class Config(BaseModel):
    """Root config model corresponding to config.toml."""

    model_config = ConfigDict(extra="allow")

    agent: AgentConfig = Field(default_factory=AgentConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    skyline: SkylineConfig = Field(default_factory=SkylineConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    spool: SpoolConfig = Field(default_factory=SpoolConfig)
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    classifier_rules: list[ClassifierRule] = Field(
        default_factory=lambda: [
            ClassifierRule(pattern="SSC0",        control_type="SSC0",   notes="System suitability control"),
            ClassifierRule(pattern="QCA",         control_type="QC_A",   notes="QC level A"),
            ClassifierRule(pattern="QCB",         control_type="QC_B",   notes="QC level B"),
            ClassifierRule(pattern="QC_A",        control_type="QC_A",   notes="QC level A (underscore form)"),
            ClassifierRule(pattern="QC_B",        control_type="QC_B",   notes="QC level B (underscore form)"),
            ClassifierRule(pattern="Hela_QC",     control_type="QC_A",   notes="Evosep HeLa QC injection"),
            ClassifierRule(pattern="eb_inbetween",control_type="BLANK",  notes="Evosep between-run blank"),
            ClassifierRule(pattern="BLANK",       control_type="BLANK",  notes="Blank injection"),
            ClassifierRule(pattern="BLK",         control_type="BLANK",  notes="Blank (short form)"),
        ]
    )
    peptide_classes: list[PeptideClassRule] = Field(default_factory=list)

    # ─── Cross-field validation ─────────────────────────────────────────────

    def cert_thumbprint_unsupported(self) -> str | None:
        """Returns an error message if cert auth is configured without a token.

        v1 does not support mTLS via cert store. Cert-configured deployments
        must fail loud, not silently fall through to local-only mode.
        See docs/AGENT_NOTES § Uploader / Auth-config decision matrix.
        """
        if self.cloud.certificate_thumbprint and not self.cloud.api_token:
            return (
                "[cloud] certificate_thumbprint is set but mTLS via the Windows "
                "certificate store is not yet implemented in the Python agent. "
                "Either set [cloud] api_token, or pin to the Rust agent until v1.1."
            )
        return None

    def is_local_only(self) -> bool:
        """True iff no upload auth is configured.

        Pending payloads are moved straight to completed/ without an upload attempt.
        """
        return not self.cloud.api_token and not self.cloud.certificate_thumbprint
