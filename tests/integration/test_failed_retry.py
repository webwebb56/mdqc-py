"""Integration test: failed file is re-observed and processed on retry.

Boots an in-process AppState with a FakeExtractor that fails the first time
and succeeds the second. Verifies that:

- the first run lands the file in the failed-files store
- /api/failed/retry re-observes the file
- the retry run lands a payload in spool/completed/

The test exercises the lifecycle wiring (Fix 1+2) and the registry-not-blocking
contract (Fix 6) end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from mdqc import __version__ as agent_version
from mdqc.activity_log import ActivityLog
from mdqc.config import load_config
from mdqc.config.defaults import IPC_HEADER
from mdqc.failed_files import FailedFilesStore
from mdqc.service.lifecycle import AppState, EventPubSub, build_api
from mdqc.spool import Spool
from mdqc.types import (
    ExtractionResult,
    ExtractionStatus,
    RunMetrics,
    TargetMetric,
    Vendor,
)
from mdqc.uploader import Uploader, UploaderWorker
from mdqc.watcher.finalizer import Finalizer
from mdqc.watcher.registry import ProcessedRegistry


class FlakyExtractor:
    """Fails on first call for any path, succeeds on subsequent calls."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.failed_once: set[Path] = set()

    @property
    def skyline_path(self) -> Path | None:
        return None

    async def extract(
        self,
        template: Path,
        raw_file: Path,
        report_name: str = "MD_QC_Report",
    ) -> ExtractionResult:
        self.calls.append(raw_file)
        if raw_file not in self.failed_once:
            self.failed_once.add(raw_file)
            return ExtractionResult(
                run_id=uuid4(),
                raw_file_path=raw_file,
                template_path=template,
                backend="fake",
                backend_version="0.0.0-fake",
                extraction_time_ms=10,
                status=ExtractionStatus.FAILED,
                error_message="synthetic first-attempt failure",
                template_hash="0" * 64,
                raw_file_hash="0" * 64,
            )

        targets = [
            TargetMetric(
                target_id=f"T{i}",
                peptide_sequence=f"PEP{i}",
                precursor_mz=500.0 + i,
                retention_time=10.0 + i,
                rt_expected=10.0 + i,
                rt_delta=0.0,
                peak_area=1000.0 * (i + 1),
                detected=True,
            )
            for i in range(2)
        ]
        return ExtractionResult(
            run_id=uuid4(),
            raw_file_path=raw_file,
            template_path=template,
            backend="fake",
            backend_version="0.0.0-fake",
            extraction_time_ms=20,
            status=ExtractionStatus.SUCCESS,
            target_metrics=targets,
            run_metrics=RunMetrics(
                targets_found=2,
                targets_expected=2,
                target_recovery_pct=100.0,
            ),
            template_hash="0" * 64,
            raw_file_hash="0" * 64,
        )


