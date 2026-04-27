"""mdqc reprocess <file> — manually re-run a file through the pipeline."""

from __future__ import annotations

from pathlib import Path

import typer

reprocess_app = typer.Typer(help="Reprocess files through the pipeline.", no_args_is_help=True)


@reprocess_app.command("file")
def reprocess_file(
    path: Path = typer.Argument(..., help="Path to raw file."),  # noqa: B008
) -> None:
    """Re-enqueue a single file with the running service."""
    from mdqc.ipc.client import IpcClient

    client = IpcClient.from_runtime_file()
    client.reprocess(path)
    typer.echo(f"Enqueued: {path}")
