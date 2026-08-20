"""Pydantic models for config.toml.

Schema must stay compatible with the Rust agent (docs/AGENT_NOTES § Cross-cutting).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


QC_THRESHOLD_FIELDS = (
    "rt_deviation_pct_max",
    "dot_product_deviation_pct_max",
    "dot_product_deviation_pct_suspect",
    "peak_area_deviation_pct_suspect",
    "peak_area_deviation_pct_warn",
    "peak_area_deviation_pct_fail",
)
"""The tunable thresholds, in the order the Settings page presents them.

Module-level rather than a class attribute: pydantic converts underscore-
prefixed class attributes into private attributes, which are unreadable from
the class itself and so unusable for driving form parsing.
"""


class QcThresholdsConfig(BaseModel):
    """Thresholds for interpreting a run against its SSC0 gold-standard baseline.

    Seeded from Evosep's first-draft decision matrix (Stoyan Stoychev,
    2026-07-28), derived from a timsTOF HT dilution series: QC B at
    100/75/50% x 200/300/500 SPD x 8 replicates, plus 16 SSC0 at 200 SPD.

    **These numbers are provisional.** They come from one instrument; the
    same series is being run across ~7 more platforms to check the pattern
    holds. They are config so that recalibrating is an edit here, not a code
    change. The agent emits the underlying measurements alongside any flag it
    derives, so the platform can always re-derive or override.
    """

    model_config = ConfigDict(extra="allow")

    # "Correct target extraction: <2% RT CV ... relative to SSC0". Retention
    # time is the single best discriminator of a wrongly-picked peak — it is
    # stable to well under 1% across a batch absent LC changes.
    rt_deviation_pct_max: float = 2.0
    # "Correct target extraction: <5% idotp/dotp CV"; ">10%" indicates a
    # wrong target. Note idotp alone is not sufficient — Evosep observed a
    # wrongly-extracted peak at idotp 0.92 — hence the combination rule below.
    dot_product_deviation_pct_max: float = 5.0
    dot_product_deviation_pct_suspect: float = 10.0
    # "potentially >30-40% Peak Area CV" on a wrongly-extracted target.
    peak_area_deviation_pct_suspect: float = 30.0
    # Response is buffered: "a 10% decrease in peak area, relative to SSC0,
    # could indicate as much as 25% decrease in Evotip load, whilst a 25%
    # decrease ... as much as 50%".
    #
    # CAVEAT, measured against Evosep's own 200 SPD numbers: the 75% QC B
    # condition medians at -9.5% deviation, so a 10.0 warn threshold reads it
    # as "ok" and misses the case the threshold exists to catch — it clears
    # the boundary by half a percentage point. The 50% condition (-27.5%)
    # trips "fail" correctly. Left at Evosep's stated 10.0 rather than
    # silently retuned; ~8.0 would catch the 75% condition. Revisit once the
    # series has run on the other platforms.
    peak_area_deviation_pct_warn: float = 10.0
    peak_area_deviation_pct_fail: float = 25.0

    @field_validator(*QC_THRESHOLD_FIELDS)
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("qc_thresholds values must be non-negative percentages")
        return v

    @model_validator(mode="after")
    def _check_band_ordering(self) -> QcThresholdsConfig:
        """Reject orderings that silently disable a band.

        ``peak_area_verdict`` tests fail before warn, so a warn threshold above
        the fail threshold makes the warn band unreachable — no run would ever
        warn, which looks indistinguishable from everything passing. The
        dot-product pair has the same trap: the "normal" bound must sit below
        the "suspect" bound or the two describe overlapping states.

        Enforced on the model rather than in the web UI so a hand-edited
        config.toml is caught at load time too.
        """
        if self.peak_area_deviation_pct_warn > self.peak_area_deviation_pct_fail:
            raise ValueError(
                "peak_area_deviation_pct_warn "
                f"({self.peak_area_deviation_pct_warn}) must not exceed "
                f"peak_area_deviation_pct_fail ({self.peak_area_deviation_pct_fail}) "
                "— the warn band would never be reached"
            )
        if self.dot_product_deviation_pct_max > self.dot_product_deviation_pct_suspect:
            raise ValueError(
                "dot_product_deviation_pct_max "
                f"({self.dot_product_deviation_pct_max}) must not exceed "
                f"dot_product_deviation_pct_suspect "
                f"({self.dot_product_deviation_pct_suspect})"
            )
        return self

    def is_default(self) -> bool:
        """True when every threshold still holds its shipped value.

        Emitted with each payload so the platform can distinguish an
        instrument running stock thresholds from one an engineer has tuned,
        without having to know our shipped defaults for every agent version.
        """
        shipped = QcThresholdsConfig()
        return all(
            getattr(self, name) == getattr(shipped, name)
            for name in QC_THRESHOLD_FIELDS
        )


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
    qc_thresholds: QcThresholdsConfig = Field(default_factory=QcThresholdsConfig)

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
