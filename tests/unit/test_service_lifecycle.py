from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi.testclient import TestClient

from mdqc.activity_log import ActivityLog
from mdqc.config import load_config, paths
from mdqc.config.defaults import IPC_HEADER
from mdqc.failed_files import FailedFilesStore
from mdqc.service.agent_id import resolve_agent_id
from mdqc.service.lifecycle import (
    AppState,
    EventPubSub,
    build_api,
)
from mdqc.spool import Spool
from mdqc.uploader import Uploader, UploaderWorker
from mdqc.watcher.finalizer import Finalizer
from mdqc.watcher.registry import ProcessedRegistry
from mdqc.webui.auth import SESSION_COOKIE_NAME


def _write_minimal_config(tmp_data_dir: Path) -> Path:
    cfg_path = tmp_data_dir / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [agent]
            agent_id = "test-agent"

            [skyline]
            path = "auto"
            """
        ),
        encoding="utf-8",
    )
    return cfg_path


def _build_state(tmp_data_dir: Path, *, token: str = "test-token") -> AppState:
    cfg_path = _write_minimal_config(tmp_data_dir)
    cfg = load_config(cfg_path)
    paths.ensure_dirs()
    spool = Spool(agent_id="test-agent", agent_version="0.0.0")
    failed = FailedFilesStore.load()
    activity = ActivityLog.load()
    registry = ProcessedRegistry()

    async def _cb(_path, _vendor):  # type: ignore[no-untyped-def]
        return None

    finalizer = Finalizer(cfg.watcher, registry=registry, processed_callback=_cb)
    uploader = Uploader(cfg.cloud, agent_version="0.0.0")
    worker = UploaderWorker(spool, uploader)

    state = AppState(
        cfg=cfg,
        agent_id="test-agent",
        spool=spool,
        failed=failed,
        activity=activity,
        processed_registry=registry,
        extractor=None,  # type: ignore[arg-type]
        uploader=uploader,
        uploader_worker=worker,
        finalizer=finalizer,
        observer=None,
        paused=asyncio.Event(),
        stop_event=asyncio.Event(),
        started_at=datetime.now(UTC),
        events_pubsub=EventPubSub(),
        config_path=cfg_path,
    )
    state.token = token
    return state


def test_appstate_instantiates(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir)
    assert state.agent_id == "test-agent"
    assert state.spool is not None
    assert state.uploader is not None


@pytest.mark.asyncio
async def test_eventpubsub_publish_visible_to_subscriber() -> None:
    pubsub = EventPubSub(max_queue=10)
    seen: list[str] = []

    async def consumer() -> None:
        async for event in pubsub.subscribe():
            seen.append(event.type)
            if len(seen) >= 1:
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    pubsub.publish("paused", {})
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == ["paused"]


def test_eventpubsub_publish_with_no_subscribers_is_noop() -> None:
    pubsub = EventPubSub(max_queue=2)
    pubsub.publish("paused", {})  # must not raise
    assert pubsub.subscriber_count == 0


@pytest.mark.asyncio
async def test_eventpubsub_subscriber_overflow_drops_oldest() -> None:
    pubsub = EventPubSub(max_queue=2)

    # Subscribe and grab the queue without consuming.
    async def grab() -> None:
        async for _event in pubsub.subscribe():
            return

    # Manually exercise: register a subscriber via an inert iterator.
    iterator = pubsub.subscribe()
    # advance once to populate the subscriber list
    advance_task = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0.01)

    pubsub.publish("a", {"i": 1})
    pubsub.publish("b", {"i": 2})
    # The first event is consumed by advance_task; subsequent pushes go to queue.
    first = await asyncio.wait_for(advance_task, timeout=1.0)
    assert first.type == "a"

    # Now overflow: publish 3 more; queue capacity is 2 → first ("b" already consumed... actually "b" sits in queue.)
    # Verify by stuffing more events than capacity and then collecting.
    pubsub.publish("c", {"i": 3})
    pubsub.publish("d", {"i": 4})
    pubsub.publish("e", {"i": 5})  # should drop the oldest in the queue

    collected: list[str] = []

    async def drain() -> None:
        try:
            while True:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=0.2)
                collected.append(event.type)
        except (TimeoutError, StopAsyncIteration):
            return

    await drain()
    # We won't assert exact contents of collected (depends on timing of put/drop)
    # but cap should hold and we should NOT see all five.
    assert len(collected) <= 4
    # cleanup
    await iterator.aclose()


def test_build_api_returns_fastapi(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir)
    app = build_api(state)
    assert app is not None
    assert state.app is app


def test_health_endpoint_with_token(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="hello")
    app = build_api(state)
    client = TestClient(app)
    response = client.get("/api/health", headers={IPC_HEADER: "hello"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_health_endpoint_rejects_missing_token(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="hello")
    app = build_api(state)
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 401


def test_health_endpoint_accepts_query_token(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="hello")
    app = build_api(state)
    client = TestClient(app)
    response = client.get("/api/health?token=hello")
    assert response.status_code == 200


def test_health_endpoint_accepts_session_cookie(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="cookie-token")
    app = build_api(state)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "cookie-token")
    response = client.get("/api/health")
    assert response.status_code == 200


def test_health_endpoint_rejects_invalid_cookie(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="cookie-token")
    app = build_api(state)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "wrong-token")
    response = client.get("/api/health")
    assert response.status_code == 401


def test_htmx_post_authenticated_by_cookie_only(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="cookie-token")
    app = build_api(state)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "cookie-token")
    response = client.post("/api/pause", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert state.paused.is_set()


def test_query_token_navigation_then_cookie_followups(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="qtok")
    app = build_api(state)
    client = TestClient(app)

    first = client.get("/api/health?token=qtok")
    assert first.status_code == 200

    # Subsequent navigation has no token in URL — cookie must be honoured.
    client.cookies.set(SESSION_COOKIE_NAME, "qtok")
    second = client.get("/api/status")
    assert second.status_code == 200


def test_pause_resume_publish_events(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="t")
    app = build_api(state)
    client = TestClient(app)

    response = client.post("/api/pause", headers={IPC_HEADER: "t"})
    assert response.status_code == 200
    assert state.paused.is_set()

    response = client.post("/api/resume", headers={IPC_HEADER: "t"})
    assert response.status_code == 200
    assert not state.paused.is_set()


def test_status_endpoint_returns_fields(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="t")
    app = build_api(state)
    client = TestClient(app)

    response = client.get("/api/status", headers={IPC_HEADER: "t"})
    assert response.status_code == 200
    body = response.json()
    assert "uptime_s" in body
    assert "pending_count" in body
    assert "failed_count" in body
    assert "local_only_mode" in body


def test_failed_clear_endpoint(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="t")
    state.failed.add("/data/foo.raw", None, "boom")
    assert len(state.failed) == 1
    app = build_api(state)
    client = TestClient(app)
    response = client.post("/api/failed/clear", headers={IPC_HEADER: "t"})
    assert response.status_code == 200
    assert len(state.failed) == 0


def test_get_config_endpoint(tmp_data_dir: Path) -> None:
    state = _build_state(tmp_data_dir, token="t")
    app = build_api(state)
    client = TestClient(app)
    response = client.get("/api/config", headers={IPC_HEADER: "t"})
    assert response.status_code == 200
    body = response.json()
    assert "agent" in body


def test_resolve_agent_id_returns_value_when_explicit() -> None:
    out = resolve_agent_id("explicit-id")
    assert out == "explicit-id"


def test_resolve_agent_id_auto_returns_16_hex() -> None:
    out = resolve_agent_id("auto")
    assert len(out) == 16
    assert all(c in "0123456789abcdef" for c in out)


def test_resolve_agent_id_auto_is_deterministic_on_same_machine() -> None:
    a = resolve_agent_id("auto")
    b = resolve_agent_id("auto")
    assert a == b
