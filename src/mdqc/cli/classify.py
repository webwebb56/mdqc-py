"""mdqc classify <file> — preview classification without processing."""

from __future__ import annotations

from pathlib import Path

import typer


def classify_cmd(
    path: Path = typer.Argument(..., help="Path to a raw file or directory."),  # noqa: B008
) -> None:
    """Preview how a file would be classified by the agent."""
    from mdqc.classifier import classify_file

    result = classify_file(path)
    typer.echo("Classification Result")
    typer.echo("=====================")
    typer.echo(f"File: {path}")
    typer.echo(f"Control Type: {result.control_type.value}")
    typer.echo(f"Well Position: {result.well_position or '-'}")
    typer.echo(f"Instrument: {result.instrument_id or '-'}")
    typer.echo(f"Plate ID: {result.plate_id or '-'}")
    typer.echo(f"Confidence: {result.confidence.value}")
    typer.echo(f"Source: {result.source.value}")
