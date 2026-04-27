"""Config loading entry point.

Public surface: load_config(path) → Config, plus re-exports of submodules
so callers can do `from mdqc.config import paths, defaults`.
"""

from __future__ import annotations

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


CONFIG_EXIT_CODE = 78  # POSIX EX_CONFIG; matches AGENT_NOTES.


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