def _write_config(cfg_path: Path, watch_path: Path, template_path: Path) -> None:
    cfg_path.write_text(
        dedent(
            f"""
            [agent]
            agent_id = "retry-test"

            [skyline]
            path = "/nonexistent"

            [watcher]
            stability_window_seconds = 1
            stabilization_timeout_seconds = 30

            [[instruments]]
            id = "retry-instrument"
            vendor = "thermo"
            watch_path = "{watch_path.as_posix()}"
            file_pattern = "*.raw"
            template = "{template_path.as_posix()}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


async def _wait_for(predicate, timeout_s: float = 15.0, poll_s: float = 0.05) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
    while datetime.now(UTC) < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll_s)
    return predicate()


@pytest.fixture
async def retry_env(tmp_data_dir: Path) -> AsyncIterator[dict]:
    watch_path = tmp_data_dir / "watch"
    watch_path.mkdir(parents=True, exist_ok=True)

    template_path = tmp_data_dir / "QC_Method.sky"
    template_path.write_bytes(b"<fake-template/>")

    cfg_path = tmp_data_dir / "config.toml"
    _write_config(cfg_path, watch_path, template_path)
    cfg = load_config(cfg_path)

    spool = Spool(agent_id="retry-test", agent_version=agent_version)
    failed = FailedFilesStore.load()
    activity = ActivityLog.load()
    registry = ProcessedRegistry()
    extractor = FlakyExtractor()
    uploader = Uploader(cfg.cloud, agent_version=agent_version)
    uploader_worker = UploaderWorker(spool, uploader, poll_interval_s=0.05)

    paused = asyncio.Event()
    stop_event = asyncio.Event()

    state_holder: dict[str, AppState] = {}

    async def _processed_callback(path: Path, vendor: Vendor) -> None:
        from mdqc.classifier import classify_file

        state = state_holder["state"]
        instrument = cfg.instruments[0]
        instrument_id = instrument.id
        finalized = False

        async def _fail(reason: str) -> None:
            nonlocal finalized
            if finalized:
                return
            state.failed.add(str(path), instrument_id, reason)
            await state.finalizer.mark_failed(path, reason)
            finalized = True

        try:
            classification = classify_file(path)
            classification.instrument_id = instrument_id
            if not classification.control_type.is_qc():
                await state.finalizer.mark_done(path)
                finalized = True
                return
            extraction = await state.extractor.extract(template_path, path)
            if extraction.status is ExtractionStatus.FAILED:
                await _fail(extraction.error_message or "extract failed")
                return
            state.spool.enqueue(classification, extraction)
            await state.finalizer.mark_done(path)
            finalized = True
        except Exception as exc:
            await _fail(f"unexpected: {exc!r}")

    finalizer = Finalizer(
        cfg.watcher,
        registry=registry,
        processed_callback=_processed_callback,
    )

    state = AppState(
        cfg=cfg,
        agent_id="retry-test",
        spool=spool,
        failed=failed,
        activity=activity,
        processed_registry=registry,
        extractor=extractor,  # type: ignore[arg-type]
        uploader=uploader,
        uploader_worker=uploader_worker,
        finalizer=finalizer,
        observer=None,
        paused=paused,
        stop_event=stop_event,
        started_at=datetime.now(UTC),
        events_pubsub=EventPubSub(),
        config_path=cfg_path,
    )
    state.token = "retry-test-token"
    state_holder["state"] = state

    app: FastAPI = build_api(state)

    async def finalizer_loop() -> None:
        while not stop_event.is_set():
            await finalizer.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=0.05)

    finalizer_task = asyncio.create_task(finalizer_loop())
    uploader_task = asyncio.create_task(uploader_worker.run(stop_event))

    yield {
        "state": state,
        "app": app,
        "watch_path": watch_path,
        "spool": spool,
        "extractor": extractor,
        "failed": failed,
    }

    stop_event.set()
    for task in (finalizer_task, uploader_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    await uploader.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_file_retry_lands_in_completed(retry_env: dict) -> None:
    state: AppState = retry_env["state"]
    app: FastAPI = retry_env["app"]
    watch_path: Path = retry_env["watch_path"]
    spool: Spool = retry_env["spool"]
    extractor: FlakyExtractor = retry_env["extractor"]
    failed: FailedFilesStore = retry_env["failed"]

    raw = watch_path / "20260426_QCA_A1_run01.raw"
    raw.write_bytes(b"x" * 4096)

    await state.finalizer.observe(raw, Vendor.THERMO)

    assert await _wait_for(lambda: failed.find(str(raw)) is not None, timeout_s=15.0), (
        "first attempt should have failed and recorded an entry"
    )
    assert len(extractor.calls) == 1

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/failed/retry",
            headers={IPC_HEADER: state.token},
            json={"path": str(raw)},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 1}

    assert await _wait_for(
        lambda: any(p.suffix == ".json" for p in spool.completed_dir.iterdir()),
        timeout_s=15.0,
    ), "retry should have produced a payload in completed/"

    assert len(extractor.calls) == 2
