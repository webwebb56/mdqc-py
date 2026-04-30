"""Settings page — view and edit config.toml from the browser."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mdqc.config import paths
from mdqc.config.schema import (
    AgentConfig,
    CloudConfig,
    Config,
    InstrumentConfig,
    SkylineConfig,
    SpoolConfig,
    WatcherConfig,
)
from mdqc.extractor.skyline import find_skyline
from mdqc.types import Vendor
from mdqc.webui._deps import common_context, get_state, get_templates

log = logging.getLogger(__name__)

router = APIRouter()

VENDORS = [v.value for v in Vendor]
LOG_LEVELS = ["error", "warn", "info", "debug", "trace"]
PRIORITIES = ["normal", "below_normal", "idle"]


@dataclass
class SectionStatus:
    state: str  # "ok" | "bad" | "muted"
    message: str

    @property
    def dot(self) -> str:
        return "●"


def _status_instrument(inst: InstrumentConfig | None) -> SectionStatus:
    if inst is None:
        return SectionStatus("bad", "No instrument configured")
    try:
        accessible = inst.watch_path.exists() and inst.watch_path.is_dir()
    except OSError:
        accessible = False
    if not accessible:
        return SectionStatus("bad", f"Watch path not accessible: {inst.watch_path}")
    return SectionStatus("ok", "Watch path accessible")


def _status_skyline(cfg: Config) -> SectionStatus:
    explicit = None
    if cfg.skyline.path and cfg.skyline.path.lower() != "auto":
        explicit = Path(cfg.skyline.path)
    found = find_skyline(explicit=explicit)
    if found is None:
        return SectionStatus("bad", "SkylineCmd.exe not found")
    return SectionStatus("ok", str(found))


def _status_template(inst: InstrumentConfig | None) -> SectionStatus:
    if inst is None:
        return SectionStatus("bad", "No instrument configured")
    name = inst.template
    candidate = Path(name)
    if candidate.is_absolute():
        if candidate.exists():
            return SectionStatus("ok", str(candidate))
        return SectionStatus("bad", f"Template not found: {candidate}")
    for base in (paths.methods_dir(), paths.templates_dir()):
        p = base / name
        if p.exists():
            return SectionStatus("ok", str(p))
    return SectionStatus("bad", f"Template not found: {name}")


def _status_cloud(cfg: Config) -> SectionStatus:
    if cfg.cloud.certificate_thumbprint and not cfg.cloud.api_token:
        return SectionStatus("bad", "Certificate thumbprint set but mTLS not supported in v1 — add an API token")
    if cfg.cloud.api_token:
        return SectionStatus("ok", "Bearer token configured")
    return SectionStatus("muted", "Local-only (no upload)")


def _settings_context(cfg: Config, saved: bool = False, error: str | None = None) -> dict[str, Any]:
    inst = cfg.instruments[0] if cfg.instruments else None
    return {
        "cfg": cfg,
        "inst": inst,
        "vendors": VENDORS,
        "log_levels": LOG_LEVELS,
        "priorities": PRIORITIES,
        "status_instrument": _status_instrument(inst),
        "status_skyline": _status_skyline(cfg),
        "status_template": _status_template(inst),
        "status_cloud": _status_cloud(cfg),
        "saved": saved,
        "error": error,
        "config_path": str(paths.config_path()),
    }


def _write_config(cfg: Config) -> None:
    target = paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = cfg.model_dump(mode="json", exclude_none=True)
    instruments = raw.pop("instruments", [])
    raw["instruments"] = instruments
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(tomli_w.dumps(raw).encode("utf-8"))
    os.replace(tmp, target)


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    state = get_state(request)
    ctx = common_context(request)
    ctx.update(_settings_context(state.cfg))
    return get_templates(request).TemplateResponse(request, "settings/index.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    # Instrument
    instrument_id: str = Form(""),
    vendor: str = Form("thermo"),
    watch_path: str = Form(""),
    file_pattern: str = Form("*"),
    template: str = Form("QC_Method.sky"),
    # Skyline
    skyline_path: str = Form("auto"),
    skyline_timeout: int = Form(900),
    skyline_priority: str = Form("below_normal"),
    # Cloud
    cloud_endpoint: str = Form(""),
    api_token: str = Form(""),
    # Agent
    log_level: str = Form("info"),
    enable_toasts: bool = Form(False),
) -> HTMLResponse:
    state = get_state(request)
    error: str | None = None

    try:
        if vendor not in VENDORS:
            vendor = "thermo"
        if log_level not in LOG_LEVELS:
            log_level = "info"
        if skyline_priority not in PRIORITIES:
            skyline_priority = "below_normal"

        instrument_id = instrument_id.strip() or "instrument-1"
        watch_path = watch_path.strip() or "."
        template = template.strip() or "QC_Method.sky"
        skyline_path = skyline_path.strip() or "auto"
        api_token = api_token.strip() or None
        cloud_endpoint = cloud_endpoint.strip() or "https://qc-ingest.massdynamics.com/v1/"

        instrument = InstrumentConfig(
            id=instrument_id,
            vendor=Vendor(vendor),
            watch_path=Path(watch_path),
            file_pattern=file_pattern.strip() or "*",
            template=template,
        )

        cfg = Config(
            agent=AgentConfig(log_level=log_level, enable_toast_notifications=enable_toasts),
            cloud=CloudConfig(endpoint=cloud_endpoint, api_token=api_token),
            skyline=SkylineConfig(
                path=skyline_path,
                timeout_seconds=skyline_timeout,
                process_priority=skyline_priority,
            ),
            watcher=WatcherConfig(),
            spool=SpoolConfig(),
            instruments=[instrument],
        )

        _write_config(cfg)
        state.cfg = cfg
        log.info("settings_saved", extra={"path": str(paths.config_path())})

    except Exception as exc:
        log.warning("settings_save_failed", extra={"error": str(exc)})
        error = str(exc)

    ctx = common_context(request)
    ctx.update(_settings_context(state.cfg, saved=error is None, error=error))
    return get_templates(request).TemplateResponse(request, "settings/index.html", ctx)


__all__ = ["router"]
