from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from mdqc.config.defaults import IPC_HEADER
from mdqc.ipc.client import IpcClient, IpcUnavailable, StatusReport
from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo


@pytest.fixture
def runtime_file(tmp_path: Path) -> RuntimeFile:
    rf = RuntimeFile(tmp_path / "runtime.json")
    rf.write(
        RuntimeInfo(
            port=12345,
            token="initial-token",
            pid=4242,
            started_at=datetime(2026, 4, 26, tzinfo=UTC),
        )
    )
    return rf


def test_from_runtime_file_missing_raises(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "absent.json")
    with pytest.raises(IpcUnavailable):
        IpcClient.from_runtime_file(runtime_file=rf)


def test_from_runtime_file_builds_client(runtime_file: RuntimeFile) -> None:
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        assert client.base_url == "http://127.0.0.1:12345"
        assert client.token == "initial-token"
    finally:
        client.close()


@respx.mock
def test_health_returns_true_on_200(runtime_file: RuntimeFile) -> None:
    respx.get("http://127.0.0.1:12345/api/health").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        assert client.health() is True
    finally:
        client.close()


@respx.mock
def test_health_token_in_header(runtime_file: RuntimeFile) -> None:
    route = respx.get("http://127.0.0.1:12345/api/health").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        client.health()
    finally:
        client.close()
    assert route.called
    request = route.calls[0].request
    assert request.headers[IPC_HEADER] == "initial-token"


@respx.mock
def test_get_status_parses_payload(runtime_file: RuntimeFile) -> None:
    payload = {
        "service_running": True,
        "uptime_s": 42,
        "paused": False,
        "pending_count": 3,
        "uploading_count": 1,
        "failed_count": 0,
        "recent_activity": [],
        "local_only_mode": True,
    }
    respx.get("http://127.0.0.1:12345/api/status").mock(
        return_value=httpx.Response(200, json=payload)
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        status = client.get_status()
    finally:
        client.close()
    assert isinstance(status, StatusReport)
    assert status.uptime_s == 42
    assert status.local_only_mode is True
    assert status.pending_count == 3


@respx.mock
def test_pause_resume_call_endpoints(runtime_file: RuntimeFile) -> None:
    pause_route = respx.post("http://127.0.0.1:12345/api/pause").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    resume_route = respx.post("http://127.0.0.1:12345/api/resume").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        client.pause()
        client.resume()
    finally:
        client.close()
    assert pause_route.called
    assert resume_route.called


@respx.mock
def test_reprocess_sends_path(runtime_file: RuntimeFile) -> None:
    route = respx.post("http://127.0.0.1:12345/api/reprocess").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        client.reprocess(Path("/data/raw.d"))
    finally:
        client.close()
    body = route.calls[0].request.content.decode()
    assert "/data/raw.d" in body


@respx.mock
def test_retry_failed_returns_count(runtime_file: RuntimeFile) -> None:
    respx.post("http://127.0.0.1:12345/api/failed/retry").mock(
        return_value=httpx.Response(200, json={"count": 7})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        n = client.retry_failed("all")
    finally:
        client.close()
    assert n == 7


@respx.mock
def test_clear_failed_invokes_endpoint(runtime_file: RuntimeFile) -> None:
    route = respx.post("http://127.0.0.1:12345/api/failed/clear").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        client.clear_failed()
    finally:
        client.close()
    assert route.called


@respx.mock
def test_get_config_and_update(runtime_file: RuntimeFile) -> None:
    respx.get("http://127.0.0.1:12345/api/config").mock(
        return_value=httpx.Response(200, json={"agent": {"agent_id": "abc"}})
    )
    put_route = respx.put("http://127.0.0.1:12345/api/config").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        cfg = client.get_config()
        client.update_config({"agent": {"agent_id": "new"}})
    finally:
        client.close()
    assert cfg["agent"]["agent_id"] == "abc"
    assert put_route.called


@respx.mock
def test_401_triggers_token_re_read_and_retry(
    tmp_path: Path, runtime_file: RuntimeFile
) -> None:
    # First call returns 401; we simulate token rotation by writing new info.
    route = respx.get("http://127.0.0.1:12345/api/status").mock(
        return_value=httpx.Response(401, json={"detail": "stale"})
    )
    new_route = respx.get("http://127.0.0.1:99999/api/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "service_running": True,
                "uptime_s": 0,
                "paused": False,
                "pending_count": 0,
                "uploading_count": 0,
                "failed_count": 0,
                "recent_activity": [],
                "local_only_mode": False,
            },
        )
    )

    client = IpcClient.from_runtime_file(runtime_file=runtime_file)
    try:
        # Rotate the token in runtime.json before issuing the request that 401s.
        runtime_file.write(
            RuntimeInfo(
                port=99999,
                token="rotated-token",
                pid=4242,
                started_at=datetime(2026, 4, 26, tzinfo=UTC),
            )
        )
        status = client.get_status()
    finally:
        client.close()

    assert route.called
    assert new_route.called
    assert client.token == "rotated-token"
    assert status.service_running is True


def test_health_returns_false_on_unavailable(tmp_path: Path) -> None:
    rf = RuntimeFile(tmp_path / "runtime.json")
    rf.write(
        RuntimeInfo(
            port=1,  # unlikely to be listening
            token="x",
            pid=1,
            started_at=datetime(2026, 4, 26, tzinfo=UTC),
        )
    )
    client = IpcClient(
        base_url="http://127.0.0.1:1",
        token="x",
        timeout_s=0.5,
        runtime_file=rf,
    )
    try:
        assert client.health() is False
    finally:
        client.close()


def test_status_render_text_contains_sections() -> None:
    report = StatusReport(
        service_running=True,
        uptime_s=0,
        paused=False,
        pending_count=0,
        uploading_count=0,
        failed_count=0,
    )
    text = report.render_text()
    assert "MD QC Agent Status" in text
    assert "Queue" in text
    assert "Recent activity" in text
