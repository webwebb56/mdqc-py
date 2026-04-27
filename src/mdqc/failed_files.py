from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mdqc.config.defaults import FAILED_FILES_MAX
from mdqc.config.paths import failed_files_path

log = logging.getLogger(__name__)


@dataclass
class FailedFileEntry:
    path: str
    instrument_id: str | None
    reason: str
    failed_at: datetime
    retry_count: int = 0
    seq: int = 0

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["failed_at"] = self.failed_at.astimezone(UTC).isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FailedFileEntry:
        failed_at_raw = data.get("failed_at")
        if isinstance(failed_at_raw, str):
            failed_at = datetime.fromisoformat(failed_at_raw)
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=UTC)
        else:
            failed_at = datetime.now(UTC)
        return cls(
            path=str(data.get("path", "")),
            instrument_id=data.get("instrument_id"),  # type: ignore[arg-type]
            reason=str(data.get("reason", "")),
            failed_at=failed_at,
            retry_count=int(data.get("retry_count", 0) or 0),
            seq=int(data.get("seq", 0) or 0),
        )


# Spec: when a same-path entry is added again, update in place (increment
# retry_count, refresh reason/failed_at/seq). The Rust version's HashMap insert
# replaces the entry and resets retry_count to 0; this is the deliberate
# improvement over Rust documented in the task.
class FailedFilesStore:
    def __init__(
        self,
        entries: list[FailedFileEntry] | None = None,
        *,
        max_entries: int = FAILED_FILES_MAX,
        path: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._path = path
        self._seq_counter = 0
        self.entries: list[FailedFileEntry] = []
        for entry in entries or []:
            if entry.seq >= self._seq_counter:
                self._seq_counter = entry.seq + 1
            self.entries.append(entry)
        self._sort_locked()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else failed_files_path()

    def _sort_locked(self) -> None:
        self.entries.sort(key=lambda e: (e.failed_at, e.seq))

    def _next_seq_locked(self) -> int:
        s = self._seq_counter
        self._seq_counter += 1
        return s

    def _trim_locked(self) -> None:
        while len(self.entries) > self._max_entries:
            self.entries.pop(0)

    def _find_locked(self, path: str) -> FailedFileEntry | None:
        for entry in self.entries:
            if entry.path == path:
                return entry
        return None

    def add(self, path: str, instrument_id: str | None, reason: str) -> None:
        with self._lock:
            now = datetime.now(UTC)
            existing = self._find_locked(path)
            if existing is not None:
                existing.retry_count += 1
                existing.reason = reason
                existing.failed_at = now
                existing.seq = self._next_seq_locked()
                if existing.instrument_id is None and instrument_id is not None:
                    existing.instrument_id = instrument_id
            else:
                entry = FailedFileEntry(
                    path=path,
                    instrument_id=instrument_id,
                    reason=reason,
                    failed_at=now,
                    retry_count=0,
                    seq=self._next_seq_locked(),
                )
                self.entries.append(entry)
            self._sort_locked()
            self._trim_locked()
            self._save_locked()

    def remove(self, path: str) -> bool:
        with self._lock:
            for i, entry in enumerate(self.entries):
                if entry.path == path:
                    del self.entries[i]
                    self._save_locked()
                    return True
            return False

    def increment_retry(self, path: str) -> None:
        with self._lock:
            entry = self._find_locked(path)
            if entry is None:
                return
            entry.retry_count += 1
            self._save_locked()

    def clear(self) -> None:
        with self._lock:
            self.entries.clear()
            self._save_locked()

    def find(self, path: str) -> FailedFileEntry | None:
        with self._lock:
            entry = self._find_locked(path)
            if entry is None:
                return None
            return FailedFileEntry(
                path=entry.path,
                instrument_id=entry.instrument_id,
                reason=entry.reason,
                failed_at=entry.failed_at,
                retry_count=entry.retry_count,
                seq=entry.seq,
            )

    def all(self) -> list[FailedFileEntry]:
        with self._lock:
            return [
                FailedFileEntry(
                    path=e.path,
                    instrument_id=e.instrument_id,
                    reason=e.reason,
                    failed_at=e.failed_at,
                    retry_count=e.retry_count,
                    seq=e.seq,
                )
                for e in self.entries
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
            payload = json.dumps(
                [e.to_dict() for e in self.entries], indent=2, sort_keys=True
            )
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("failed_files: persistence failed: %s", exc)
        except Exception as exc:
            log.warning("failed_files: persistence error: %s", exc)

    @classmethod
    def load(
        cls,
        *,
        max_entries: int = FAILED_FILES_MAX,
        path: Path | None = None,
    ) -> FailedFilesStore:
        target = path if path is not None else failed_files_path()
        if not target.exists():
            return cls(max_entries=max_entries, path=path)
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("failed_files: load failed (%s); starting empty", exc)
            return cls(max_entries=max_entries, path=path)
        if not isinstance(data, list):
            log.warning("failed_files: malformed payload (not a list); starting empty")
            return cls(max_entries=max_entries, path=path)
        entries: list[FailedFileEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(FailedFileEntry.from_dict(item))
            except (ValueError, TypeError) as exc:
                log.warning("failed_files: skipping malformed entry: %s", exc)
        return cls(entries=entries, max_entries=max_entries, path=path)


__all__ = ["FailedFileEntry", "FailedFilesStore"]
