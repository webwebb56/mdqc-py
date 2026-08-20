"""Settings page — view and edit config.toml from the browser."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from mdqc.config import defaults, paths
from mdqc.config.schema import (
    CONTROL_TYPE_VALUES,
    QC_THRESHOLD_FIELDS,
    AgentConfig,
    ClassifierRule,
    CloudConfig,
    Config,
    InstrumentConfig,
    QcThresholdsConfig,
)
from mdqc.extractor.skyline import find_skyline
from mdqc.types import Vendor
from mdqc.webui._deps import common_context, get_state, get_templates

log = logging.getLogger(__name__)

router = APIRouter()

VENDORS = [v.value for v in Vendor]
LOG_LEVELS = ["error", "warn", "info", "debug", "trace"]
RETENTION_LOW_WATERMARK = 100
"""Warn below this many retained payloads — under one night of acquisition."""
PRIORITIES = ["normal", "below_normal", "idle"]
CONTROL_TYPES = CONTROL_TYPE_VALUES


@dataclass
class SectionStatus:
    state: str  # "ok" | "bad" | "muted"
    message: str


def _status_instrument(inst: InstrumentConfig) -> SectionStatus:
    try:
        accessible = inst.watch_path.exists() and inst.watch_path.is_dir()
    except OSError:
        accessible = False
    if not accessible:
        return SectionStatus("bad", f"Watch path not accessible: {inst.watch_path}")
    return SectionStatus("ok", "Watch path accessible")


def _status_template(inst: InstrumentConfig) -> SectionStatus:
    name = inst.template
    candidate = Path(name)
    if candidate.is_absolute():
        if candidate.exists():
            return SectionStatus("ok", str(candidate))
        return SectionStatus("bad", f"Not found: {candidate}")
    for base in (paths.methods_dir(), paths.templates_dir()):
        p = base / name
        if p.exists():
            return SectionStatus("ok", str(p))
    return SectionStatus("bad", f"Not found: {name}")


def _status_skyline(cfg: Config) -> SectionStatus:
    explicit = None
    if cfg.skyline.path and cfg.skyline.path.lower() != "auto":
        explicit = Path(cfg.skyline.path)
    found = find_skyline(explicit=explicit)
    if found is None:
        return SectionStatus("bad", "SkylineCmd.exe not found")
    return SectionStatus("ok", str(found))


def _status_cloud(cfg: Config) -> SectionStatus:
    if cfg.cloud.certificate_thumbprint and not cfg.cloud.api_token:
        return SectionStatus("bad", "Certificate thumbprint set but mTLS not supported in v1 — add an API token")
    if cfg.cloud.api_token:
        return SectionStatus("ok", "Bearer token configured")
    return SectionStatus("muted", "Local-only (no upload)")


def _parse_float(raw: Any, fallback: float) -> float:
    """Parse a form percentage, falling back to the current value when unusable."""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


THRESHOLD_GROUPS: list[dict[str, Any]] = [
    {
        "title": "Target extraction",
        "question": (
            "Was the correct peak measured? A wrongly integrated peak and a genuine "
            "performance drop look identical in peak area alone."
        ),
        "fields": [
            ("rt_deviation_pct_max", "Retention time deviation",
             "Beyond this, the peak may not be the intended target."),
            ("dot_product_deviation_pct_max", "Dot product deviation — normal",
             "Within this, extraction is considered sound."),
            ("dot_product_deviation_pct_suspect", "Dot product deviation — suspect",
             "Beyond this, combined with retention-time drift, flags a wrong peak."),
            ("peak_area_deviation_pct_suspect", "Peak area deviation — suspect",
             "Alternative corroborating signal for a wrong peak."),
        ],
    },
    {
        "title": "Signal level",
        "question": (
            "Is the run performing? The response is buffered — a modest change in peak "
            "area can reflect a much larger change in material reaching the column."
        ),
        "fields": [
            ("peak_area_deviation_pct_warn", "Warn at",
             "Roughly a 25% reduction in material reaching the column."),
            ("peak_area_deviation_pct_fail", "Fail at",
             "Roughly a 50% reduction in material reaching the column."),
        ],
    },
]


def _thresholds_ctx(cfg: Config) -> list[dict[str, Any]]:
    """Render-ready threshold groups with current and shipped values."""
    shipped = QcThresholdsConfig()
    groups = []
    for group in THRESHOLD_GROUPS:
        fields = [
            {
                "name": name,
                "label": label,
                "hint": hint,
                "value": getattr(cfg.qc_thresholds, name),
                "default": getattr(shipped, name),
            }
            for name, label, hint in group["fields"]
        ]
        groups.append({**group, "fields": fields})
    return groups


def _parse_int(raw: Any, fallback: int) -> int:
    """Parse a form integer, falling back to the current value when unusable."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


def _cloud_environment(endpoint: str) -> str:
    """Classify a saved endpoint as one of the named presets, or 'custom'."""
    if endpoint == defaults.ENDPOINT_DEV:
        return "dev"
    if endpoint == defaults.ENDPOINT_PROD:
        return "prod"
    return "custom"


