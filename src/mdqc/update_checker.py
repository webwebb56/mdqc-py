"""GitHub releases update checker.

Throttled to one network call per UPDATE_CHECK_INTERVAL_S. Sends
``If-Modified-Since`` to avoid GitHub rate limits in heavily-restarted
environments (this is the bug-fix vs the Rust version).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from mdqc import __version__ as _agent_version
from mdqc.config import paths
from mdqc.config.defaults import GITHUB_RELEASES_API, UPDATE_CHECK_INTERVAL_S
from mdqc.log import get_logger

log = get_logger(__name__)


@dataclass
class UpdateInfo:
    version: str
    tag_name: str
    release_url: str
    published_at: datetime | None = None


@dataclass
class _CheckState:
    last_check: datetime | None = None
    last_modified_header: str | None = None
    latest_known_version: str | None = None

    @classmethod
    def load(cls, path: Path) -> _CheckState:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            log.warning("update_state_read_failed", error=str(exc))
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        last_check_raw = data.get("last_check")
        last_check: datetime | None = None
        if isinstance(last_check_raw, str):
            try:
                parsed = datetime.fromisoformat(last_check_raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                last_check = parsed
            except ValueError:
                last_check = None
        return cls(
            last_check=last_check,
            last_modified_header=data.get("last_modified_header"),
            latest_known_version=data.get("latest_known_version"),
        )

    def save(self, path: Path) -> None:
        payload = {
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_modified_header": self.last_modified_header,
            "latest_known_version": self.latest_known_version,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_str = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            tmp_path = Path(tmp_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp_path, path)
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                raise
        except OSError as exc:
            log.warning("update_state_write_failed", error=str(exc))


def _parse_version(tag: str) -> tuple[int, ...] | None:
    stripped = tag.lstrip("vV").strip()
    if not stripped:
        return None
    parts = stripped.split(".")
    out: list[int] = []
    for part in parts:
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        try:
            out.append(int(digits))
        except ValueError:
            return None
    return tuple(out) if out else None


def _is_newer(remote: str, current: str) -> bool | None:
    r = _parse_version(remote)
    c = _parse_version(current)
    if r is None or c is None:
        return None
    length = max(len(r), len(c))
    r_padded = r + (0,) * (length - len(r))
    c_padded = c + (0,) * (length - len(c))
    return r_padded > c_padded


class UpdateChecker:
    def __init__(
        self,
        *,
        current_version: str | None = None,
        state_path: Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        api_url: str = GITHUB_RELEASES_API,
        check_interval_s: float = UPDATE_CHECK_INTERVAL_S,
    ) -> None:
        self.current_version = current_version or _agent_version
        self.state_path = state_path or paths.update_state_path()
        self._client = http_client
        self._owns_client = http_client is None
        self.api_url = api_url
        self.check_interval_s = check_interval_s

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"User-Agent": f"mdqc-agent/{self.current_version}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cached_info(self, state: _CheckState) -> UpdateInfo | None:
        if not state.latest_known_version:
            return None
        verdict = _is_newer(state.latest_known_version, self.current_version)
        if not verdict:
            return None
        tag = state.latest_known_version
        if not tag.startswith("v"):
            tag = f"v{tag}"
        return UpdateInfo(
            version=state.latest_known_version,
            tag_name=tag,
            release_url=f"https://github.com/webwebb56/mdqc-py/releases/tag/{tag}",
        )

    async def check(self) -> UpdateInfo | None:
        state = _CheckState.load(self.state_path)

        now = datetime.now(UTC)
        if state.last_check is not None:
            elapsed = (now - state.last_check).total_seconds()
            if elapsed < self.check_interval_s:
                return self._cached_info(state)

        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if state.last_modified_header:
            headers["If-Modified-Since"] = state.last_modified_header

        client = await self._get_client()
        try:
            response = await client.get(self.api_url, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("update_check_failed", error=str(exc))
            state.last_check = now
            state.save(self.state_path)
            return self._cached_info(state)

        if response.status_code == 304:
            state.last_check = now
            state.save(self.state_path)
            return self._cached_info(state)

        if response.status_code != 200:
            log.warning(
                "update_check_unexpected_status", status=response.status_code
            )
            state.last_check = now
            state.save(self.state_path)
            return self._cached_info(state)

        try:
            data: Any = response.json()
        except json.JSONDecodeError as exc:
            log.warning("update_check_bad_json", error=str(exc))
            state.last_check = now
            state.save(self.state_path)
            return None

        tag_name = data.get("tag_name") if isinstance(data, dict) else None
        html_url = data.get("html_url") if isinstance(data, dict) else None
        published_at_raw = data.get("published_at") if isinstance(data, dict) else None

        if not isinstance(tag_name, str) or not isinstance(html_url, str):
            state.last_check = now
            state.save(self.state_path)
            return None

        version = tag_name.lstrip("vV")
        verdict = _is_newer(version, self.current_version)

        new_state = _CheckState(
            last_check=now,
            last_modified_header=response.headers.get("Last-Modified")
            or state.last_modified_header,
            latest_known_version=version,
        )
        new_state.save(self.state_path)

        if verdict is None or not verdict:
            return None

        published_at: datetime | None = None
        if isinstance(published_at_raw, str):
            try:
                parsed = datetime.fromisoformat(
                    published_at_raw.replace("Z", "+00:00")
                )
                published_at = parsed
            except ValueError:
                published_at = None

        return UpdateInfo(
            version=version,
            tag_name=tag_name,
            release_url=html_url,
            published_at=published_at,
        )


__all__ = [
    "UpdateChecker",
    "UpdateInfo",
]
