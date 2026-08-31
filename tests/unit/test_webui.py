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
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from mdqc import gold_standards as gs
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
from mdqc.types import (
    ClassificationSource,
    Confidence,
    ControlType,
    ExtractionResult,
    RunClassification,
    RunMetrics,
    TargetMetric,
    Vendor,
)
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


def test_dashboard_shows_version(state_with_instruments: _FakeAppState) -> None:
    from mdqc import __version__

    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert "Version" in body
    assert f"v{__version__}" in body


def test_dashboard_status_fragment_shows_version(state_with_instruments: _FakeAppState) -> None:
    from mdqc import __version__

    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard/status")
    assert response.status_code == 200
    assert f"v{__version__}" in response.text


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
    # No cloud_environment posted -> defaults to "dev".
    assert "https://dev.massdynamics.com/api/evosep_qcs" in contents


def test_wizard_save_prod_environment(
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
        data={"output_mode": "cloud", "cloud_environment": "prod", "api_token": "tok"},
    )
    assert response.status_code == 200
    contents = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "https://app.massdynamics.com/api/evosep_qcs" in contents


def test_settings_get_prefills_default_cloud_endpoint(
    state_without_instruments: _FakeAppState,
) -> None:
    # A fresh Config() has no endpoint override, so Settings should show
    # "Development" pre-selected out of the box — an operator only needs to
    # paste a token, not also learn/guess the endpoint.
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.get("/settings")
    assert response.status_code == 200
    assert "dev.massdynamics.com" in response.text
    assert 'value="dev" selected' in response.text.replace("\n", " ")


def test_settings_get_prod_endpoint_preselects_prod(
    state_without_instruments: _FakeAppState,
) -> None:
    state_without_instruments.cfg.cloud.endpoint = "https://app.massdynamics.com/api/evosep_qcs"
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.get("/settings")
    assert response.status_code == 200
    assert 'value="prod" selected' in response.text.replace("\n", " ")


def test_settings_post_prod_environment_resolves_prod_endpoint(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data={"log_level": "info", "skyline_priority": "below_normal",
              "skyline_path": "auto", "skyline_timeout": "900",
              "api_token": "prod-token", "cloud_environment": "prod"},
    )
    assert response.status_code == 200
    contents = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "https://app.massdynamics.com/api/evosep_qcs" in contents


def test_settings_post_custom_environment_uses_custom_field(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data={"log_level": "info", "skyline_priority": "below_normal",
              "skyline_path": "auto", "skyline_timeout": "900",
              "api_token": "tok", "cloud_environment": "custom",
              "cloud_endpoint_custom": "https://staging.example.com/api/evosep_qcs"},
    )
    assert response.status_code == 200
    contents = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "https://staging.example.com/api/evosep_qcs" in contents


def test_settings_post_token_only_uses_default_endpoint(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates the "out of the box" flow: operator pastes a token, leaves
    # the endpoint field untouched (submitted blank), saves. The written
    # config must point at the real endpoint, not an empty/stale one.
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data={"log_level": "info", "skyline_priority": "below_normal",
              "skyline_path": "auto", "skyline_timeout": "900",
              "api_token": "my-real-token", "cloud_endpoint": ""},
    )
    assert response.status_code == 200
    config_path = tmp_path / "config.toml"
    contents = config_path.read_text(encoding="utf-8")
    assert "https://dev.massdynamics.com/api/evosep_qcs" in contents
    assert "my-real-token" in contents


def test_settings_post_cloud_change_shows_restart_hint(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The running Uploader is built once at startup from a config snapshot;
    # saving a new token in Settings does not reach it. The operator must be
    # told to restart, or their token silently does nothing.
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data={"log_level": "info", "skyline_priority": "below_normal",
              "skyline_path": "auto", "skyline_timeout": "900",
              "api_token": "new-token"},
    )
    assert response.status_code == 200
    assert "restart" in response.text.lower()


