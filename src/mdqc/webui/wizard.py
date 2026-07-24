"""First-run setup wizard.

5 steps: vendor → instrument & path → Skyline → template → output mode.
Session storage is a process-local dict keyed by the wizard session token
(`mdqc_session` cookie); fine for v1 since the wizard runs once on a single
machine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import tomli_w
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
from mdqc.extractor import find_skyline
from mdqc.types import Vendor
from mdqc.webui._deps import common_context, get_state, get_templates

log = logging.getLogger(__name__)

router = APIRouter()

VENDORS: list[str] = [v.value for v in Vendor]
STEP_TITLES: list[str] = ["Vendor", "Instrument", "Skyline", "Template", "Output"]
TOTAL_STEPS: int = 5

COMMON_VENDOR_PATHS: dict[str, list[str]] = {
    "thermo": [r"D:\Data", r"D:\Xcalibur\Data", r"C:\Xcalibur\Data"],
    "bruker": [r"D:\Data", r"D:\Bruker\Data"],
    "sciex": [r"D:\Analyst Data", r"D:\Sciex Data"],
    "waters": [r"D:\MassLynx\Data", r"D:\Waters\Data"],
    "agilent": [r"D:\MassHunter\Data", r"D:\Agilent\Data"],
}

COMMON_SKYLINE_PATHS: list[str] = [
    r"C:\Program Files\Skyline\SkylineCmd.exe",
    r"C:\Program Files (x86)\Skyline\SkylineCmd.exe",
]

_SESSIONS: dict[str, dict[str, Any]] = {}


def _session_key(request: Request) -> str:
    return request.cookies.get("mdqc_session") or "anonymous"


def _session(request: Request) -> dict[str, Any]:
    key = _session_key(request)
    if key not in _SESSIONS:
        _SESSIONS[key] = {}
    return _SESSIONS[key]


def _step_context(request: Request, step: int) -> dict[str, Any]:
    data = _session(request)
    ctx: dict[str, Any] = {
        "step": step,
        "step_titles": STEP_TITLES,
        "vendors": VENDORS,
        "data": data,
    }
    if step == 2:
        vendor = data.get("vendor", "thermo")
        ctx["suggested_paths"] = COMMON_VENDOR_PATHS.get(vendor, [])
    elif step == 3:
        ctx["detected_skyline"] = find_skyline()
        ctx["common_skyline_paths"] = COMMON_SKYLINE_PATHS
    elif step == 4:
        bundled = paths.methods_dir() / "QC_Method.sky"
        ctx["bundled_template"] = str(bundled)
    return ctx


def _has_instruments(state: Any) -> bool:
    cfg = getattr(state, "cfg", None)
    return bool(cfg and cfg.instruments)


@router.get("/wizard", response_class=HTMLResponse)
async def wizard_index(request: Request) -> Any:
    state = get_state(request)
    if _has_instruments(state):
        return RedirectResponse(url="/dashboard", status_code=303)
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, 1))
    return templates.TemplateResponse(request, "wizard/index.html", ctx)


@router.get("/wizard/step/{n}", response_class=HTMLResponse)
async def wizard_step(request: Request, n: int) -> HTMLResponse:
    n = max(1, min(TOTAL_STEPS, n))
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, n))
    return templates.TemplateResponse(request, f"wizard/step_{n}.html", ctx)


@router.post("/wizard/step/1", response_class=HTMLResponse)
async def wizard_step_1(request: Request, vendor: str = Form(...)) -> HTMLResponse:
    if vendor not in VENDORS:
        vendor = "thermo"
    _session(request)["vendor"] = vendor
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, 2))
    return templates.TemplateResponse(request, "wizard/step_2.html", ctx)


@router.post("/wizard/step/2", response_class=HTMLResponse)
async def wizard_step_2(
    request: Request,
    instrument_id: str = Form(...),
    watch_path: str = Form(...),
) -> HTMLResponse:
    sess = _session(request)
    sess["instrument_id"] = instrument_id.strip()
    sess["watch_path"] = watch_path.strip()
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, 3))
    return templates.TemplateResponse(request, "wizard/step_3.html", ctx)


@router.post("/wizard/step/3", response_class=HTMLResponse)
async def wizard_step_3(
    request: Request, skyline_path: str = Form("")
) -> HTMLResponse:
    _session(request)["skyline_path"] = skyline_path.strip()
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, 4))
    return templates.TemplateResponse(request, "wizard/step_4.html", ctx)


@router.post("/wizard/step/4", response_class=HTMLResponse)
async def wizard_step_4(
    request: Request, template_path: str = Form("")
) -> HTMLResponse:
    _session(request)["template_path"] = template_path.strip()
    templates = get_templates(request)
    ctx = common_context(request)
    ctx.update(_step_context(request, 5))
    return templates.TemplateResponse(request, "wizard/step_5.html", ctx)


@router.post("/wizard/step/5", response_class=HTMLResponse)
async def wizard_step_5(
    request: Request,
    output_mode: str = Form("cloud"),
    cloud_environment: str = Form("dev"),
    api_token: str = Form(""),
) -> HTMLResponse:
    sess = _session(request)
    sess["output_mode"] = output_mode
    sess["cloud_environment"] = cloud_environment if cloud_environment in ("dev", "prod") else "dev"
    sess["api_token"] = api_token.strip()
    return await wizard_save(request)


def _build_config(data: dict[str, Any]) -> Config:
    vendor_str = data.get("vendor", "thermo")
    vendor = Vendor(vendor_str)
    instrument_id = data.get("instrument_id") or "instrument-1"
    watch_path = data.get("watch_path") or "."
    template = data.get("template_path") or "QC_Method.sky"
    skyline_path = data.get("skyline_path") or "auto"
    output_mode = data.get("output_mode", "cloud")
    cloud_environment = data.get("cloud_environment", "dev")
    api_token = data.get("api_token") or None

    instrument = InstrumentConfig(
        id=instrument_id,
        vendor=vendor,
        watch_path=Path(watch_path),
        file_pattern="*",
        template=template,
    )
    endpoint = defaults.ENDPOINT_PROD if cloud_environment == "prod" else defaults.ENDPOINT_DEV
    cloud = CloudConfig(
        endpoint=endpoint,
        api_token=api_token if output_mode == "cloud" else None,
    )
    return Config(
        agent=AgentConfig(),
        cloud=cloud,
        skyline=SkylineConfig(path=skyline_path or "auto"),
        watcher=WatcherConfig(),
        spool=SpoolConfig(),
        instruments=[instrument],
    )


def _serialize_config(cfg: Config) -> dict[str, Any]:
    raw = cfg.model_dump(mode="json", exclude_none=True)
    instruments = raw.pop("instruments", [])
    raw["instruments"] = instruments
    return raw


@router.post("/wizard/save", response_class=HTMLResponse)
async def wizard_save(request: Request) -> HTMLResponse:
    sess = _session(request)
    cfg = _build_config(sess)
    target = paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    payload = _serialize_config(cfg)
    tmp.write_bytes(tomli_w.dumps(payload).encode("utf-8"))
    import os

    os.replace(tmp, target)

    log.info("wizard_saved", extra={"path": str(target)})
    state = get_state(request)
    reload_fn = getattr(state, "reload_config", None)
    if callable(reload_fn):
        try:
            reload_fn()
        except Exception as exc:
            log.warning("wizard_reload_failed", extra={"error": str(exc)})

    templates = get_templates(request)
    ctx = common_context(request)
    ctx["config_path"] = str(target)
    return templates.TemplateResponse(request, "wizard/saved.html", ctx)


__all__ = ["STEP_TITLES", "TOTAL_STEPS", "VENDORS", "router"]
