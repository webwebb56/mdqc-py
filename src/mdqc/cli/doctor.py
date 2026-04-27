"""mdqc doctor — system health check."""

from __future__ import annotations

import typer


def doctor_cmd() -> None:
    """Check Skyline install, vendor readers, templates, certs, cloud connectivity."""
    from mdqc.diagnostics import render_text_report, run_diagnostics_blocking

    report = run_diagnostics_blocking()
    typer.echo(render_text_report(report))
    raise typer.Exit(code=0 if report.overall_ok else 1)
