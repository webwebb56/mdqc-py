"""Log viewer: last 50 lines + SSE tail."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from mdqc.config import paths
from mdqc.webui._deps import common_context, get_templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/logs")

LOG_FILENAME = "mdqc.log"
TAIL_LINES = 50
POLL_INTERVAL_S = 1.0


def _log_path() -> Path:
    return paths.log_dir() / LOG_FILENAME


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                fh.seek(size)
                data = fh.read(read_size) + data
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        return lines[-n:]
    except OSError:
        return []


@router.get("", response_class=HTMLResponse)
async def logs_index(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    log_path = _log_path()
    lines = _tail_lines(log_path, TAIL_LINES)
    ctx = common_context(request)
    ctx["log_path"] = str(log_path)
    ctx["lines"] = lines
    return templates.TemplateResponse(request, "logs/index.html", ctx)


async def _tail_stream(path: Path, request: Request) -> AsyncGenerator[dict[str, str], None]:
    last_size = path.stat().st_size if path.exists() else 0
    while True:
        if await request.is_disconnected():
            break
        try:
            if not path.exists():
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            current_size = path.stat().st_size
            if current_size < last_size:
                last_size = 0
            if current_size > last_size:
                with open(path, "rb") as fh:
                    fh.seek(last_size)
                    chunk = fh.read(current_size - last_size)
                last_size = current_size
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    yield {"event": "message", "data": line}
        except OSError:
            pass
        await asyncio.sleep(POLL_INTERVAL_S)


@router.get("/stream")
async def logs_stream(request: Request) -> EventSourceResponse:
    path = _log_path()
    return EventSourceResponse(_tail_stream(path, request))


__all__ = ["router"]
