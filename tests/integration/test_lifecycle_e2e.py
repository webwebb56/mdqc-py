from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from mdqc.config.defaults import IPC_HEADER, SHUTDOWN_GRACE_S
from mdqc.ipc.runtime import RuntimeFile
from mdqc.service.lifecycle import main_async


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_boots_and_health_returns_ok(tmp_data_dir: Path) -> None:
    cfg_path = tmp_data_dir / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [agent]
            agent_id = "integ"

            [skyline]
            path = "/nonexistent"
            """
        ),
        encoding="utf-8",
    )

    service_task = asyncio.create_task(main_async(service_mode=False))

    rf = RuntimeFile()
    info = None
    for _ in range(40):
        info = rf.read()
        if info is not None:
            break
        await asyncio.sleep(0.25)

    try:
        assert info is not None, "service did not write runtime.json"
        assert info.port > 0
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"http://127.0.0.1:{info.port}/api/health",
                headers={IPC_HEADER: info.token},
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True}

            response_no_auth = await client.get(
                f"http://127.0.0.1:{info.port}/api/health"
            )
            assert response_no_auth.status_code == 401
    finally:
        # Send SIGTERM-like shutdown by setting the stop event via signal.
        with contextlib.suppress(Exception):
            signal.raise_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(service_task, timeout=SHUTDOWN_GRACE_S + 5)
        except TimeoutError:
            service_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await service_task
