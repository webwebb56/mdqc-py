"""In-memory baseline cache and run-vs-baseline comparison."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mdqc.types import TargetMetric


@dataclass
class Baseline:
    instrument_id: str
    template_hash: str
    established_at: datetime
    target_metrics: dict[str, dict[str, float]] = field(default_factory=dict)


class BaselineCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._store: dict[str, Baseline] = {}

    async def get(self, instrument_id: str) -> Baseline | None:
        async with self._lock:
            return self._store.get(instrument_id)

    async def set(self, baseline: Baseline) -> None:
        async with self._lock:
            self._store[baseline.instrument_id] = baseline

    async def clear(self, instrument_id: str) -> None:
        async with self._lock:
            self._store.pop(instrument_id, None)

    async def compare(
        self, instrument_id: str, run_targets: list[TargetMetric]
    ) -> dict[str, Any] | None:
        baseline = await self.get(instrument_id)
        if baseline is None:
            return None
        return _compare(baseline, run_targets)

    async def refresh_from_cloud(self, _endpoint: str | None = None) -> None:
        # Stub — Rust impl is also a no-op; preserve parity.
        return None


def _compare(baseline: Baseline, run_targets: list[TargetMetric]) -> dict[str, Any]:
    rt_shifts: list[float] = []
    area_ratios: list[float] = []
    outliers: list[str] = []

    baseline_rt_std = float(_overall_rt_std(baseline))

    for t in run_targets:
        bt = baseline.target_metrics.get(t.target_id)
        if bt is None:
            continue
        bt_rt = bt.get("retention_time")
        bt_area = bt.get("peak_area")

        rt_shift: float | None = None
        if bt_rt is not None and t.retention_time is not None:
            rt_shift = float(t.retention_time - bt_rt)
            rt_shifts.append(rt_shift)

        area_ratio: float | None = None
        if bt_area is not None and bt_area > 0 and t.peak_area is not None:
            area_ratio = float(t.peak_area / bt_area)
            area_ratios.append(area_ratio)

        is_outlier = False
        if (
            rt_shift is not None
            and baseline_rt_std > 0
            and abs(rt_shift) > 3 * baseline_rt_std
        ):
            is_outlier = True
        if area_ratio is not None and abs(area_ratio - 1.0) > 0.5:
            is_outlier = True
        if is_outlier:
            outliers.append(t.target_id)

    return {
        "rt_shift_mean": _mean(rt_shifts),
        "rt_shift_std": _std(rt_shifts),
        "area_ratio_mean": _mean(area_ratios),
        "area_ratio_std": _std(area_ratios),
        "outlier_targets": outliers,
    }


def _overall_rt_std(baseline: Baseline) -> float:
    values = [
        v["rt_std"]
        for v in baseline.target_metrics.values()
        if "rt_std" in v
    ]
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return float(var**0.5)
