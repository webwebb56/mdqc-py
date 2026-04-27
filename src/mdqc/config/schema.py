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


class SkylineConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = "auto"
    timeout_seconds: int = defaults.SKYLINE_TIMEOUT_S
    process_priority: Literal["normal", "below_normal", "idle"] = "below_normal"


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
