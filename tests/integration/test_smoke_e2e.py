"""End-to-end smoke test for the watcher → finalizer → spool → uploader pipeline.

The full pipeline depends on SkylineCmd, which is not present on developer
machines or CI. We substitute a `FakeExtractor` that returns a populated
`ExtractionResult`. Everything else (watcher polling, finalizer state machine,
spool atomic writes, uploader-in-local-only-mode) runs unmodified.

Marked `@pytest.mark.integration` because it boots real background tasks and
performs filesystem activity beyond what unit tests do.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from mdqc import __version__ as agent_version
from mdqc.activity_log import ActivityLog
from mdqc.classifier import classify_file
from mdqc.config import load_config, paths
from mdqc.config.schema import CloudConfig
from mdqc.failed_files import FailedFilesStore
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
from mdqc.watcher.observer import WatchdogObserver
from mdqc.watcher.registry import ProcessedRegistry


class FakeExtractor:
    """Drop-in replacement for `mdqc.extractor.Extractor` for tests.

    Skips SkylineCmd entirely; returns a populated `ExtractionResult` with five
    synthetic target metrics so the spool payload looks realistic.
    """

    def __init__(self) -> None:
        self.calls: list[Path] = []

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
        targets = [
            TargetMetric(
                target_id=f"fake-target-{i}",
                peptide_sequence=f"PEPTIDEK{i}",
                precursor_mz=500.0 + i,
                retention_time=10.0 + i,
                rt_expected=10.0 + i,
                rt_delta=0.0,
                peak_area=1000.0 * (i + 1),
                peak_height=500.0 * (i + 1),
                mass_error_ppm=0.5,
                detected=True,
            )
            for i in range(5)
        ]
        run_metrics = RunMetrics(
            targets_found=5,
            targets_expected=5,
            target_recovery_pct=100.0,
            median_rt_shift=0.0,
            median_mass_error_ppm=0.5,
        )
        return ExtractionResult(
            run_id=uuid4(),
            raw_file_path=raw_file,
            template_path=template,
            backend="fake",
            backend_version="0.0.0-fake",
            extraction_time_ms=42,
            status=ExtractionStatus.SUCCESS,
            target_metrics=targets,
            run_metrics=run_metrics,
            template_hash="0" * 64,
            raw_file_hash="0" * 64,
        )


def _write_config(cfg_path: Path, watch_path: Path, template_path: Path) -> None:
    cfg_path.write_text(
        dedent(
            f"""
            [agent]
            agent_id = "smoke-test"

            [cloud]
            # No api_token, no certificate_thumbprint -> local-only mode.

            [skyline]
            path = "/nonexistent"

            [watcher]
            stability_window_seconds = 1
            stabilization_timeout_seconds = 30

            [[instruments]]
            id = "smoke-instrument"
            vendor = "thermo"
            watch_path = "{watch_path.as_posix()}"
            file_pattern = "*.raw"
            template = "{template_path.as_posix()}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


async def _wait_for(predicate, timeout_s: float = 15.0, poll_s: float = 0.1) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
    while datetime.now(UTC) < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll_s)
    return predicate()


