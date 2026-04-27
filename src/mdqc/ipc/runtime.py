from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mdqc.config import paths
from mdqc.config.defaults import IPC_TOKEN_BYTES

log = logging.getLogger(__name__)


@dataclass
class RuntimeInfo:
    port: int
    token: str
    pid: int
    started_at: datetime

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = asdict(self)
        d["started_at"] = self.started_at.astimezone(UTC).isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RuntimeInfo:
        started_raw = data.get("started_at")
        if isinstance(started_raw, str):
            started = datetime.fromisoformat(started_raw)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        elif isinstance(started_raw, (int, float)):
            started = datetime.fromtimestamp(float(started_raw), tz=UTC)
        else:
            started = datetime.now(UTC)
        return cls(
            port=int(data.get("port", 0) or 0),
            token=str(data.get("token", "")),
            pid=int(data.get("pid", 0) or 0),
            started_at=started,
        )


def generate_token() -> str:
    return secrets.token_urlsafe(IPC_TOKEN_BYTES)


class RuntimeFile:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else paths.runtime_file()

    def write(self, info: RuntimeInfo) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        payload = json.dumps(info.to_dict(), indent=2).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        if sys.platform == "win32":
            self._try_set_acls(target)

    @staticmethod
    def _try_set_acls(target: Path) -> None:
        try:
            import ctypes  # noqa: F401
        except Exception as exc:
            log.warning("runtime_acl_failed", extra={"error": str(exc)})

    def read(self) -> RuntimeInfo | None:
        target = self.path
        if not target.exists():
            return None
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return RuntimeInfo.from_dict(data)
        except (TypeError, ValueError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("runtime_clear_failed", extra={"error": str(exc)})

    def wait_for(self, timeout_s: float) -> RuntimeInfo:
        deadline = time.monotonic() + timeout_s
        poll_interval_s = 0.25
        while True:
            info = self.read()
            if info is not None:
                return info
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"runtime.json not found at {self.path} after {timeout_s}s"
                )
            time.sleep(poll_interval_s)


__all__ = ["RuntimeFile", "RuntimeInfo", "generate_token"]
