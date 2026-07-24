"""Web UI router registration entry point.

Called by `mdqc.service.lifecycle.attach_webui(state)`. Mounts static files,
sets up the Jinja2 environment, and includes the wizard, dashboard,
diagnostics, failed-files, logs, and gold-standards routers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from mdqc.service.lifecycle import AppState


WEBUI_ROOT: Path = Path(__file__).resolve().parent
STATIC_DIR: Path = WEBUI_ROOT / "static"
TEMPLATES_DIR: Path = WEBUI_ROOT / "templates"


def build_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(TEMPLATES_DIR))


def _ensure_static_assets() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    icon_target = STATIC_DIR / "icon.png"
    if not icon_target.exists():
        from mdqc.config.paths import bundled_assets_dir

        src = bundled_assets_dir() / "icon.png"
        if src.exists():
            import shutil

            shutil.copy2(src, icon_target)


def register(app: FastAPI, state: AppState) -> None:
    templates = build_templates()
    app.state.mdqc_state = state
    app.state.mdqc_templates = templates

    _ensure_static_assets()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from mdqc.webui import dashboard, diagnostics, failed, gold_standards, logs, settings, wizard

    app.include_router(dashboard.router)
    app.include_router(settings.router)
    app.include_router(wizard.router)
    app.include_router(diagnostics.router)
    app.include_router(failed.router)
    app.include_router(logs.router)
    app.include_router(gold_standards.router)


__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "build_templates", "register"]