def _instruments_ctx(instruments: list[InstrumentConfig]) -> list[dict[str, Any]]:
    result = []
    for i, inst in enumerate(instruments):
        result.append({
            "idx": i,
            "inst": inst,
            "status": _status_instrument(inst),
            "status_template": _status_template(inst),
        })
    return result


def _parse_classifier_rules(form: dict[str, Any]) -> list[ClassifierRule]:
    indices: set[int] = set()
    for key in form:
        for prefix in ("rule_pattern_", "rule_type_", "rule_notes_"):
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                if suffix.isdigit():
                    indices.add(int(suffix))

    rules = []
    for idx in sorted(indices):
        pattern = str(form.get(f"rule_pattern_{idx}", "")).strip()
        control_type = str(form.get(f"rule_type_{idx}", "QC_A"))
        notes = str(form.get(f"rule_notes_{idx}", "")).strip()
        if not pattern:
            continue
        if control_type not in CONTROL_TYPES:
            control_type = "QC_A"
        rules.append(ClassifierRule(pattern=pattern, control_type=control_type, notes=notes))
    return rules


def _settings_context(
    cfg: Config, saved: bool = False, error: str | None = None, cloud_changed: bool = False
) -> dict[str, Any]:
    return {
        "cfg": cfg,
        "instruments_ctx": _instruments_ctx(cfg.instruments),
        "vendors": VENDORS,
        "log_levels": LOG_LEVELS,
        "priorities": PRIORITIES,
        "control_types": CONTROL_TYPES,
        "status_skyline": _status_skyline(cfg),
        "status_cloud": _status_cloud(cfg),
        "cloud_environment": _cloud_environment(cfg.cloud.endpoint),
        "endpoint_dev": defaults.ENDPOINT_DEV,
        "endpoint_prod": defaults.ENDPOINT_PROD,
        # Drives the spool-retention copy: local-only means completed/ holds
        # the only copy of every payload and is pruned by age, not by count.
        "local_only": cfg.is_local_only(),
        # A count this low cannot survive one night of acquisition. Warn even
        # in local-only mode, where the cap is currently dormant — it goes
        # live the moment an API token is saved, and a value persisted from
        # an older install (the default used to be 10) would start deleting
        # payloads with no further warning.
        "retention_low": cfg.spool.completed_retention_count < RETENTION_LOW_WATERMARK,
        "retention_low_watermark": RETENTION_LOW_WATERMARK,
        "threshold_groups": _thresholds_ctx(cfg),
        "thresholds_are_default": cfg.qc_thresholds.is_default(),
        "saved": saved,
        "error": error,
        "cloud_changed": cloud_changed,
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


def _parse_instruments(form: dict[str, Any]) -> list[InstrumentConfig]:
    # Collect all instrument indices from field names like instrument_id_0, vendor_1, etc.
    indices: set[int] = set()
    for key in form:
        for prefix in ("instrument_id_", "vendor_", "watch_path_", "file_pattern_", "template_"):
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                if suffix.isdigit():
                    indices.add(int(suffix))

    instruments = []
    for idx in sorted(indices):
        raw_id = str(form.get(f"instrument_id_{idx}", "")).strip()
        raw_vendor = str(form.get(f"vendor_{idx}", "thermo"))
        raw_path = str(form.get(f"watch_path_{idx}", "")).strip()
        raw_pattern = str(form.get(f"file_pattern_{idx}", "*")).strip()
        raw_template = str(form.get(f"template_{idx}", "QC_Method.sky")).strip()

        if not raw_id and not raw_path:
            continue  # skip completely empty rows

        instruments.append(InstrumentConfig(
            id=raw_id or f"instrument-{idx + 1}",
            vendor=Vendor(raw_vendor if raw_vendor in VENDORS else "thermo"),
            watch_path=Path(raw_path or "."),
            file_pattern=raw_pattern or "*",
            template=raw_template or "QC_Method.sky",
        ))

    return instruments


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    state = get_state(request)
    ctx = common_context(request)
    ctx.update(_settings_context(state.cfg))
    return get_templates(request).TemplateResponse(request, "settings/index.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request) -> HTMLResponse:
    state = get_state(request)
    error: str | None = None
    cloud_changed = False

    try:
        raw_form = await request.form()
        form: dict[str, Any] = dict(raw_form)

        log_level = str(form.get("log_level", "info"))
        if log_level not in LOG_LEVELS:
            log_level = "info"
        skyline_priority = str(form.get("skyline_priority", "below_normal"))
        if skyline_priority not in PRIORITIES:
            skyline_priority = "below_normal"

        skyline_path = str(form.get("skyline_path", "auto")).strip() or "auto"
        skyline_timeout = int(form.get("skyline_timeout", 900) or 900)
        api_token = str(form.get("api_token", "")).strip() or None
        cloud_environment = str(form.get("cloud_environment", "dev"))
        if cloud_environment == "prod":
            cloud_endpoint = defaults.ENDPOINT_PROD
        elif cloud_environment == "custom":
            cloud_endpoint = str(form.get("cloud_endpoint_custom", "")).strip() or defaults.DEFAULT_ENDPOINT
        else:
            cloud_endpoint = defaults.ENDPOINT_DEV
        enable_toasts = "enable_toasts" in form

        instruments = _parse_instruments(form)
        classifier_rules = _parse_classifier_rules(form)

        completed_retention = _parse_int(
            form.get("completed_retention_count"),
            state.cfg.spool.completed_retention_count,
        )
        max_age_days = _parse_int(
            form.get("max_age_days"), state.cfg.spool.max_age_days
        )

        # "Restore recommended values" resets to the shipped defaults rather
        # than to whatever was last saved — the point of the button is to get
        # back to Evosep's published numbers from an unknown state.
        if "restore_thresholds" in form:
            qc_thresholds = QcThresholdsConfig()
        else:
            qc_thresholds = QcThresholdsConfig(
                **{
                    name: _parse_float(
                        form.get(name), getattr(state.cfg.qc_thresholds, name)
                    )
                    for name in QC_THRESHOLD_FIELDS
                }
            )

        # Rebuild from the CURRENT config, overriding only what this form
        # actually renders. Constructing bare SkylineConfig()/SpoolConfig()/
        # Config() here would silently reset every field without a form
        # control — report_skyr_path, collapse_transitions_to_peptides,
        # peptide_classes, and the watcher block — so an operator following
        # the docs to paste in a cloud token would also lose their custom
        # .skyr and their digest-efficiency peptide classes.
        prev = state.cfg
        cfg = Config(
            agent=AgentConfig(log_level=log_level, enable_toast_notifications=enable_toasts),
            cloud=CloudConfig(endpoint=cloud_endpoint, api_token=api_token),
            skyline=prev.skyline.model_copy(
                update={
                    "path": skyline_path,
                    "timeout_seconds": skyline_timeout,
                    "process_priority": skyline_priority,
                }
            ),
            watcher=prev.watcher,
            spool=prev.spool.model_copy(
                update={
                    "completed_retention_count": completed_retention,
                    "max_age_days": max_age_days,
                }
            ),
            instruments=instruments,
            classifier_rules=classifier_rules,
            peptide_classes=prev.peptide_classes,
            qc_thresholds=qc_thresholds,
        )

        # The running Uploader was built once at startup from a snapshot of
        # cloud config — reassigning state.cfg below does not reach it. Flag
        # the change so the UI can tell the operator a restart is needed,
        # rather than silently leaving payloads un-pushed after they've
        # entered a token and been told "saved successfully".
        cloud_changed = (
            state.cfg.cloud.endpoint != cfg.cloud.endpoint
            or state.cfg.cloud.api_token != cfg.cloud.api_token
        )

        _write_config(cfg)
        state.cfg = cfg
        log.info("settings_saved", extra={"path": str(paths.config_path()), "instruments": len(instruments)})

    except ValidationError as exc:
        # Surface the rule that was broken, not pydantic's full report — the
        # threshold ordering checks exist to be read by an operator.
        log.warning("settings_save_failed", extra={"error": str(exc)})
        error = "; ".join(
            str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors()
        ) or str(exc)
    except Exception as exc:
        log.warning("settings_save_failed", extra={"error": str(exc)})
        error = str(exc)

    ctx = common_context(request)
    ctx.update(_settings_context(
        state.cfg, saved=error is None, error=error,
        cloud_changed=cloud_changed and error is None,
    ))
    return get_templates(request).TemplateResponse(request, "settings/index.html", ctx)


@router.post("/settings/reset-processed", response_class=HTMLResponse)
async def reset_processed(request: Request) -> HTMLResponse:
    """Clear the processed-files registry so previously-handled paths are re-extracted."""
    state = get_state(request)
    registry = getattr(state, "processed_registry", None)
    count = 0
    if registry is not None:
        try:
            count = len(registry)
            registry.clear()
            log.info("processed_registry_cleared", extra={"entries_removed": count})
        except Exception as exc:
            log.warning("processed_registry_clear_failed", extra={"error": str(exc)})
            return HTMLResponse(
                f'<span class="status-msg bad">Failed: {exc}</span>',
                status_code=500,
            )
    return HTMLResponse(
        f'<span class="status-msg ok">Cleared ({count} {"entry" if count == 1 else "entries"} removed)</span>'
    )


@router.get("/settings/open-template")
async def open_template(name: str = Query(...)) -> JSONResponse:
    """Resolve a template name and open it in the system default app (Skyline GUI)."""
    candidate = Path(name)
    resolved: Path | None = None
    if candidate.is_absolute():
        if candidate.exists():
            resolved = candidate
    else:
        for base in (paths.methods_dir(), paths.templates_dir()):
            p = base / name
            if p.exists():
                resolved = p
                break

    if resolved is None:
        return JSONResponse({"ok": False, "error": f"Template not found: {name}"}, status_code=404)

    try:
        os.startfile(str(resolved))  # Windows: opens with registered app (Skyline)
        return JSONResponse({"ok": True, "path": str(resolved)})
    except Exception as exc:
        log.warning(
            "open_template_failed",
            extra={"path": str(resolved), "error": str(exc)},
        )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


__all__ = ["router"]
