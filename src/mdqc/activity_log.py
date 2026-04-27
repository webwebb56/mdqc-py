from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mdqc.config.defaults import ACTIVITY_LOG_MAX
from mdqc.config.paths import activity_log_path
from mdqc.types import ExtractionStatus

log = logging.getLogger(__name__)


@dataclass
class ActivityEntry:
    path: str
    instrument_id: str | None
    timestamp: datetime
    result: ExtractionStatus
    targets_found: int | None = None
    targets_expected: int | None = None
    extraction_time_ms: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.astimezone(UTC).isoformat()
        d["result"] = self.result.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ActivityEntry:
        ts_raw = data.get("timestamp")
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        else:
            ts = datetime.now(UTC)
        result_raw = data.get("result")
        result = (
            ExtractionStatus(result_raw)
            if isinstance(result_raw, str)
            else ExtractionStatus.SUCCESS
        )
        return cls(
            path=str(data.get("path", "")),
            instrument_id=data.get("instrument_id"),  # type: ignore[arg-type]
            timestamp=ts,
            result=result,
            targets_found=_opt_int(data.get("targets_found")),
            targets_expected=_opt_int(data.get("targets_expected")),
            extraction_time_ms=_opt_int(data.get("extraction_time_ms")),
            error=data.get("error") if isinstance(data.get("error"), str) else None,  # type: ignore[arg-type]
        )


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class ActivityLog:
    def __init__(
        self,
        entries: list[ActivityEntry] | None = None,
        *,
        max_entries: int = ACTIVITY_LOG_MAX,
        path: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._path = path
        self.entries: list[ActivityEntry] = list(entries or [])
        self._trim_locked()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else activity_log_path()

    def _trim_locked(self) -> None:
        if len(self.entries) > self._max_entries:
            del self.entries[self._max_entries :]

    def record(self, entry: ActivityEntry) -> None:
        with self._lock:
            self.entries.insert(0, entry)
            self._trim_locked()
            self._save_locked()

    def recent(self, n: int = 20) -> list[ActivityEntry]:
        with self._lock:
            slice_ = self.entries[: max(0, n)]
            return [
                ActivityEntry(
                    path=e.path,
                    instrument_id=e.instrument_id,
                    timestamp=e.timestamp,
                    result=e.result,
                    targets_found=e.targets_found,
                    targets_expected=e.targets_expected,
                    extraction_time_ms=e.extraction_time_ms,
                    error=e.error,
                )
                for e in slice_
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self.entries)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        target = self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            payload = json.dumps([e.to_dict() for e in self.entries], indent=2)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("activity_log: persistence failed: %s", exc)
        except Exception as exc:
            log.warning("activity_log: persistence error: %s", exc)

    @classmethod
    def load(
        cls,
        *,
        max_entries: int = ACTIVITY_LOG_MAX,
        path: Path | None = None,
    ) -> ActivityLog:
        target = path if path is not None else activity_log_path()
        if not target.exists():
            return cls(max_entries=max_entries, path=path)
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("activity_log: load failed (%s); starting empty", exc)
            return cls(max_entries=max_entries, path=path)
        if not isinstance(data, list):
            log.warning("activity_log: malformed payload (not a list); starting empty")
            return cls(max_entries=max_entries, path=path)
        entries: list[ActivityEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(ActivityEntry.from_dict(item))
            except (ValueError, TypeError) as exc:
                log.warning("activity_log: skipping malformed entry: %s", exc)
        return cls(entries=entries, max_entries=max_entries, path=path)


__all__ = ["ActivityEntry", "ActivityLog"]
