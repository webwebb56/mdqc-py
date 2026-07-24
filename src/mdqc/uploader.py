"""Cloud uploader.

See docs/AGENT_NOTES.md § Uploader. Critical: 4-entry wait_chain (NOT 5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import tenacity

from mdqc.config.defaults import (
    HTTP_CONNECT_TIMEOUT_S,
    HTTP_TIMEOUT_S,
    UPLOAD_RETRY_SLEEPS,
    UPLOAD_TOTAL_ATTEMPTS,
)
from mdqc.config.schema import CloudConfig
from mdqc.spool import Spool

log = logging.getLogger(__name__)


class UploadError(Exception):
    pass


class TransientUploadError(UploadError):
    pass


class PermanentUploadError(UploadError):
    pass


class AuthenticationError(PermanentUploadError):
    pass


def _classify_status(status: int, body: str) -> UploadError | None:
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return AuthenticationError(f"status {status}: {body}")
    if status == 408 or status == 429 or 500 <= status < 600:
        return TransientUploadError(f"status {status}: {body}")
    return PermanentUploadError(f"status {status}: {body}")


SleepFn = Callable[[float], Awaitable[None]]


class Uploader:
    def __init__(
        self,
        cloud_cfg: CloudConfig,
        *,
        agent_version: str,
        http_client: httpx.AsyncClient | None = None,
        _sleep: SleepFn | None = None,
    ) -> None:
        if cloud_cfg.certificate_thumbprint and not cloud_cfg.api_token:
            raise RuntimeError(
                "[cloud] certificate_thumbprint is set but mTLS via the Windows "
                "certificate store is not yet implemented in the Python agent. "
                "Either set [cloud] api_token, or pin to the Rust agent until v1.1."
            )

        self.cloud_cfg = cloud_cfg
        self.agent_version = agent_version

        if http_client is None:
            client_kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(HTTP_TIMEOUT_S, connect=HTTP_CONNECT_TIMEOUT_S),
                "trust_env": True,
            }
            if cloud_cfg.proxy:
                client_kwargs["proxy"] = cloud_cfg.proxy
            http_client = httpx.AsyncClient(**client_kwargs)
        self._client = http_client

        wait_entries = [tenacity.wait_random(min=lo, max=hi) for (lo, hi) in UPLOAD_RETRY_SLEEPS]
        retrying_kwargs: dict[str, Any] = {
            "stop": tenacity.stop_after_attempt(UPLOAD_TOTAL_ATTEMPTS),
            "wait": tenacity.wait_chain(*wait_entries),
            "retry": tenacity.retry_if_exception_type(TransientUploadError),
            "reraise": True,
        }
        if _sleep is not None:
            retrying_kwargs["sleep"] = _sleep
        self._retrying = tenacity.AsyncRetrying(**retrying_kwargs)

    @property
    def is_local_only(self) -> bool:
        return not self.cloud_cfg.api_token and not self.cloud_cfg.certificate_thumbprint

    async def upload_payload(self, payload: dict[str, Any], filename: str) -> None:
        # The platform's POST /api/evosep_qcs expects the payload wrapped as
        # {"filename": ..., "blob": <payload object>}, not the bare payload.
        envelope = {"filename": filename, "blob": payload}
        body = json.dumps(envelope).encode("utf-8")

        async def _attempt() -> None:
            await self._post_once(body)

        await self._retrying(_attempt)

    async def _post_once(self, body: bytes) -> None:
        headers = {
            "User-Agent": f"mdqc-py/{self.agent_version}",
            "Content-Type": "application/json",
            # The platform serves responses as its versioned media type.
            "Accept": "application/vnd.md-v2+json",
        }
        if self.cloud_cfg.api_token:
            headers["Authorization"] = f"Bearer {self.cloud_cfg.api_token}"

        try:
            response = await self._client.post(
                self.cloud_cfg.endpoint,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise TransientUploadError(f"network error: {e!r}") from e

        text = ""
        try:
            text = response.text
        except Exception:
            text = ""

        err = _classify_status(response.status_code, text)
        if err is not None:
            raise err

    async def aclose(self) -> None:
        await self._client.aclose()


class UploaderWorker:
    def __init__(
        self,
        spool: Spool,
        uploader: Uploader,
        *,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.spool = spool
        self.uploader = uploader
        self.poll_interval_s = poll_interval_s
        self._logged_local_only = False

    async def upload_one(self) -> bool:
        claimed = self.spool.claim_next()
        if claimed is None:
            return False
        path, payload = claimed

        if self.uploader.is_local_only:
            self.spool.mark_completed(path)
            return True

        try:
            await self.uploader.upload_payload(payload, path.name)
        except (PermanentUploadError, AuthenticationError) as e:
            log.error("Permanent upload failure", extra={"path": str(path), "error": str(e)})
            self.spool.mark_failed(path, str(e))
            return True
        except tenacity.RetryError as e:
            log.error("Upload retries exhausted", extra={"path": str(path), "error": str(e)})
            self.spool.mark_failed(path, f"retries exhausted: {e!r}")
            return True
        except TransientUploadError as e:
            log.error("Transient upload failure surfaced", extra={"path": str(path), "error": str(e)})
            self.spool.mark_failed(path, str(e))
            return True

        self.spool.mark_completed(path)
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        if self.uploader.is_local_only and not self._logged_local_only:
            log.info(
                "Running in local-only mode (no cloud auth configured). "
                "Payloads will be retained locally only."
            )
            self._logged_local_only = True

        while not stop_event.is_set():
            processed = await self.upload_one()
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_s)
