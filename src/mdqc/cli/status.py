"""mdqc status — show agent status and queue. Thin client of /api/status."""

from __future__ import annotations

import typer


def status_cmd() -> None:
    """Show service status, queue, and recent activity."""
    from mdqc.ipc.client import IpcClient

    client = IpcClient.from_runtime_file()
    status = client.get_status()
    typer.echo(status.render_text())
