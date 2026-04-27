"""Internal helpers shared by the web UI routers.

Each router receives `state: AppState` and `templates: Jinja2Templates` via
`request.app.state.mdqc_state` / `mdqc_templates`, populated in
`mdqc.webui.routes.register`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from mdqc.service.lifecycle import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.mdqc_state  # type: ignore[no-any-return]


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.mdqc_templates  # type: ignore[no-any-return]


def common_context(request: Request) -> dict[str, Any]:
    from mdqc import __version__

    return {"request": request, "agent_version": __version__}


__all__ = ["common_context", "get_state", "get_templates"]
