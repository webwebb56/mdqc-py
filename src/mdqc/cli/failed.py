"""mdqc failed <subcommand> — manage the failed-files store."""

from __future__ import annotations

import typer

failed_app = typer.Typer(help="Manage failed-files history.", no_args_is_help=True)


@failed_app.command("list")
def list_cmd() -> None:
    """Show files that failed extraction."""
    from mdqc.failed_files import FailedFilesStore

    store = FailedFilesStore.load()
    if not store.entries:
        typer.echo("No failed files.")
        return
    for e in store.entries:
        typer.echo(f"{e.failed_at.isoformat()}  {e.path}  retries={e.retry_count}  {e.reason}")


@failed_app.command("retry")
def retry_cmd(path: str = typer.Argument(..., help='File path, or "all".')) -> None:
    """Retry a specific failed file (or "all")."""
    from mdqc.ipc.client import IpcClient

    client = IpcClient.from_runtime_file()
    n = client.retry_failed(path)
    typer.echo(f"Re-enqueued {n} file(s).")


@failed_app.command("clear")
def clear_cmd() -> None:
    """Clear the failed-files list."""
    from mdqc.failed_files import FailedFilesStore

    store = FailedFilesStore.load()
    n = len(store.entries)
    store.clear()
    typer.echo(f"Cleared {n} entries.")
