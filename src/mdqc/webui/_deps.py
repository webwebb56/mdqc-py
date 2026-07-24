"""Internal helpers shared by the web UI routers.

Each router receives `state: AppState` and `templates: Jinja2Templates` via
`request.app.state.mdqc_state` / `mdqc_templates`, populated in
`mdqc.webui.routes.register`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from mdqc.service.lifecycle import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.mdqc_state  # type: ignore[no-any-return]


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.mdqc_templates  # type: ignore[no-any-return]


def platform_app_url(request: Request) -> str | None:
    """Root URL of the configured MD platform environment (dev/prod), or None.

    None when cloud push isn't configured (local-only) — the nav then falls
    back to the local Streamlit link instead of pointing at a platform the
    agent isn't actually uploading to.
    """
    try:
        state = get_state(request)
        cfg = state.cfg
    except Exception:
        return None
    if cfg is None or cfg.is_local_only():
        return None
    parts = urlsplit(cfg.cloud.endpoint)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def common_context(request: Request) -> dict[str, Any]:
    from mdqc import __version__

    return {
        "request": request,
        "agent_version": __version__,
        "platform_app_url": platform_app_url(request),
    }


__all__ = ["common_context", "get_state", "get_templates", "platform_app_url"]
