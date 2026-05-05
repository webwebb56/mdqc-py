"""Web UI integration-style unit tests using FastAPI TestClient.

TODO: once Agent E lands the real token-auth middleware in
mdqc.service.lifecycle.build_api(), drop the fake middleware here and use that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from mdqc.config.schema import (
    AgentConfig,
    CloudConfig,
    Config,
    InstrumentConfig,
    SkylineConfig,
    SpoolConfig,
    WatcherConfig,
)
from mdqc.failed_files import FailedFilesStore
from mdqc.spool import Spool
from mdqc.types import Vendor
from mdqc.webui import register

_FAKE_TOKEN = "test-token-12345"


class _FakeActivityLog:
    def __init__(self) -> None:
        self._entries: list[Any] = []

    def recent(self, n: int) -> list[Any]:
        return self._entries[:n]


class _FakeAppState:
    """Stand-in for mdqc.service.lifecycle.AppState during tests."""

    def __init__(
        self,
        *,
        cfg: Config,
        spool: Spool,
        failed: FailedFilesStore,
        activity: _FakeActivityLog,
    ) -> None:
        self.cfg = cfg
        self.spool = spool
        self.failed = failed
        self.activity = activity
        self.events_pubsub: Any = None
        self.paused: bool = False
        self.started_at = datetime.now(UTC)
        self.agent_id: str = "agent-test-0001"
        self.app: FastAPI | None = None


class _FakeTokenMiddleware(BaseHTTPMiddleware):
    """Mimics Agent E's token middleware: accept header, query, or session cookie."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        token: str | None = request.headers.get("X-MDQC-Token")
        if token is None:
            token = request.query_params.get("token")
        if token is None:
            token = request.cookies.get("mdqc_session")
        if token != _FAKE_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        return response


def _build_config(*, with_instruments: bool = False, tmp_path: Path) -> Config:
    instruments: list[InstrumentConfig] = []
    if with_instruments:
        watch = tmp_path / "watch"
        watch.mkdir(parents=True, exist_ok=True)
        instruments.append(
            InstrumentConfig(
                id="qe-test",
                vendor=Vendor.THERMO,
                watch_path=watch,
                file_pattern="*.raw",
                template="QC_Method.sky",
            )
        )
    return Config(
        agent=AgentConfig(),
        cloud=CloudConfig(api_token="abc"),
        skyline=SkylineConfig(),
        watcher=WatcherConfig(),
        spool=SpoolConfig(),
        instruments=instruments,
    )


def _build_app(state: _FakeAppState) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_FakeTokenMiddleware)
    register(app, state)  # type: ignore[arg-type]
    state.app = app
    return app


def _client(app: FastAPI) -> TestClient:
    client = TestClient(app)
    client.cookies.set("mdqc_session", _FAKE_TOKEN)
    return client


@pytest.fixture()
def state_with_instruments(tmp_path: Path) -> Iterator[_FakeAppState]:
    cfg = _build_config(with_instruments=True, tmp_path=tmp_path)
    spool = Spool(agent_id="agent-test", agent_version="0.1.0", root=tmp_path / "spool")
    failed = FailedFilesStore(path=tmp_path / "failed.json")
    activity = _FakeActivityLog()
    state = _FakeAppState(cfg=cfg, spool=spool, failed=failed, activity=activity)
    yield state


@pytest.fixture()
def state_without_instruments(tmp_path: Path) -> Iterator[_FakeAppState]:
    cfg = _build_config(with_instruments=False, tmp_path=tmp_path)
    spool = Spool(agent_id="agent-test", agent_version="0.1.0", root=tmp_path / "spool")
    failed = FailedFilesStore(path=tmp_path / "failed.json")
    activity = _FakeActivityLog()
    state = _FakeAppState(cfg=cfg, spool=spool, failed=failed, activity=activity)
    yield state


