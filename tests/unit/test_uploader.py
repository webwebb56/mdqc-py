from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mdqc.config.schema import CloudConfig
from mdqc.spool import Spool
from mdqc.uploader import (
    AuthenticationError,
    PermanentUploadError,
    TransientUploadError,
    Uploader,
    UploaderWorker,
)

ENDPOINT = "https://qc-ingest.test/v1/"


def _payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "payload_id": "00000000-0000-0000-0000-000000000001",
        "correlation_id": "agent-20260101000000-deadbeef",
        "agent_id": "agent",
        "agent_version": "0.1.0",
        "run": {"run_id": "00000000-0000-0000-0000-000000000002"},
    }


def _enqueue_payload(spool: Spool, payload: dict[str, Any], name: str = "test_payload.json") -> Path:
    path = spool.pending_dir / name
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    return path


def _make_spool(tmp_path: Path) -> Spool:
    return Spool(agent_id="agent", agent_version="0.1.0", root=tmp_path / "spool")


def _make_uploader(
    *,
    api_token: str | None = "tok-123",
    certificate_thumbprint: str | None = None,
    proxy: str | None = None,
    sleep_recorder: list[float] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Uploader:
    cfg = CloudConfig(
        endpoint=ENDPOINT,
        api_token=api_token,
        certificate_thumbprint=certificate_thumbprint,
        proxy=proxy,
    )
    sleep_fn = None
    if sleep_recorder is not None:
        async def _record(seconds: float) -> None:
            sleep_recorder.append(seconds)
        sleep_fn = _record
    return Uploader(
        cfg,
        agent_version="0.1.0",
        http_client=http_client,
        _sleep=sleep_fn,
    )


# ─── 1. Auth-token happy path ────────────────────────────────────────────────


@respx.mock
async def test_auth_token_happy_path() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    uploader = _make_uploader()
    try:
        await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer tok-123"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"] == "mdqc-py/0.1.0"


# ─── 2. Local-only mode ──────────────────────────────────────────────────────


@respx.mock
async def test_local_only_mode_skips_http(tmp_path: Path) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    spool = _make_spool(tmp_path)
    payload_path = _enqueue_payload(spool, _payload())

    uploader = _make_uploader(api_token=None, certificate_thumbprint=None)
    assert uploader.is_local_only is True
    worker = UploaderWorker(spool, uploader, poll_interval_s=0.01)
    try:
        processed = await worker.upload_one()
    finally:
        await uploader.aclose()

    assert processed is True
    assert route.call_count == 0
    assert not payload_path.exists()
    assert (spool.completed_dir / "test_payload.json").exists()


# ─── 3. Cert-configured-without-token raises ─────────────────────────────────


def test_cert_without_token_raises() -> None:
    cfg = CloudConfig(
        endpoint=ENDPOINT,
        api_token=None,
        certificate_thumbprint="A" * 40,
    )
    with pytest.raises(RuntimeError, match="certificate_thumbprint"):
        Uploader(cfg, agent_version="0.1.0")


# ─── 4. 401 raises AuthenticationError, no retries ───────────────────────────


@respx.mock
async def test_401_raises_authentication_error_no_retry() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(401, text="nope"))
    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    try:
        with pytest.raises(AuthenticationError):
            await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 1
    assert sleeps == []


# ─── 5. 5xx then 200 retries once ────────────────────────────────────────────


@respx.mock
async def test_5xx_then_200_retries_once() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200),
        ]
    )
    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    try:
        await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 2
    assert len(sleeps) == 1
    assert 20.0 <= sleeps[0] <= 40.0


# ─── 6. CRITICAL: Tenacity 4-entry timing test (off-by-one canary) ───────────


@respx.mock
async def test_tenacity_timing_canary_exactly_four_sleeps() -> None:
    """If this test fails, you've hit the off-by-one wait_chain trap.

    With UPLOAD_TOTAL_ATTEMPTS=5 and UPLOAD_RETRY_SLEEPS as 4 entries,
    we expect exactly 4 inter-retry sleeps in the documented ranges.
    A leading (0,0) entry would produce 5 sleeps starting with ~0s.
    """
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))
    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    try:
        with pytest.raises(TransientUploadError):
            await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 5, f"expected 5 attempts, got {route.call_count}"
    assert len(sleeps) == 4, (
        f"expected exactly 4 sleeps (off-by-one trap canary); got {len(sleeps)}: {sleeps}"
    )
    assert 20.0 <= sleeps[0] <= 40.0, f"sleep 1 out of range: {sleeps[0]}"
    assert 90.0 <= sleeps[1] <= 150.0, f"sleep 2 out of range: {sleeps[1]}"
    assert 480.0 <= sleeps[2] <= 720.0, f"sleep 3 out of range: {sleeps[2]}"
    assert 3000.0 <= sleeps[3] <= 4200.0, f"sleep 4 out of range: {sleeps[3]}"


