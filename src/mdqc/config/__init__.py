"""Config loading entry point.

Public surface: load_config(path) → Config, plus re-exports of submodules
so callers can do `from mdqc.config import paths, defaults`.
"""

from __future__ import annotations

import logging
import sys
import tomllib
from pathlib import Path

from mdqc.config import defaults, paths
from mdqc.config.schema import (
    AgentConfig,
    CloudConfig,
    Config,
    InstrumentConfig,
    SkylineConfig,
    SpoolConfig,
    WatcherConfig,
)


class ConfigError(RuntimeError):
    """Raised when config.toml is missing, malformed, or invalid."""


log = logging.getLogger(__name__)

CONFIG_EXIT_CODE = 78  # POSIX EX_CONFIG


def load_config(path: Path | None = None, *, strict_cert_guard: bool = True) -> Config:
    """Load + validate config.toml.

    Args:
        path: explicit path; defaults to MDQC_CONFIG env or paths.config_path().
        strict_cert_guard: if True, raise ConfigError when certificate_thumbprint
            is set without api_token (v1 fail-fast). Disabled in tests that need
            to load cert-configured fixtures without exiting.
    """
    cfg_path = path or paths.config_path()
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    try:
        with cfg_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML parse error in {cfg_path}: {e}") from e

    try:
        cfg = Config.model_validate(raw)
    except Exception as e:  # pydantic ValidationError + others
        raise ConfigError(f"Config validation failed: {e}") from e

    saved_endpoint = str((raw.get("cloud") or {}).get("endpoint") or "").strip()
    if saved_endpoint and saved_endpoint != cfg.cloud.endpoint:
        # Loud, because the file on disk still says the old thing.
        log.warning(
            "cloud_endpoint_migrated",
            extra={"saved": saved_endpoint, "using": cfg.cloud.endpoint},
        )

    # Pre-1.3 rules say QC_A / QC_B. They are translated on load; the file
    # keeps the old values until the next save from the Settings page.
    saved_rules = raw.get("classifier_rules") or []
    renamed = [
        f"{r.get('control_type')} -> {healed.control_type}"
        for r, healed in zip(saved_rules, cfg.classifier_rules, strict=False)
        if isinstance(r, dict) and r.get("control_type") != healed.control_type
    ]
    if renamed:
        log.warning(
            "classifier_rules_migrated",
            extra={"count": len(renamed), "changes": sorted(set(renamed))},
        )

    if strict_cert_guard:
        msg = cfg.cert_thumbprint_unsupported()
        if msg:
            raise ConfigError(msg)

    return cfg


def load_or_exit(path: Path | None = None) -> Config:
    """Used by CLI entry points: print to stderr + exit 78 on error."""
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"mdqc: config error: {e}", file=sys.stderr)
        sys.exit(CONFIG_EXIT_CODE)


__all__ = [
    "CONFIG_EXIT_CODE",
    "AgentConfig",
    "CloudConfig",
    "Config",
    "ConfigError",
    "InstrumentConfig",
    "SkylineConfig",
    "SpoolConfig",
    "WatcherConfig",
    "defaults",
    "load_config",
    "load_or_exit",
    "paths",
]
