"""mdqc run — start the agent.

Two modes:
- --foreground: one process in the user's session (the tray is separate: `mdqc tray`)
- --service-mode: NSSM-managed, headless, no tray
"""

from __future__ import annotations

import typer


def run_cmd(
    foreground: bool = typer.Option(False, "--foreground", help="Run in the foreground (dev mode)."),
    service_mode: bool = typer.Option(
        False, "--service-mode", help="Run as a headless background service (NSSM)."
    ),
) -> None:
    """Start the agent (watcher + extractor + spool + uploader + IPC server)."""
    if not foreground and not service_mode:
        typer.echo("Specify either --foreground or --service-mode.", err=True)
        raise typer.Exit(2)

    from mdqc.service.lifecycle import main_blocking

    main_blocking(service_mode=service_mode)
