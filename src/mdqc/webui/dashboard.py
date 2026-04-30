"""Dashboard routes: status, queue counts, recent activity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mdqc.config import paths
from mdqc.webui._deps import common_context, get_state, get_templates

router = APIRouter()

STREAMLIT_PORT = 8501
_STREAMLIT_HEALTH_URL = f"http://127.0.0.1:{STREAMLIT_PORT}/_stcore/health"


def _queue_counts(state: Any) -> dict[str, int]:
    pending = uploading = failed = completed = 0
    spool = getattr(state, "spool", None)
    if spool is not None:
        try:
            pending = sum(
                1
                for p in spool.pending_dir.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        except (OSError, AttributeError):
            pending = 0
        try:
            uploading = sum(
                1
                for p in spool.uploading_dir.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        except (OSError, AttributeError):
            uploading = 0
        try:
            completed = sum(
                1
                for p in spool.completed_dir.iterdir()
                if p.is_file() and p.suffix == ".json"
            )
        except (OSError, AttributeError):
            completed = 0
    failed_store = getattr(state, "failed", None)
    if failed_store is not None:
        try:
            failed = len(failed_store)
        except TypeError:
            try:
                failed = len(failed_store.entries)
            except AttributeError:
                failed = 0
    return {
        "pending": pending,
        "uploading": uploading,
        "completed": completed,
        "failed": failed,
    }


def _format_uptime(started_at: datetime | None) -> str:
    if started_at is None:
        return "unknown"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - started_at
    total_s = int(delta.total_seconds())
    if total_s < 0:
        total_s = 0
    d, rem = divmod(total_s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _cloud_mode(state: Any) -> str:
    cfg = getattr(state, "cfg", None)
    if cfg is None:
        return "unknown"
    if getattr(cfg.cloud, "api_token", None):
        return "bearer token"
    if getattr(cfg.cloud, "certificate_thumbprint", None):
        return "mTLS (unsupported in v1)"
    return "local-only"


async def _streamlit_running() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(_STREAMLIT_HEALTH_URL, timeout=1.0)
            return r.status_code == 200
    except Exception:
        return False


async def _dashboard_context(request: Request) -> dict[str, Any]:
    state = get_state(request)
    cfg = getattr(state, "cfg", None)
    instruments = list(cfg.instruments) if cfg is not None else []
    activity_log = getattr(state, "activity", None)
    activity = activity_log.recent(20) if activity_log is not None else []
    ctx = common_context(request)
    ctx.update(
        {
            "paused": bool(getattr(state, "paused", False)),
            "agent_id": getattr(state, "agent_id", "unknown"),
            "uptime": _format_uptime(getattr(state, "started_at", None)),
            "cloud_mode": _cloud_mode(state),
            "instruments": instruments,
            "queue": _queue_counts(state),
            "activity": activity,
            "streamlit_running": await _streamlit_running(),
            "streamlit_port": STREAMLIT_PORT,
        }
    )
    return ctx


@router.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    state = get_state(request)
    cfg = getattr(state, "cfg", None)
    if cfg is None or not cfg.instruments:
        templates = get_templates(request)
        ctx = common_context(request)
        from mdqc.webui.wizard import _step_context

        ctx.update(_step_context(request, 1))
        return templates.TemplateResponse(request, "wizard/index.html", ctx)
    templates = get_templates(request)
    return templates.TemplateResponse(
        request, "dashboard/index.html", await _dashboard_context(request)
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(
        request, "dashboard/index.html", await _dashboard_context(request)
    )


@router.get("/dashboard/status", response_class=HTMLResponse)
async def status_fragment(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(
        request, "dashboard/status_fragment.html", await _dashboard_context(request)
    )


@router.get("/dashboard/streamlit", response_class=HTMLResponse)
async def streamlit_fragment(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    ctx = common_context(request)
    ctx["streamlit_running"] = await _streamlit_running()
    ctx["streamlit_port"] = STREAMLIT_PORT
    return templates.TemplateResponse(request, "dashboard/streamlit_fragment.html", ctx)


@router.get("/dashboard/queue", response_class=HTMLResponse)
async def queue_fragment(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    state = get_state(request)
    ctx = common_context(request)
    ctx["queue"] = _queue_counts(state)
    return templates.TemplateResponse(request, "dashboard/queue_fragment.html", ctx)


@router.get("/dashboard/activity", response_class=HTMLResponse)
async def activity_fragment(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    state = get_state(request)
    activity_log = getattr(state, "activity", None)
    activity = activity_log.recent(20) if activity_log is not None else []
    ctx = common_context(request)
    ctx["activity"] = activity
    return templates.TemplateResponse(request, "dashboard/activity_fragment.html", ctx)


# Reference paths.log_dir to keep import surface intact for typing/static analysis.
_ = paths
