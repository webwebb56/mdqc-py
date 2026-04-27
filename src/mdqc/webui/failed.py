"""Failed-files manager: view, retry, clear."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mdqc.webui._deps import common_context, get_state, get_templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/failed")


def _entries(state: Any) -> list[Any]:
    failed = getattr(state, "failed", None)
    if failed is None:
        return []
    entries = getattr(failed, "entries", None)
    if entries is None:
        return []
    return list(entries)


def _render_table(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    state = get_state(request)
    ctx = common_context(request)
    ctx["entries"] = _entries(state)
    return templates.TemplateResponse(request, "failed/_table.html", ctx)


@router.get("", response_class=HTMLResponse)
async def failed_index(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    state = get_state(request)
    ctx = common_context(request)
    ctx["entries"] = _entries(state)
    return templates.TemplateResponse(request, "failed/index.html", ctx)


@router.post("/retry/{path:path}", response_class=HTMLResponse)
async def failed_retry(request: Request, path: str) -> HTMLResponse:
    state = get_state(request)
    failed = getattr(state, "failed", None)
    if failed is not None:
        try:
            failed.increment_retry(path)
        except Exception as exc:
            log.warning("failed_retry_increment_failed", extra={"error": str(exc)})
        retry_fn = getattr(state, "retry_failed", None)
        if callable(retry_fn):
            try:
                retry_fn(path)
            except Exception as exc:
                log.warning("failed_retry_dispatch_failed", extra={"error": str(exc)})
    return _render_table(request)


@router.post("/clear", response_class=HTMLResponse)
async def failed_clear(request: Request) -> HTMLResponse:
    state = get_state(request)
    failed = getattr(state, "failed", None)
    if failed is not None:
        try:
            failed.clear()
        except Exception as exc:
            log.warning("failed_clear_failed", extra={"error": str(exc)})
    return _render_table(request)


__all__ = ["router"]