# ─── 7. 5 attempts exhausted → worker marks failed ───────────────────────────


@respx.mock
async def test_attempts_exhausted_worker_marks_failed(tmp_path: Path) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))
    spool = _make_spool(tmp_path)
    payload_path = _enqueue_payload(spool, _payload())

    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    worker = UploaderWorker(spool, uploader, poll_interval_s=0.01)
    try:
        processed = await worker.upload_one()
    finally:
        await uploader.aclose()

    assert processed is True
    assert route.call_count == 5
    assert not payload_path.exists()
    assert (spool.failed_dir / "test_payload.json").exists()


# ─── 8. Connection error retries ─────────────────────────────────────────────


@respx.mock
async def test_connection_error_retries() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.ConnectError("conn refused"),
            httpx.Response(200),
        ]
    )
    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    try:
        await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 2
    assert len(sleeps) == 1


# ─── 9. Worker loop processes one then stops ─────────────────────────────────


@respx.mock
async def test_worker_loop_processes_one_then_stops(tmp_path: Path) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    spool = _make_spool(tmp_path)
    payload_path = _enqueue_payload(spool, _payload())

    uploader = _make_uploader()
    worker = UploaderWorker(spool, uploader, poll_interval_s=0.01)
    stop_event = asyncio.Event()

    async def _stop_after_first() -> None:
        while payload_path.exists():
            await asyncio.sleep(0.01)
        stop_event.set()

    try:
        await asyncio.wait_for(
            asyncio.gather(worker.run(stop_event), _stop_after_first()),
            timeout=5.0,
        )
    finally:
        await uploader.aclose()

    assert (spool.completed_dir / "test_payload.json").exists()


# ─── 10. Permanent 4xx (e.g. 422) does not retry ─────────────────────────────


@respx.mock
async def test_permanent_4xx_no_retry(tmp_path: Path) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(422, text="bad"))
    spool = _make_spool(tmp_path)
    payload_path = _enqueue_payload(spool, _payload())

    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    worker = UploaderWorker(spool, uploader, poll_interval_s=0.01)
    try:
        processed = await worker.upload_one()
    finally:
        await uploader.aclose()

    assert processed is True
    assert route.call_count == 1
    assert sleeps == []
    assert not payload_path.exists()
    assert (spool.failed_dir / "test_payload.json").exists()


async def test_permanent_4xx_raises_directly() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(ENDPOINT).mock(return_value=httpx.Response(422, text="bad"))
        sleeps: list[float] = []
        uploader = _make_uploader(sleep_recorder=sleeps)
        try:
            with pytest.raises(PermanentUploadError):
                await uploader.upload_payload(_payload())
        finally:
            await uploader.aclose()
        assert route.call_count == 1
        assert sleeps == []


# ─── 11. Proxy config is respected ───────────────────────────────────────────


async def test_proxy_config_respected() -> None:
    uploader = _make_uploader(proxy="http://proxy.test:8080")
    try:
        mounts = uploader._client._mounts
        found = False
        for transport in mounts.values():
            pool = getattr(transport, "_pool", None)
            proxy_url = getattr(pool, "_proxy_url", None)
            if proxy_url is None:
                continue
            host = proxy_url.host
            if isinstance(host, bytes):
                host = host.decode("ascii")
            if host == "proxy.test" and proxy_url.port == 8080:
                found = True
                break
        assert found, f"proxy not wired into client; mounts={mounts}"
    finally:
        await uploader.aclose()


async def test_no_proxy_means_no_mounts() -> None:
    uploader = _make_uploader(proxy=None)
    try:
        assert uploader._client._mounts == {}
    finally:
        await uploader.aclose()


# ─── 12. Idempotency: same payload retried doesn't change body ───────────────


@respx.mock
async def test_idempotency_body_byte_equal_across_retries() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200),
        ]
    )
    sleeps: list[float] = []
    uploader = _make_uploader(sleep_recorder=sleeps)
    try:
        await uploader.upload_payload(_payload())
    finally:
        await uploader.aclose()

    assert route.call_count == 3
    bodies = [bytes(call.request.content) for call in route.calls]
    assert bodies[0] == bodies[1] == bodies[2]
