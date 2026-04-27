from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from mdqc.activity_log import ActivityEntry
from mdqc.config.defaults import IPC_HEADER
from mdqc.ipc.runtime import RuntimeFile

log = logging.getLogger(__name__)


class IpcUnavailable(RuntimeError):
    pass


@dataclass
class StatusReport:
    service_running: bool
    uptime_s: int
    paused: bool
    pending_count: int
    uploading_count: int
    failed_count: int
    recent_activity: list[ActivityEntry] = field(default_factory=list)
    local_only_mode: bool = False

    def render_text(self) -> str:
        lines: list[str] = []
        lines.append("MD QC Agent Status")
        lines.append("==================")
        lines.append(f"Service running: {'yes' if self.service_running else 'no'}")
        lines.append(f"Paused: {'yes' if self.paused else 'no'}")
        lines.append(f"Uptime: {self.uptime_s}s")
        lines.append(f"Local-only mode: {'yes' if self.local_only_mode else 'no'}")
        lines.append("")
        lines.append("Queue")
        lines.append("-----")
        lines.append(f"Pending: {self.pending_count}")
        lines.append(f"Uploading: {self.uploading_count}")
        lines.append(f"Failed: {self.failed_count}")
        lines.append("")
        lines.append("Recent activity")
        lines.append("---------------")
        if not self.recent_activity:
            lines.append("(none)")
        else:
            for entry in self.recent_activity:
                lines.append(
                    f"{entry.timestamp.isoformat()}  {entry.result.value}  {entry.path}"
                )
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatusReport:
        recent_raw = data.get("recent_activity", []) or []
        recent: list[ActivityEntry] = []
        for item in recent_raw:
            if isinstance(item, dict):
                try:
                    recent.append(ActivityEntry.from_dict(item))
                except (ValueError, TypeError):
                    continue
        return cls(
            service_running=bool(data.get("service_running", False)),
            uptime_s=int(data.get("uptime_s", 0) or 0),
            paused=bool(data.get("paused", False)),
            pending_count=int(data.get("pending_count", 0) or 0),
            uploading_count=int(data.get("uploading_count", 0) or 0),
            failed_count=int(data.get("failed_count", 0) or 0),
            recent_activity=recent,
            local_only_mode=bool(data.get("local_only_mode", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_running": self.service_running,
            "uptime_s": self.uptime_s,
            "paused": self.paused,
            "pending_count": self.pending_count,
            "uploading_count": self.uploading_count,
            "failed_count": self.failed_count,
            "recent_activity": [e.to_dict() for e in self.recent_activity],
            "local_only_mode": self.local_only_mode,
        }


class IpcClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 5.0,
        runtime_file: RuntimeFile | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self._runtime_file = runtime_file
        self._client = client or httpx.Client(timeout=timeout_s)

    @classmethod
    def from_runtime_file(
        cls,
        timeout_s: float = 5.0,
        *,
        runtime_file: RuntimeFile | None = None,
        client: httpx.Client | None = None,
    ) -> IpcClient:
        rf = runtime_file or RuntimeFile()
        info = rf.read()
        if info is None:
            raise IpcUnavailable(
                f"runtime.json not found at {rf.path}; is the service running?"
            )
        base_url = f"http://127.0.0.1:{info.port}"
        return cls(
            base_url=base_url,
            token=info.token,
            timeout_s=timeout_s,
            runtime_file=rf,
            client=client,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> IpcClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {IPC_HEADER: self.token}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                headers=self._headers(),
                json=json_body,
            )
        except httpx.RequestError as exc:
            raise IpcUnavailable(f"connection to service failed: {exc!r}") from exc

        if response.status_code == 401 and self._runtime_file is not None:
            info = self._runtime_file.read()
            if info is not None:
                self.token = info.token
                self.base_url = f"http://127.0.0.1:{info.port}"
                url = f"{self.base_url}{path}"
                try:
                    response = self._client.request(
                        method,
                        url,
                        headers=self._headers(),
                        json=json_body,
                    )
                except httpx.RequestError as exc:
                    raise IpcUnavailable(
                        f"connection to service failed after token rotate: {exc!r}"
                    ) from exc

        return response

    def health(self) -> bool:
        try:
            response = self._request("GET", "/api/health")
        except IpcUnavailable:
            return False
        return response.status_code == 200

    def get_status(self) -> StatusReport:
        response = self._request("GET", "/api/status")
        response.raise_for_status()
        return StatusReport.from_dict(response.json())

    def get_diagnostics(self) -> dict[str, Any]:
        response = self._request("GET", "/api/diagnostics")
        response.raise_for_status()
        return dict(response.json())

    def pause(self) -> None:
        response = self._request("POST", "/api/pause")
        response.raise_for_status()

    def resume(self) -> None:
        response = self._request("POST", "/api/resume")
        response.raise_for_status()

    def reprocess(self, path: Path) -> None:
        response = self._request("POST", "/api/reprocess", json_body={"path": str(path)})
        response.raise_for_status()

    def retry_failed(self, path: str) -> int:
        response = self._request(
            "POST", "/api/failed/retry", json_body={"path": path}
        )
        response.raise_for_status()
        body = response.json()
        return int(body.get("count", 0) or 0)

    def clear_failed(self) -> None:
        response = self._request("POST", "/api/failed/clear")
        response.raise_for_status()

    def get_config(self) -> dict[str, Any]:
        response = self._request("GET", "/api/config")
        response.raise_for_status()
        return dict(response.json())

    def update_config(self, payload: dict[str, Any]) -> None:
        response = self._request("PUT", "/api/config", json_body=payload)
        response.raise_for_status()


__all__ = [
    "IpcClient",
    "IpcUnavailable",
    "StatusReport",
]
