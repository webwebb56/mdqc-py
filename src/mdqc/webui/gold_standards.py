"""Gold Standards page: SSC0 run review and per-(instrument, SPD) baseline selection.

Read/write against mdqc.gold_standards (agent-local storage, separate from
spool/completed). Does not touch QcPayload.baseline_context — see that
module's docstring for why the payload wiring is a later step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mdqc import gold_standards as gs
from mdqc.webui._deps import common_context, get_state, get_templates

router = APIRouter(prefix="/gold-standards")


def _instrument_ids(cfg: Any) -> list[str]:
    return [i.id for i in cfg.instruments]


def _resolve_instrument(requested: str | None, cfg: Any) -> str | None:
    ids = _instrument_ids(cfg)
    if requested and requested in ids:
        return requested
    return ids[0] if ids else None


def _parse_spd(raw: Any) -> int | None:
    if raw in (None, "", "unknown"):
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def _resolve_spd(requested: str | None, available: list[int]) -> int | None:
    parsed = _parse_spd(requested)
    if parsed is not None and parsed in available:
        return parsed
    return available[0] if available else None


async def _page_context(
    request: Request,
    instrument_id: str | None,
    spd: int | None,
    saved: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    state = get_state(request)
    cfg = state.cfg
    ctx = common_context(request)

    runs = gs.list_ssc0_runs(instrument_id, spd) if instrument_id else []
    active = gs.get_active_baseline(instrument_id, spd) if instrument_id else None
    default_checked = (
        set(active["source_run_ids"]) if active else {r["run_id"] for r in runs}
    )

    # The heatmap shades against the same warn/fail percentages that decide a
    # run's payload verdict. Two sets of numbers for the same judgement is how
    # a page ends up disagreeing with the verdict it sits next to.
    th = cfg.qc_thresholds
    ctx.update(
        {
            "instruments": _instrument_ids(cfg),
            "instrument_id": instrument_id,
            "available_spds": gs.list_available_spds(instrument_id) if instrument_id else [],
            "spd": spd,
            "runs_data": runs,
            "default_checked": sorted(default_checked),
            "active_baseline": active,
            "baseline_reference": (active or {}).get("per_peptide") or {},
            "dev_warn_pct": th.peak_area_deviation_pct_warn,
            "dev_fail_pct": th.peak_area_deviation_pct_fail,
            "rt_dev_pct_max": th.rt_deviation_pct_max,
            "saved": saved,
            "error": error,
        }
    )
    return ctx


@router.get("", response_class=HTMLResponse)
async def gold_standards_index(request: Request) -> HTMLResponse:
    state = get_state(request)
    cfg = state.cfg
    instrument_id = _resolve_instrument(request.query_params.get("instrument"), cfg)
    available_spds = gs.list_available_spds(instrument_id) if instrument_id else []
    spd = _resolve_spd(request.query_params.get("spd"), available_spds)

    templates = get_templates(request)
    ctx = await _page_context(request, instrument_id, spd)
    return templates.TemplateResponse(request, "gold_standards/index.html", ctx)


@router.post("/save", response_class=HTMLResponse)
async def gold_standards_save(request: Request) -> HTMLResponse:
    state = get_state(request)
    cfg = state.cfg
    form = await request.form()

    instrument_id = str(form.get("instrument_id") or "") or None
    spd = _parse_spd(form.get("spd"))
    label = str(form.get("label") or "")
    run_ids = [str(r) for r in form.getlist("run_id")]

    error: str | None = None
    saved = False
    if not run_ids:
        error = "Select at least one SSC0 run before saving a baseline."
    else:
        gs.save_baseline(instrument_id, spd, run_ids, label, cfg.peptide_classes)
        saved = True

    templates = get_templates(request)
    ctx = await _page_context(request, instrument_id, spd, saved=saved, error=error)
    return templates.TemplateResponse(request, "gold_standards/index.html", ctx)


__all__ = ["router"]