def test_dashboard_renders(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert "Mass Dynamics QC Agent" in body
    assert "Dashboard" in body


def test_root_redirects_to_dashboard_when_configured(
    state_with_instruments: _FakeAppState,
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_wizard_step_1_when_no_instruments(
    state_without_instruments: _FakeAppState,
) -> None:
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.get("/wizard")
    assert response.status_code == 200
    body = response.text
    assert "Setup" in body or "vendor" in body.lower()
    assert "Step 1" in body


def test_wizard_redirects_when_instruments_present(
    state_with_instruments: _FakeAppState,
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/wizard", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert response.headers.get("location") == "/dashboard"


def test_diagnostics_renders(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/diagnostics")
    assert response.status_code == 200
    body = response.text
    assert "Diagnostics" in body
    assert "Skyline" in body
    assert "Cloud" in body
    assert "Spool" in body


def test_failed_renders_with_table(
    state_with_instruments: _FakeAppState, tmp_path: Path
) -> None:
    state_with_instruments.failed.add(
        path=str(tmp_path / "broken.raw"),
        instrument_id="qe-test",
        reason="boom",
    )
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/failed")
    assert response.status_code == 200
    body = response.text
    assert "broken.raw" in body
    assert "boom" in body
    assert "<table" in body


def test_failed_clear_empties_store(
    state_with_instruments: _FakeAppState, tmp_path: Path
) -> None:
    state_with_instruments.failed.add(
        path=str(tmp_path / "broken.raw"),
        instrument_id="qe-test",
        reason="boom",
    )
    assert len(state_with_instruments.failed) == 1
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/failed/clear")
    assert response.status_code == 200
    assert len(state_with_instruments.failed) == 0
    assert "No failed files" in response.text


def test_static_htmx_served(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert response.headers["content-type"] in (
        "application/javascript",
        "text/javascript",
        "application/javascript; charset=utf-8",
        "text/javascript; charset=utf-8",
    )
    assert b"htmx" in response.content.lower() or len(response.content) > 1000


def test_session_cookie_accepted(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = TestClient(app)
    client.cookies.set("mdqc_session", _FAKE_TOKEN)
    response = client.get("/dashboard")
    assert response.status_code == 200


def test_request_without_token_rejected(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = TestClient(app)
    response = client.get("/dashboard")
    assert response.status_code == 401


def test_dashboard_queue_fragment(state_with_instruments: _FakeAppState) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard/queue")
    assert response.status_code == 200
    body = response.text
    assert "Pending" in body
    assert "Failed" in body


def test_wizard_step_post_advances(
    state_without_instruments: _FakeAppState,
) -> None:
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post("/wizard/step/1", data={"vendor": "thermo"})
    assert response.status_code == 200
    assert "Step 2" in response.text


def test_wizard_save_writes_config(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    client.post("/wizard/step/1", data={"vendor": "thermo"})
    client.post(
        "/wizard/step/2",
        data={"instrument_id": "qe-99", "watch_path": str(tmp_path / "data")},
    )
    client.post("/wizard/step/3", data={"skyline_path": ""})
    client.post("/wizard/step/4", data={"template_path": ""})
    response = client.post(
        "/wizard/step/5",
        data={"output_mode": "cloud", "api_token": "tok"},
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower()
    config_path = tmp_path / "config.toml"
    assert config_path.exists()
    contents = config_path.read_text(encoding="utf-8")
    assert "qe-99" in contents
    assert "thermo" in contents


def test_reset_processed_clears_registry(
    state_with_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    from mdqc.watcher.registry import ProcessedRegistry

    registry = ProcessedRegistry()
    registry.add(tmp_path / "a.raw")
    registry.add(tmp_path / "b.raw")
    assert len(registry) == 2

    state_with_instruments.processed_registry = registry  # type: ignore[attr-defined]

    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/settings/reset-processed")

    assert response.status_code == 200
    assert "Cleared" in response.text
    assert "2 entries" in response.text
    assert len(registry) == 0


def test_reset_processed_with_no_registry_returns_ok(
    state_with_instruments: _FakeAppState,
) -> None:
    # _FakeAppState doesn't define processed_registry — endpoint should still 200.
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/settings/reset-processed")
    assert response.status_code == 200
    assert "Cleared" in response.text


def test_logs_index(state_with_instruments: _FakeAppState, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "mdqc.log"
    log_file.write_text("hello log line\n", encoding="utf-8")
    import os

    os.environ["MDQC_DATA_DIR"] = str(tmp_path)
    try:
        app = _build_app(state_with_instruments)
        client = _client(app)
        response = client.get("/logs")
        assert response.status_code == 200
        assert "hello log line" in response.text
    finally:
        # the autouse session fixture restores the dir at session teardown.
        pass


# Marker so pytest collection doesn't drop unused symbol warnings.
_ = json
