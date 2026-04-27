from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from mdqc.config import defaults
from mdqc.config.schema import WatcherConfig
from mdqc.types import FinalizationState, Vendor
from mdqc.watcher.finalizer import Finalizer
from mdqc.watcher.registry import ProcessedRegistry


def _make_thermo_file(tmp_path: Path, name: str = "run.raw", size: int = 1024) -> Path:
    f = tmp_path / name
    f.write_bytes(b"x" * size)
    return f


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Vendor]] = []

    async def __call__(self, path: Path, vendor: Vendor) -> None:
        self.calls.append((path, vendor))


def _cfg(**kwargs: object) -> WatcherConfig:
    base = {
        "use_filesystem_events": True,
        "scan_interval_seconds": defaults.SCAN_INTERVAL_S,
        "stability_window_seconds": defaults.STABILITY_WINDOW_S,
        "stabilization_timeout_seconds": defaults.STABILIZATION_TIMEOUT_S,
    }
    base.update(kwargs)
    return WatcherConfig(**base)


@pytest.mark.asyncio
async def test_happy_path_detected_to_processing(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    callback = _Recorder()
    fin = Finalizer(_cfg(stability_window_seconds=10), registry=ProcessedRegistry(), processed_callback=callback)

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        assert fin.state_of(raw) is FinalizationState.DETECTED

        await fin.tick()
        assert fin.state_of(raw) is FinalizationState.STABILIZING

        frozen.tick(delta=timedelta(seconds=11))
        await fin.tick()
        assert fin.state_of(raw) is FinalizationState.READY

        await fin.tick()
        assert fin.state_of(raw) is FinalizationState.PROCESSING
        assert callback.calls == [(raw, Vendor.THERMO)]


@pytest.mark.asyncio
async def test_stabilization_timeout_marks_failed(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    fin = Finalizer(
        _cfg(stability_window_seconds=10, stabilization_timeout_seconds=30),
        registry=ProcessedRegistry(),
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()

        for _ in range(5):
            frozen.tick(delta=timedelta(seconds=4))
            raw.write_bytes(raw.read_bytes() + b"y")
            await fin.tick()
            assert fin.state_of(raw) is FinalizationState.STABILIZING

        frozen.tick(delta=timedelta(seconds=60))
        await fin.tick()
        assert fin.state_of(raw) is None


@pytest.mark.asyncio
async def test_changing_file_stays_in_stabilizing(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    fin = Finalizer(
        _cfg(stability_window_seconds=10, stabilization_timeout_seconds=600),
        registry=ProcessedRegistry(),
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()

        for i in range(5):
            frozen.tick(delta=timedelta(seconds=8))
            raw.write_bytes(b"z" * (1024 + i + 1))
            await fin.tick()
            assert fin.state_of(raw) is FinalizationState.STABILIZING


@pytest.mark.asyncio
async def test_processing_timeout_marks_failed(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    callback = _Recorder()
    fin = Finalizer(
        _cfg(stability_window_seconds=5),
        registry=ProcessedRegistry(),
        processed_callback=callback,
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()
        frozen.tick(delta=timedelta(seconds=6))
        await fin.tick()
        assert fin.state_of(raw) is FinalizationState.READY
        await fin.tick()
        assert fin.state_of(raw) is FinalizationState.PROCESSING

        frozen.tick(delta=timedelta(seconds=defaults.PROCESSING_TIMEOUT_S + 1))
        await fin.tick()
        assert fin.state_of(raw) is None


@pytest.mark.asyncio
async def test_done_path_added_to_registry_and_reobserve_ignored(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    raw = _make_thermo_file(tmp_path)
    callback = _Recorder()
    registry = ProcessedRegistry()
    fin = Finalizer(_cfg(stability_window_seconds=5), registry=registry, processed_callback=callback)

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()
        frozen.tick(delta=timedelta(seconds=6))
        await fin.tick()
        await fin.tick()
        await fin.mark_done(raw)

    assert registry.contains(raw)
    assert fin.state_of(raw) is None

    await fin.observe(raw, Vendor.THERMO)
    assert fin.state_of(raw) is None


@pytest.mark.asyncio
async def test_bruker_uses_longer_stability_window(tmp_path: Path, tmp_data_dir: Path) -> None:
    d = tmp_path / "run.d"
    d.mkdir()
    (d / "analysis.tdf").write_bytes(b"data")

    fin = Finalizer(
        _cfg(stability_window_seconds=10, stabilization_timeout_seconds=600),
        registry=ProcessedRegistry(),
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(d, Vendor.BRUKER)
        await fin.tick()

        frozen.tick(delta=timedelta(seconds=11))
        await fin.tick()
        assert fin.state_of(d) is FinalizationState.STABILIZING

        frozen.tick(delta=timedelta(seconds=defaults.BRUKER_STABILITY_WINDOW_S + 1))
        await fin.tick()
        assert fin.state_of(d) is FinalizationState.READY


@pytest.mark.asyncio
async def test_bruker_lock_file_keeps_in_stabilizing(tmp_path: Path, tmp_data_dir: Path) -> None:
    d = tmp_path / "run.d"
    d.mkdir()
    (d / "analysis.tdf").write_bytes(b"data")
    (d / "analysis.tdf-journal").write_bytes(b"")

    fin = Finalizer(
        _cfg(stability_window_seconds=5, stabilization_timeout_seconds=600),
        registry=ProcessedRegistry(),
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(d, Vendor.BRUKER)
        await fin.tick()
        frozen.tick(delta=timedelta(seconds=defaults.BRUKER_STABILITY_WINDOW_S + 5))
        await fin.tick()
        assert fin.state_of(d) is FinalizationState.STABILIZING

        (d / "analysis.tdf-journal").unlink()
        frozen.tick(delta=timedelta(seconds=defaults.BRUKER_STABILITY_WINDOW_S + 5))
        await fin.tick()
        assert fin.state_of(d) is FinalizationState.READY


@pytest.mark.asyncio
async def test_observe_ignored_when_already_in_registry(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    registry.add(raw)
    fin = Finalizer(_cfg(), registry=registry, processed_callback=_Recorder())
    await fin.observe(raw, Vendor.THERMO)
    assert fin.state_of(raw) is None


@pytest.mark.asyncio
async def test_mark_failed_removes_tracker(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    fin = Finalizer(_cfg(), registry=registry, processed_callback=_Recorder())
    await fin.observe(raw, Vendor.THERMO)
    await fin.mark_failed(raw, "boom")
    assert fin.state_of(raw) is None
    assert not registry.contains(raw)


@pytest.mark.asyncio
async def test_mark_failed_does_not_block_reobserve(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    fin = Finalizer(_cfg(), registry=registry, processed_callback=_Recorder())
    await fin.observe(raw, Vendor.THERMO)
    await fin.mark_failed(raw, "boom")

    await fin.observe(raw, Vendor.THERMO)
    assert fin.state_of(raw) is FinalizationState.DETECTED


@pytest.mark.asyncio
async def test_mark_done_blocks_reobserve(tmp_path: Path, tmp_data_dir: Path) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    fin = Finalizer(_cfg(), registry=registry, processed_callback=_Recorder())
    await fin.observe(raw, Vendor.THERMO)
    await fin.mark_done(raw)
    assert registry.contains(raw)

    await fin.observe(raw, Vendor.THERMO)
    assert fin.state_of(raw) is None


@pytest.mark.asyncio
async def test_stabilization_timeout_does_not_pollute_registry(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    fin = Finalizer(
        _cfg(stability_window_seconds=10, stabilization_timeout_seconds=30),
        registry=registry,
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()

        for _ in range(5):
            frozen.tick(delta=timedelta(seconds=4))
            raw.write_bytes(raw.read_bytes() + b"y")
            await fin.tick()

        frozen.tick(delta=timedelta(seconds=60))
        await fin.tick()

    assert fin.state_of(raw) is None
    assert not registry.contains(raw)


@pytest.mark.asyncio
async def test_processing_timeout_does_not_pollute_registry(
    tmp_path: Path, tmp_data_dir: Path
) -> None:
    raw = _make_thermo_file(tmp_path)
    registry = ProcessedRegistry()
    fin = Finalizer(
        _cfg(stability_window_seconds=5),
        registry=registry,
        processed_callback=_Recorder(),
    )

    with freeze_time("2026-01-01 12:00:00", tick=False) as frozen:
        await fin.observe(raw, Vendor.THERMO)
        await fin.tick()
        frozen.tick(delta=timedelta(seconds=6))
        await fin.tick()
        await fin.tick()

        frozen.tick(delta=timedelta(seconds=defaults.PROCESSING_TIMEOUT_S + 1))
        await fin.tick()

    assert fin.state_of(raw) is None
    assert not registry.contains(raw)
