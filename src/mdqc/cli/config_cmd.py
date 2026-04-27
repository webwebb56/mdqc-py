"""mdqc config <subcommand> — config inspection."""

from __future__ import annotations

import typer

config_app = typer.Typer(help="Validate or inspect configuration.", no_args_is_help=True)


@config_app.command("validate")
def validate() -> None:
    """Check the config file for errors."""
    from mdqc.config import load_or_exit

    cfg = load_or_exit()
    typer.echo(f"Config OK ({len(cfg.instruments)} instrument(s) configured).")


@config_app.command("show")
def show() -> None:
    """Print the parsed config as JSON."""
    from mdqc.config import load_or_exit

    cfg = load_or_exit()
    typer.echo(cfg.model_dump_json(indent=2))


@config_app.command("path")
def path_cmd() -> None:
    """Print the resolved config file path."""
    from mdqc.config import paths

    typer.echo(str(paths.config_path()))
