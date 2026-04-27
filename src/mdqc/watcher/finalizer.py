from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mdqc.config import defaults
from mdqc.config.schema import WatcherConfig
from mdqc.types import FinalizationState, Vendor
from mdqc.watcher.registry import ProcessedRegistry
from mdqc.watcher.vendor import is_artifact_complete, vendor_stability_window


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FileTracker:
    path: Path
    vendor: Vendor
    state: FinalizationState
    detected_at: datetime
    last_size: int = 0
    last_mtime: float = 0.0
    last_change_at: datetime = field(default_factory=_now)
    processing_started_at: datetime | None = None
    failure_reason: str | None = None


def _measure(path: Path, vendor: Vendor) -> tuple[int, float]:
    target: Path
    if vendor is Vendor.BRUKER:
        target = path / "analysis.tdf"
    elif vendor is Vendor.WATERS:
        target = path / "_FUNC001.DAT"
    elif vendor is Vendor.AGILENT:
        target = path / "AcqData"
    elif vendor is Vendor.SCIEX:
        scan = Path(str(path) + ".scan")
        try:
            wiff = path.stat()
        except OSError:
            return (0, 0.0)
        size = wiff.st_size
        mtime = wiff.st_mtime
        if scan.exists():
            try:
                ss = scan.stat()
            except OSError:
                return (size, mtime)
            size += ss.st_size
            mtime = max(mtime, ss.st_mtime)
        return (size, mtime)
    else:
        target = path

    try:
        st = target.stat()
    except OSError:
        return (0, 0.0)
    return (st.st_size, st.st_mtime)


ProcessedCallback = Callable[[Path, Vendor], Awaitable[None]]


class Finalizer:
    def __init__(
        self,
        watcher_cfg: WatcherConfig,
        *,
        registry: ProcessedRegistry,
        processed_callback: ProcessedCallback,
    ) -> None:
        self._cfg = watcher_cfg
        self._registry = registry
        self._callback = processed_callback
        self._trackers: dict[Path, FileTracker] = {}
        self._lock = asyncio.Lock()

    async def observe(self, path: Path, vendor: Vendor) -> None:
        async with self._lock:
            existing = self._trackers.get(path)
            if existing is not None and existing.state in {
                FinalizationState.DONE,
                FinalizationState.FAILED,
                FinalizationState.PROCESSING,
            }:
                return
            if self._registry.contains(path):
                return
            if existing is not None:
                return
            size, mtime = _measure(path, vendor)
            self._trackers[path] = FileTracker(
                path=path,
                vendor=vendor,
                state=FinalizationState.DETECTED,
                detected_at=_now(),
                last_size=size,
                last_mtime=mtime,
                last_change_at=_now(),
            )

    def trackers_snapshot(self) -> dict[Path, FileTracker]:
        return dict(self._trackers)

    def state_of(self, path: Path) -> FinalizationState | None:
        tracker = self._trackers.get(path)
        return tracker.state if tracker else None

    async def mark_done(self, path: Path) -> None:
        async with self._lock:
            tracker = self._trackers.get(path)
            if tracker is None:
                return
            tracker.state = FinalizationState.DONE
            self._registry.add(path)
            self._trackers.pop(path, None)

    async def mark_failed(self, path: Path, reason: str) -> None:
        async with self._lock:
            tracker = self._trackers.get(path)
            if tracker is None:
                return
            tracker.state = FinalizationState.FAILED
            tracker.failure_reason = reason
            self._trackers.pop(path, None)

    async def tick(self) -> None:
        ready_calls: list[tuple[Path, Vendor]] = []
        async with self._lock:
            now = _now()
            stabilization_timeout = self._cfg.stabilization_timeout_seconds
            base_window = self._cfg.stability_window_seconds
            terminal: list[Path] = []

            for path, tracker in self._trackers.items():
                if tracker.state is FinalizationState.DETECTED:
                    tracker.state = FinalizationState.STABILIZING
                    tracker.last_change_at = now
                    continue

                if tracker.state is FinalizationState.STABILIZING:
                    elapsed = (now - tracker.detected_at).total_seconds()
                    if elapsed > stabilization_timeout:
                        tracker.state = FinalizationState.FAILED
                        tracker.failure_reason = (
                            f"Stabilization timeout after {stabilization_timeout}s"
                        )
                        terminal.append(path)
                        continue

                    size, mtime = _measure(path, tracker.vendor)
                    if size != tracker.last_size or mtime != tracker.last_mtime:
                        tracker.last_size = size
                        tracker.last_mtime = mtime
                        tracker.last_change_at = now
                        continue

                    window = vendor_stability_window(tracker.vendor, base_window)
                    stable_for = (now - tracker.last_change_at).total_seconds()
                    if stable_for >= window and is_artifact_complete(path, tracker.vendor):
                        tracker.state = FinalizationState.READY
                    continue

                if tracker.state is FinalizationState.READY:
                    tracker.state = FinalizationState.PROCESSING
                    tracker.processing_started_at = now
                    ready_calls.append((path, tracker.vendor))
                    continue

                if tracker.state is FinalizationState.PROCESSING:
                    started = tracker.processing_started_at or tracker.detected_at
                    if (now - started).total_seconds() > defaults.PROCESSING_TIMEOUT_S:
                        tracker.state = FinalizationState.FAILED
                        tracker.failure_reason = (
                            f"Processing timeout after {defaults.PROCESSING_TIMEOUT_S}s"
                        )
                        terminal.append(path)
                    continue

            for path in terminal:
                self._trackers.pop(path, None)

        for path, vendor in ready_calls:
            try:
                await self._callback(path, vendor)
            except Exception:
                await self.mark_failed(path, "processed_callback raised")
