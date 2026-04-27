"""Diagnostics page — web rendering of `mdqc doctor`."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mdqc.diagnostics import run_diagnostics
from mdqc.webui._deps import common_context, get_templates

router = APIRouter(prefix="/diagnostics")


@router.get("", response_class=HTMLResponse)
async def diagnostics_index(request: Request) -> HTMLResponse:
    report = await run_diagnostics(check_cloud=False)
    templates = get_templates(request)
    ctx = common_context(request)
    ctx["report"] = report
    return templates.TemplateResponse(request, "diagnostics/index.html", ctx)


@router.post("/refresh", response_class=HTMLResponse)
async def diagnostics_refresh(request: Request) -> HTMLResponse:
    report = await run_diagnostics(check_cloud=True)
    templates = get_templates(request)
    ctx = common_context(request)
    ctx["report"] = report
    return templates.TemplateResponse(request, "diagnostics/_report.html", ctx)


__all__ = ["router"]
