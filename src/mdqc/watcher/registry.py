from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

from mdqc.config import defaults, paths


class ProcessedRegistry:
    def __init__(self) -> None:
        self._path = paths.processed_registry_path()
        self._entries: deque[str] = deque()
        self._set: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                return
        except (OSError, json.JSONDecodeError):
            return
        for item in data:
            if isinstance(item, str) and item not in self._set:
                self._entries.append(item)
                self._set.add(item)

    def contains(self, path: Path) -> bool:
        return str(path) in self._set

    def add(self, path: Path) -> None:
        key = str(path)
        if key in self._set:
            return
        self._entries.append(key)
        self._set.add(key)
        while len(self._entries) > defaults.PROCESSED_REGISTRY_MAX:
            evicted = self._entries.popleft()
            self._set.discard(evicted)
        self._persist()

    def clear(self) -> None:
        self._entries.clear()
        self._set.clear()
        self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps(list(self._entries), indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    def __len__(self) -> int:
        return len(self._entries)