@pytest.fixture
async def smoke_env(tmp_data_dir: Path) -> AsyncIterator[dict]:
    watch_path = tmp_data_dir / "watch"
    watch_path.mkdir(parents=True, exist_ok=True)

    bundled = paths.bundled_assets_dir() / "QC_Method.sky"
    methods = paths.methods_dir()
    methods.mkdir(parents=True, exist_ok=True)
    template_target = methods / "QC_Method.sky"
    if bundled.exists():
        shutil.copy2(bundled, template_target)
    else:
        template_target.write_bytes(b"<fake-skyline-template/>")

    cfg_path = tmp_data_dir / "config.toml"
    _write_config(cfg_path, watch_path, template_target)

    monkey_env = {"MDQC_CONFIG": str(cfg_path)}
    import os

    prior_env = {k: os.environ.get(k) for k in monkey_env}
    os.environ.update(monkey_env)

    cfg = load_config(cfg_path)

    spool = Spool(agent_id="smoke-test", agent_version=agent_version)
    failed = FailedFilesStore.load()
    activity = ActivityLog.load()
    registry = ProcessedRegistry()
    extractor = FakeExtractor()
    uploader = Uploader(CloudConfig(), agent_version=agent_version)
    uploader_worker = UploaderWorker(spool, uploader, poll_interval_s=0.1)

    stop_event = asyncio.Event()
    detected_queue: asyncio.Queue[tuple[Path, Vendor]] = asyncio.Queue()
    main_loop = asyncio.get_running_loop()

    instrument = cfg.instruments[0]

    async def processed_callback(path: Path, vendor: Vendor) -> None:
        classification = classify_file(path)
        try:
            result = await extractor.extract(template=template_target, raw_file=path)
        except Exception as exc:
            await finalizer.mark_failed(path, f"extract failed: {exc!r}")
            return
        try:
            spool.enqueue(classification, result)
        except Exception as exc:
            await finalizer.mark_failed(path, f"spool failed: {exc!r}")
            return
        await finalizer.mark_done(path)

    finalizer = Finalizer(
        cfg.watcher,
        registry=registry,
        processed_callback=processed_callback,
    )

    def on_detected(path: Path, vendor: Vendor) -> None:
        main_loop.call_soon_threadsafe(detected_queue.put_nowait, (path, vendor))

    observer = WatchdogObserver(
        [(instrument.watch_path, instrument.vendor, instrument.file_pattern)],
        on_detected=on_detected,
    )
    observer.start()

    async def detection_pump() -> None:
        while not stop_event.is_set():
            try:
                path, vendor = await asyncio.wait_for(detected_queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            await finalizer.observe(path, vendor)

    async def finalizer_loop() -> None:
        while not stop_event.is_set():
            await finalizer.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=0.25)

    pump_task = asyncio.create_task(detection_pump())
    finalizer_task = asyncio.create_task(finalizer_loop())
    uploader_task = asyncio.create_task(uploader_worker.run(stop_event))

    yield {
        "cfg": cfg,
        "watch_path": watch_path,
        "spool": spool,
        "extractor": extractor,
        "uploader": uploader,
    }

    stop_event.set()
    observer.stop()
    for task in (pump_task, finalizer_task, uploader_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await uploader.aclose()
    del activity
    del failed

    for k, v in prior_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drop_file_observed_extracted_spooled_completed(smoke_env: dict) -> None:
    watch_path: Path = smoke_env["watch_path"]
    spool: Spool = smoke_env["spool"]
    extractor: FakeExtractor = smoke_env["extractor"]
    uploader: Uploader = smoke_env["uploader"]

    assert uploader.is_local_only, "smoke env must run in local-only mode"

    raw = watch_path / "20260426_smoke_QCA_A1_run01.raw"
    raw.write_bytes(b"x" * 4096)

    extracted = await _wait_for(lambda: len(extractor.calls) >= 1, timeout_s=20.0)
    assert extracted, "FakeExtractor was never invoked — watcher/finalizer pipeline stalled"

    completed_dir = spool.completed_dir
    completed = await _wait_for(
        lambda: any(p.suffix == ".json" for p in completed_dir.iterdir()),
        timeout_s=20.0,
    )
    assert completed, f"no payload appeared in completed/: {list(completed_dir.iterdir())}"

    payloads = [p for p in completed_dir.iterdir() if p.suffix == ".json"]
    assert len(payloads) == 1, f"expected exactly one payload, got {payloads}"

    payload = json.loads(payloads[0].read_text(encoding="utf-8"))
    assert payload["correlation_id"].startswith("smoke-test-")
    assert payload["agent_id"] == "smoke-test"
    assert "run_id" in payload["run"]
    assert payload["run"]["control_type"] == "QC_A"
    assert payload["run"]["raw_file_name"] == "20260426_smoke_QCA_A1_run01.raw"
    assert payload["extraction"]["status"] == "SUCCESS"
    assert len(payload["target_metrics"]) == 5
    assert payload["run_metrics"]["targets_found"] == 5