def test_settings_post_no_cloud_change_no_restart_hint(
    state_without_instruments: _FakeAppState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # state_without_instruments already has api_token="abc" (see _build_config)
    # — resubmitting the same value must not spuriously demand a restart.
    monkeypatch.setenv("MDQC_DATA_DIR", str(tmp_path))
    app = _build_app(state_without_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data={"log_level": "info", "skyline_priority": "below_normal",
              "skyline_path": "auto", "skyline_timeout": "900",
              "api_token": "abc",
              "cloud_endpoint": state_without_instruments.cfg.cloud.endpoint},
    )
    assert response.status_code == 200
    assert "restart the agent" not in response.text.lower()


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


# ─── Settings preserves config it does not render ──────────────────────────


def _base_settings_form(**overrides: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "log_level": "info",
        "skyline_priority": "below_normal",
        "skyline_path": "auto",
        "skyline_timeout": "900",
        "api_token": "abc",
        "cloud_environment": "dev",
    }
    form.update(overrides)
    return form


def test_settings_save_preserves_unrendered_config(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """Regression: saving Settings must not reset fields it has no control for.

    The docs tell operators to set their cloud token from this page. Before
    the fix that also silently reverted report_skyr_path to "auto" and
    dropped every peptide_classes rule, which would stop digest efficiency
    being computed without any visible error.
    """
    from mdqc.config.schema import PeptideClassRule

    cfg = state_with_instruments.cfg
    cfg.skyline.report_skyr_path = r"C:\ProgramData\MassDynamics\QC\methods\Custom.skyr"
    cfg.skyline.collapse_transitions_to_peptides = False
    cfg.peptide_classes = [
        PeptideClassRule(
            protein_name="Miss-clevage_pair",
            purpose="digest_efficiency",
            exclude_from_recovery=True,
        )
    ]

    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/settings", data=_base_settings_form())
    assert response.status_code == 200

    saved = state_with_instruments.cfg
    assert saved.skyline.report_skyr_path == (
        r"C:\ProgramData\MassDynamics\QC\methods\Custom.skyr"
    )
    assert saved.skyline.collapse_transitions_to_peptides is False
    assert len(saved.peptide_classes) == 1
    assert saved.peptide_classes[0].purpose == "digest_efficiency"


def test_settings_save_updates_retention_fields(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data=_base_settings_form(completed_retention_count="750", max_age_days="60"),
    )
    assert response.status_code == 200
    assert state_with_instruments.cfg.spool.completed_retention_count == 750
    assert state_with_instruments.cfg.spool.max_age_days == 60


def test_settings_retention_absent_from_form_keeps_current_value(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """A retention field missing from the POST must not reset the saved value."""
    state_with_instruments.cfg.spool.completed_retention_count = 999
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/settings", data=_base_settings_form())
    assert response.status_code == 200
    assert state_with_instruments.cfg.spool.completed_retention_count == 999


def test_retention_count_editable_in_local_only(tmp_path: Path, tmp_data_dir: Path) -> None:
    """Regression: Stoyan, 2026-07-27 — the field was disabled and unusable.

    Disabling it in local-only mode also stopped an operator raising a value
    persisted from an older install before switching on cloud upload, at
    which point the stale cap would immediately start deleting payloads.
    """
    cfg = Config(
        agent=AgentConfig(),
        cloud=CloudConfig(),  # no token -> local-only
        skyline=SkylineConfig(),
        watcher=WatcherConfig(),
        spool=SpoolConfig(completed_retention_count=10),
        instruments=[],
    )
    spool = Spool(agent_id="agent-test", agent_version="0.1.0", root=tmp_path / "spool")
    failed = FailedFilesStore(path=tmp_path / "failed.json")
    state = _FakeAppState(cfg=cfg, spool=spool, failed=failed, activity=_FakeActivityLog())
    app = _build_app(state)
    client = _client(app)

    body = client.get("/settings").text
    assert "disabled" not in body.split('name="completed_retention_count"')[1][:200]

    client.post(
        "/settings",
        data=_base_settings_form(
            api_token="", completed_retention_count="500", max_age_days="30"
        ),
    )
    assert state.cfg.spool.completed_retention_count == 500


def test_settings_warns_when_retention_too_low(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    state_with_instruments.cfg.spool.completed_retention_count = 10
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text
    assert "below one night of acquisition" in body


def test_settings_no_warning_at_healthy_retention(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    state_with_instruments.cfg.spool.completed_retention_count = 200
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text
    assert "below one night of acquisition" not in body


def test_settings_page_shows_retention_panel(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/settings")
    assert response.status_code == 200
    body = response.text
    assert "Payload retention" in body
    assert "completed_retention_count" in body
    assert "max_age_days" in body


def test_every_settings_field_is_inside_the_form(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """Regression: Stoyan, 2026-08-06 — retention edits reverted on save.

    The Payload retention and QC thresholds panels had been placed after
    ``</form>``, so their inputs never submitted and the POST handler fell
    back to the stored values. Every other test posts to /settings directly
    with a hand-built dict, which bypasses the form entirely and so cannot
    see this class of fault at all.
    """
    import re

    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text

    # Rows added by JS land inside containers that are themselves in the form,
    # so the markup to check is everything outside <script>.
    markup = re.sub(r"<script\b.*?</script>", "", body, flags=re.S | re.I)
    start, end = markup.index("<form "), markup.index("</form>")

    stray = [
        m.group(1)
        for m in re.finditer(
            r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', markup
        )
        if not start < m.start() < end
    ]
    assert not stray, f"fields outside the form will never submit: {stray}"


def test_settings_submit_button_is_inside_the_form(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """A submit button outside the form silently does nothing when clicked."""
    import re

    app = _build_app(state_with_instruments)
    client = _client(app)
    markup = re.sub(
        r"<script\b.*?</script>", "", client.get("/settings").text, flags=re.S | re.I
    )
    start, end = markup.index("<form "), markup.index("</form>")
    stray = [
        m.start()
        for m in re.finditer(r'<button[^>]*type="submit"', markup)
        if not start < m.start() < end
    ]
    assert not stray, "a submit button sits outside the form"


# ─── QC thresholds panel ───────────────────────────────────────────────────


def _threshold_form(**overrides: Any) -> dict[str, Any]:
    """Base settings form carrying the current threshold values."""
    from mdqc.config.schema import QC_THRESHOLD_FIELDS, QcThresholdsConfig

    shipped = QcThresholdsConfig()
    form = _base_settings_form()
    form.update({f: str(getattr(shipped, f)) for f in QC_THRESHOLD_FIELDS})
    form.update(overrides)
    return form


def test_settings_renders_threshold_panel(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    from mdqc.config.schema import QC_THRESHOLD_FIELDS

    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text
    assert "QC thresholds" in body
    for field in QC_THRESHOLD_FIELDS:
        assert f'name="{field}"' in body, field
    assert "Using recommended values" in body
    assert "provisional" in body


def test_settings_marks_customised_thresholds(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    state_with_instruments.cfg.qc_thresholds.peak_area_deviation_pct_warn = 8.0
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text
    assert "Customised" in body
    # The shipped value stays visible so an operator can see what they moved from.
    assert "recommended 10.0%" in body


def test_settings_saves_new_thresholds(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data=_threshold_form(peak_area_deviation_pct_warn="8.0", rt_deviation_pct_max="1.5"),
    )
    assert response.status_code == 200
    saved = state_with_instruments.cfg.qc_thresholds
    assert saved.peak_area_deviation_pct_warn == 8.0
    assert saved.rt_deviation_pct_max == 1.5
    assert saved.is_default() is False


def test_settings_rejects_warn_above_fail_with_a_readable_message(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """The operator must see the rule, not a pydantic report."""
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data=_threshold_form(
            peak_area_deviation_pct_warn="30.0", peak_area_deviation_pct_fail="25.0"
        ),
    )
    assert response.status_code == 200
    body = response.text
    assert "never be reached" in body
    assert "pydantic" not in body.lower()
    # Nothing was saved.
    assert state_with_instruments.cfg.qc_thresholds.is_default() is True


def test_settings_restore_returns_shipped_defaults(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """Restore means the published Evosep values, not the last saved ones."""
    state_with_instruments.cfg.qc_thresholds.peak_area_deviation_pct_warn = 3.0
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/settings",
        data=_threshold_form(peak_area_deviation_pct_warn="3.0", restore_thresholds="1"),
    )
    assert response.status_code == 200
    assert state_with_instruments.cfg.qc_thresholds.is_default() is True
    assert state_with_instruments.cfg.qc_thresholds.peak_area_deviation_pct_warn == 10.0


def test_settings_threshold_absent_from_form_keeps_current_value(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """A form posted without the panel expanded must not reset thresholds."""
    state_with_instruments.cfg.qc_thresholds.rt_deviation_pct_max = 1.5
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post("/settings", data=_base_settings_form())
    assert response.status_code == 200
    assert state_with_instruments.cfg.qc_thresholds.rt_deviation_pct_max == 1.5


# ─── Gold standards ────────────────────────────────────────────────────────


def _seed_ssc0_run(
    instrument_id: str = "qe-test", spd: int = 200, area: float = 1000.0, rt: float = 5.0
) -> str:
    classification = RunClassification(
        control_type=ControlType.SSC0,
        well_position=None,
        instrument_id=instrument_id,
        plate_id=None,
        confidence=Confidence.HIGH,
        source=ClassificationSource.FILENAME,
        spd=spd,
    )
    metric = TargetMetric(
        target_id="PEPA", peptide_sequence="PEPA", protein_name="Targets",
        peak_area=area, retention_time=rt,
    )
    extraction = ExtractionResult(
        run_id=uuid4(),
        target_metrics=[metric],
        run_metrics=RunMetrics(targets_found=1, targets_expected=1, target_recovery_pct=100.0),
    )
    gs.record_ssc0_run(classification, extraction)
    return str(extraction.run_id)


def test_gold_standards_empty_state(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/gold-standards")
    assert response.status_code == 200
    assert "No SSC0 runs recorded yet" in response.text
    assert "qe-test" in response.text


def test_gold_standards_renders_runs_table(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    _seed_ssc0_run()
    _seed_ssc0_run()
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/gold-standards?instrument=qe-test&spd=200")
    assert response.status_code == 200
    body = response.text
    assert "200 SPD" in body
    assert "PEPA" in body
    assert "No baseline saved yet" in body


def test_gold_standards_save_persists_and_activates_baseline(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    run_id_1 = _seed_ssc0_run()
    run_id_2 = _seed_ssc0_run()
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/gold-standards/save",
        data={
            "instrument_id": "qe-test",
            "spd": "200",
            "label": "Install baseline",
            "run_id": [run_id_1, run_id_2],
        },
    )
    assert response.status_code == 200
    assert "Baseline saved" in response.text
    assert "Install baseline" in response.text

    active = gs.get_active_baseline("qe-test", 200)
    assert active is not None
    assert sorted(active["source_run_ids"]) == sorted([run_id_1, run_id_2])


def test_gold_standards_save_without_selection_shows_error(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    _seed_ssc0_run()
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.post(
        "/gold-standards/save",
        data={"instrument_id": "qe-test", "spd": "200", "label": ""},
    )
    assert response.status_code == 200
    assert "Select at least one" in response.text
    assert gs.get_active_baseline("qe-test", 200) is None


def test_nav_shows_gold_standards_link(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert 'href="/gold-standards"' in response.text
    assert "Gold standards" in response.text


def test_nav_platform_link_when_cloud_configured(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    # state_with_instruments' cloud config carries an api_token (see _build_config),
    # so it is not local-only and the platform link should replace the plots link.
    app = _build_app(state_with_instruments)
    client = _client(app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert "Mass Dynamics ↗" in body
    assert state_with_instruments.cfg.cloud.endpoint.split("/api/")[0] in body
    assert "Local QC plots" not in body


def test_gold_standards_shows_instrument_selector_when_multiple(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    watch_a = tmp_path / "watch_a"
    watch_b = tmp_path / "watch_b"
    watch_a.mkdir()
    watch_b.mkdir()
    cfg = Config(
        agent=AgentConfig(),
        cloud=CloudConfig(api_token="abc"),
        skyline=SkylineConfig(),
        watcher=WatcherConfig(),
        spool=SpoolConfig(),
        instruments=[
            InstrumentConfig(
                id="Astral_0001", vendor=Vendor.THERMO, watch_path=watch_a,
                file_pattern="*.raw", template="QC_Method.sky",
            ),
            InstrumentConfig(
                id="Exploris01", vendor=Vendor.THERMO, watch_path=watch_b,
                file_pattern="*.raw", template="QC_Method.sky",
            ),
        ],
    )
    spool = Spool(agent_id="agent-test", agent_version="0.1.0", root=tmp_path / "spool")
    failed = FailedFilesStore(path=tmp_path / "failed.json")
    state = _FakeAppState(cfg=cfg, spool=spool, failed=failed, activity=_FakeActivityLog())
    app = _build_app(state)
    client = _client(app)

    response = client.get("/gold-standards")
    assert response.status_code == 200
    body = response.text
    assert "<select" in body
    assert "Astral_0001" in body
    assert "Exploris01" in body

    # Defaults to the first instrument when none is requested.
    assert "No SSC0 runs recorded yet for <strong>Astral_0001</strong>" in body

    response2 = client.get("/gold-standards?instrument=Exploris01")
    assert response2.status_code == 200
    assert "No SSC0 runs recorded yet for <strong>Exploris01</strong>" in response2.text


def test_nav_falls_back_to_streamlit_when_local_only(tmp_path: Path) -> None:
    cfg = Config(
        agent=AgentConfig(),
        cloud=CloudConfig(),  # no api_token -> local-only
        skyline=SkylineConfig(),
        watcher=WatcherConfig(),
        spool=SpoolConfig(),
        instruments=[],
    )
    spool = Spool(agent_id="agent-test", agent_version="0.1.0", root=tmp_path / "spool")
    failed = FailedFilesStore(path=tmp_path / "failed.json")
    activity = _FakeActivityLog()
    state = _FakeAppState(cfg=cfg, spool=spool, failed=failed, activity=activity)
    app = _build_app(state)
    client = _client(app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert "Local QC plots" in body
    assert "Mass Dynamics ↗" not in body


# Marker so pytest collection doesn't drop unused symbol warnings.
_ = json


# ── Gold standards: deviation basis ─────────────────────────────────────────
# The heatmap used to shade at ±1/±2 SD of the selected runs. On a tight panel
# that flags ordinary runs and on a noisy one it hides bad ones, so the page
# now shades by percentage from the gold standard (or from the mean where no
# baseline exists) using the same thresholds that decide the payload verdict.


def test_gold_standards_offers_deviation_basis_choice(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    _seed_ssc0_run()
    _seed_ssc0_run()
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/gold-standards?instrument=qe-test&spd=200").text

    for value in ("gold", "mean", "sd"):
        assert f'name="gs-basis" value="{value}"' in body


def test_gold_standards_disables_gold_basis_without_baseline(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    _seed_ssc0_run()
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/gold-standards?instrument=qe-test&spd=200").text

    # Nothing to compare against yet, so the option must not be selectable.
    assert 'name="gs-basis" value="gold" disabled' in body
    assert "save a baseline below to compare against a gold standard" in body


def test_gold_standards_ships_verdict_thresholds_to_the_page(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """Shading must use the configured warn/fail, not hard-coded numbers.

    Two sets of thresholds for the same judgement is how the colour on this
    page ends up disagreeing with the verdict sent to the platform.
    """
    _seed_ssc0_run()
    state_with_instruments.cfg = state_with_instruments.cfg.model_copy(
        update={
            "qc_thresholds": state_with_instruments.cfg.qc_thresholds.model_copy(
                update={
                    "peak_area_deviation_pct_warn": 15.0,
                    "peak_area_deviation_pct_fail": 30.0,
                    "rt_deviation_pct_max": 1.5,
                }
            )
        }
    )
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/gold-standards?instrument=qe-test&spd=200").text

    assert "const DEV_WARN_PCT = 15.0;" in body
    assert "const DEV_FAIL_PCT = 30.0;" in body
    assert "const RT_DEVIATION_PCT_MAX = 1.5;" in body


def test_gold_standards_exposes_saved_baseline_as_reference(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    run_id = _seed_ssc0_run(area=1000.0)
    app = _build_app(state_with_instruments)
    client = _client(app)
    client.post(
        "/gold-standards/save",
        data={"instrument_id": "qe-test", "spd": "200", "label": "ref", "run_id": [run_id]},
    )

    body = client.get("/gold-standards?instrument=qe-test&spd=200").text
    assert "const BASELINE_REF = " in body
    assert "peak_area_median" in body
    # With a baseline present the gold-standard option becomes selectable.
    assert 'name="gs-basis" value="gold" disabled' not in body


def test_settings_thresholds_panel_is_linkable(
    state_with_instruments: _FakeAppState, tmp_data_dir: Path
) -> None:
    """The Gold standards page deep-links here; the anchor must exist."""
    app = _build_app(state_with_instruments)
    client = _client(app)
    body = client.get("/settings").text
    assert 'id="qc-thresholds"' in body
    assert 'id="qc-thresholds-details"' in body
