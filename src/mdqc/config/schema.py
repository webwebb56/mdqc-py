"""Pydantic models for config.toml.

Schema must stay compatible across agent implementations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mdqc.config import defaults
from mdqc.types import ControlType, Vendor


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str = "auto"
    log_level: Literal["error", "warn", "info", "debug", "trace"] = "info"
    enable_toast_notifications: bool = True


def normalise_endpoint(raw: str) -> str:
    """Tidy an endpoint and retire hosts that no longer answer.

    Trim, add https:// when absent, drop a trailing slash. A schemeless URL
    fails twice over: platform_app_url() cannot parse it, so the nav loses the
    platform link, and httpx rejects the POST as a request error, which the
    uploader classifies as transient and retries forever. A retired host is
    replaced with the shipped default rather than left to fail at DNS.

    On the model, so a hand-edited config.toml, the wizard, the Settings form
    and the tests all heal identically."""
    value = (raw or "").strip()
    if not value:
        return defaults.DEFAULT_ENDPOINT
    if "://" not in value:
        value = f"https://{value}"
    if urlsplit(value).netloc.lower() in defaults.LEGACY_ENDPOINT_HOSTS:
        return defaults.DEFAULT_ENDPOINT
    # A trailing slash is the server's business - /v1 and /v1/ are different
    # resources - so it is left alone, except where trimming it lands exactly
    # on one of our own endpoints. That keeps the picker on Production for a
    # URL pasted with one, without rewriting anybody else's URL.
    trimmed = value.rstrip("/")
    if trimmed in (defaults.ENDPOINT_DEV, defaults.ENDPOINT_PROD):
        return trimmed
    return value


class CloudConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    endpoint: str = defaults.DEFAULT_ENDPOINT
    api_token: str | None = None
    certificate_thumbprint: str | None = None
    proxy: str | None = None
    # Hold an instrument's uploads at an SPD until it has a gold standard, and
    # again while new gold-standard runs are waiting to be saved as one. Held
    # runs are re-scored against the new baseline before they upload, so
    # nothing reaches the platform compared against the wrong reference.
    hold_until_gold_standard: bool = True

    @field_validator("endpoint")
    @classmethod
    def _clean_endpoint(cls, v: str) -> str:
        return normalise_endpoint(v)

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


# Wire values, in the order an operator meets them.
CONTROL_TYPE_VALUES = ["PCD", "SSC", "SSC0", "BLANK", "SAMPLE"]

# What people call them. The web UI and `mdqc classify` show these; the
# payload carries the wire value.
CONTROL_TYPE_LABELS = {
    "PCD": "PCD — Process Control Digest",
    "SSC": "SSC — System Suitability Control",
    "SSC0": "SSC-gold — gold standard",
    "BLANK": "Blank",
    "SAMPLE": "Sample",
}


def normalise_control_type(raw: str) -> str:
    """Current wire value for a control type, healing pre-1.3 spellings.

    Configs written before schema 1.3 say QC_A and QC_B. Rejecting those would
    stop an upgraded agent from starting at all, so they are translated — the
    same treatment a retired cloud endpoint gets."""
    try:
        return ControlType(raw).value
    except ValueError:
        raise ValueError(f"control_type must be one of {CONTROL_TYPE_VALUES}") from None


# Notes carried by the default rules before 1.3, and what they say now. Only
# these exact strings are rewritten; a note an operator wrote is left alone.
_LEGACY_RULE_NOTES = {
    "QC level A": "Process Control Digest (pre-2026 name)",
    "QC level A (underscore form)": "Process Control Digest (pre-2026 name)",
    "QC level B": "System Suitability Control (pre-2026 name)",
    "QC level B (underscore form)": "System Suitability Control (pre-2026 name)",
    "System suitability control": "SSC-gold (gold standard)",
}


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

    Seeded from the instrument vendor's first-draft decision matrix
    (2026-07-28), derived from a timsTOF HT dilution series: QC B at
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
    # wrong target. Note idotp alone is not sufficient — the vendor observed a
    # wrongly-extracted peak at idotp 0.92 — hence the combination rule below.
    dot_product_deviation_pct_max: float = 5.0
    dot_product_deviation_pct_suspect: float = 10.0
    # "potentially >30-40% Peak Area CV" on a wrongly-extracted target.
    peak_area_deviation_pct_suspect: float = 30.0
    # Response is buffered: "a 10% decrease in peak area, relative to SSC0,
    # could indicate as much as 25% decrease in Evotip load, whilst a 25%
    # decrease ... as much as 50%".
    #
    # Raised 10.0 -> 15.0 at the vendor's instruction (SOP review, annotations
    # 46-48): late-eluting targets under-estimate recovery loss and were
    # producing false warnings, with mid-gradient peptides the better
    # predictors. The figure was given three times, with that reasoning,
    # so it is applied rather than queried.
    #
    # CAVEAT, recorded rather than silently resolved: measured against
    # the vendor's own 200 SPD numbers, the 75% load condition medians at -9.5%
    # deviation, so it read "ok" even at 10.0 — clearing the old threshold by
    # half a point — and reads "ok" more comfortably at 15.0. The 50%
    # condition (-27.5%) still trips "fail" correctly. Widening therefore
    # trades sensitivity to a partial-load fault for fewer false alarms on
    # peptides that were never good load reporters. The vendor is generating the
    # multi-platform series that settles it; the intended end state is
    # per-peptide handling, where only mid-gradient peptides score recovery.
    peak_area_deviation_pct_warn: float = 15.0
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
    control_type: str = "PCD"
    notes: str = ""

    @field_validator("control_type")
    @classmethod
    def _valid_control_type(cls, v: str) -> str:
        return normalise_control_type(v)

    @field_validator("notes")
    @classmethod
    def _refresh_default_note(cls, v: str) -> str:
        return _LEGACY_RULE_NOTES.get(v, v)

    @field_validator("pattern")
    @classmethod
    def _nonempty_pattern(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pattern must be non-empty")
        return v.strip()


def default_peptide_classes() -> list[PeptideClassRule]:
    """Classes for the standard QC peptide panel, matched on Skyline's Protein column.

    Without a digest_efficiency rule digest efficiency is never calculated.
    The pair's protein is spelled two ways in templates in the field —
    "Miss-cleavage_pair" in the vendor's current report, "Miss-clevage_pair"
    in an earlier one — and a rule only matches its own spelling, so both are
    covered.
    """
    return [
        PeptideClassRule(
            protein_name="Non_reactive_Targets", label="Non-reactive targets", purpose="recovery"
        ),
        PeptideClassRule(
            protein_name="Miss-cleavage_pair", label="Miss-cleavage pair",
            purpose="digest_efficiency",
        ),
        PeptideClassRule(
            protein_name="Miss-clevage_pair", label="Miss-cleavage pair",
            purpose="digest_efficiency",
        ),
    ]


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
            ClassifierRule(pattern="SSC0",        control_type="SSC0",   notes="SSC-gold (gold standard)"),
            ClassifierRule(pattern="QCA",         control_type="PCD",    notes="Process Control Digest (pre-2026 name)"),
            ClassifierRule(pattern="QCB",         control_type="SSC",    notes="System Suitability Control (pre-2026 name)"),
            ClassifierRule(pattern="QC_A",        control_type="PCD",    notes="Process Control Digest (pre-2026 name)"),
            ClassifierRule(pattern="QC_B",        control_type="SSC",    notes="System Suitability Control (pre-2026 name)"),
            ClassifierRule(pattern="Hela_QC",     control_type="PCD",    notes="HeLa QC injection"),
            ClassifierRule(pattern="eb_inbetween",control_type="BLANK",  notes="Between-run blank"),
            ClassifierRule(pattern="BLANK",       control_type="BLANK",  notes="Blank injection"),
            ClassifierRule(pattern="BLK",         control_type="BLANK",  notes="Blank (short form)"),
        ]
    )
    peptide_classes: list[PeptideClassRule] = Field(default_factory=lambda: default_peptide_classes())
    qc_thresholds: QcThresholdsConfig = Field(default_factory=QcThresholdsConfig)
    # Per-SPD overrides of individual thresholds, keyed by SPD:
    #   [qc_thresholds_by_spd.500]
    #   rt_deviation_pct_max = 1.0
    # Anything not overridden comes from [qc_thresholds]. A 500 SPD gradient is
    # under half the length of a 200 SPD one, so a retention-time tolerance
    # that suits 200 SPD is too loose there.
    qc_thresholds_by_spd: dict[int, dict[str, float]] = Field(default_factory=dict)
    # "lenient" recognises a control-type marker anywhere in a filename,
    # including every pre-PCD spelling. "strict" recognises one only as the first
    # part of the name, where automated sample prep writes it — so a customer sample that
    # happens to contain PC or SSC can't be taken for a QC run. Custom
    # classifier_rules apply in both modes.
    classification_mode: Literal["lenient", "strict"] = "lenient"

    @field_validator("peptide_classes")
    @classmethod
    def _peptide_classes_default_when_empty(
        cls, v: list[PeptideClassRule]
    ) -> list[PeptideClassRule]:
        # Every Settings save before 1.2 wrote `peptide_classes = []`, so an
        # empty list means "never configured", not "configured to nothing".
        # The defaults only match two protein names; for any other panel they
        # do nothing.
        return v or default_peptide_classes()

    @field_validator("qc_thresholds_by_spd")
    @classmethod
    def _known_threshold_fields(
        cls, v: dict[int, dict[str, float]]
    ) -> dict[int, dict[str, float]]:
        for spd, overrides in v.items():
            unknown = sorted(set(overrides) - set(QC_THRESHOLD_FIELDS))
            if unknown:
                raise ValueError(
                    f"qc_thresholds_by_spd.{spd}: unknown threshold(s) {unknown}; "
                    f"valid names are {list(QC_THRESHOLD_FIELDS)}"
                )
        return v

    @model_validator(mode="after")
    def _per_spd_thresholds_are_coherent(self) -> Config:
        # Build each merged set now, so an override that makes warn exceed
        # fail is rejected at load rather than on the first run at that SPD.
        for spd in self.qc_thresholds_by_spd:
            self.thresholds_for(spd)
        return self

    def thresholds_for(self, spd: int | None) -> QcThresholdsConfig:
        """The thresholds that apply at this SPD: global values plus overrides."""
        overrides = self.qc_thresholds_by_spd.get(spd) if spd is not None else None
        if not overrides:
            return self.qc_thresholds
        merged = self.qc_thresholds.model_dump()
        merged.update(overrides)
        return QcThresholdsConfig(**merged)

    # ─── Cross-field validation ─────────────────────────────────────────────

    def cert_thumbprint_unsupported(self) -> str | None:
        """Returns an error message if cert auth is configured without a token.

        v1 does not support mTLS via cert store. Cert-configured deployments
        must fail loud, not silently fall through to local-only mode.
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
