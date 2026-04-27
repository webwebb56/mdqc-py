from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from mdqc.baseline import Baseline, BaselineCache
from mdqc.types import TargetMetric


def _baseline(instrument_id: str = "INST01") -> Baseline:
    return Baseline(
        instrument_id=instrument_id,
        template_hash="deadbeef",
        established_at=datetime(2026, 1, 1),
        target_metrics={
            "p1": {"retention_time": 10.0, "peak_area": 1000.0, "rt_std": 0.05},
            "p2": {"retention_time": 20.0, "peak_area": 2000.0, "rt_std": 0.05},
        },
    )


@pytest.mark.asyncio
async def test_set_get_round_trip() -> None:
    cache = BaselineCache()
    b = _baseline()
    await cache.set(b)
    fetched = await cache.get("INST01")
    assert fetched is not None
    assert fetched.instrument_id == "INST01"
    assert fetched.template_hash == "deadbeef"


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    cache = BaselineCache()
    assert await cache.get("nope") is None


@pytest.mark.asyncio
async def test_compare_no_baseline_returns_none() -> None:
    cache = BaselineCache()
    result = await cache.compare("INST01", [])
    assert result is None


@pytest.mark.asyncio
async def test_compare_outlier_rt_shift() -> None:
    cache = BaselineCache()
    await cache.set(_baseline())
    # rt_std baseline is 0.05; threshold is 3*0.05 = 0.15
    targets = [
        TargetMetric(
            target_id="p1", retention_time=10.5, peak_area=1000.0, rt_delta=0.5
        ),
        TargetMetric(
            target_id="p2", retention_time=20.01, peak_area=2000.0, rt_delta=0.01
        ),
    ]
    result = await cache.compare("INST01", targets)
    assert result is not None
    assert "p1" in result["outlier_targets"]
    assert "p2" not in result["outlier_targets"]


@pytest.mark.asyncio
async def test_compare_outlier_area_ratio() -> None:
    cache = BaselineCache()
    await cache.set(_baseline())
    targets = [
        TargetMetric(target_id="p1", retention_time=10.0, peak_area=2000.0),
        TargetMetric(target_id="p2", retention_time=20.0, peak_area=2000.0),
    ]
    result = await cache.compare("INST01", targets)
    assert result is not None
    assert "p1" in result["outlier_targets"]


@pytest.mark.asyncio
async def test_compare_returns_stats_keys() -> None:
    cache = BaselineCache()
    await cache.set(_baseline())
    targets = [
        TargetMetric(target_id="p1", retention_time=10.05, peak_area=1100.0),
        TargetMetric(target_id="p2", retention_time=20.05, peak_area=2100.0),
    ]
    result = await cache.compare("INST01", targets)
    assert result is not None
    assert set(result.keys()) == {
        "rt_shift_mean",
        "rt_shift_std",
        "area_ratio_mean",
        "area_ratio_std",
        "outlier_targets",
    }


@pytest.mark.asyncio
async def test_clear_removes_entry() -> None:
    cache = BaselineCache()
    await cache.set(_baseline())
    await cache.clear("INST01")
    assert await cache.get("INST01") is None


@pytest.mark.asyncio
async def test_concurrent_get_set_no_corruption() -> None:
    cache = BaselineCache()

    async def writer(idx: int) -> None:
        await cache.set(_baseline(f"INST{idx:02d}"))

    async def reader(idx: int) -> None:
        for _ in range(20):
            await cache.get(f"INST{idx:02d}")
            await asyncio.sleep(0)

    tasks = [writer(i) for i in range(10)] + [reader(i) for i in range(10)]
    await asyncio.gather(*tasks)
    for i in range(10):
        b = await cache.get(f"INST{i:02d}")
        assert b is not None
        assert b.instrument_id == f"INST{i:02d}"


@pytest.mark.asyncio
async def test_refresh_from_cloud_is_noop() -> None:
    cache = BaselineCache()
    # Should not raise.
    await cache.refresh_from_cloud("https://example.com")
