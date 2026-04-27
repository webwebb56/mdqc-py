"""Top-level typer app and subcommand registration.

Subcommand modules each define a typer.Typer() that's added here. Subcommands
that aren't implemented yet show a stub message but exit cleanly.
"""

from __future__ import annotations

import typer

from mdqc import __version__

app = typer.Typer(
    name="mdqc",
    help="MD QC Agent — automated quality control monitoring for mass spectrometry instruments.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("version")
def version_cmd() -> None:
    """Show version information."""
    typer.echo(f"mdqc {__version__}")


# Subcommand modules register themselves here. Imports are lazy via try/except
# so that a missing/broken module doesn't take down the whole CLI during the
# build-out phase.
def _register() -> None:
    from mdqc.cli import classify as _classify
    from mdqc.cli import config_cmd as _config_cmd
    from mdqc.cli import doctor as _doctor
    from mdqc.cli import failed as _failed
    from mdqc.cli import reprocess as _reprocess
    from mdqc.cli import run as _run
    from mdqc.cli import selfcheck as _selfcheck
    from mdqc.cli import status as _status
    from mdqc.cli import tray as _tray

    app.command("doctor")(_doctor.doctor_cmd)
    app.command("status")(_status.status_cmd)
    app.command("classify")(_classify.classify_cmd)
    app.command("run")(_run.run_cmd)
    app.command("tray")(_tray.tray_cmd)
    app.command("selfcheck")(_selfcheck.selfcheck_cmd)
    app.add_typer(_config_cmd.config_app, name="config")
    app.add_typer(_failed.failed_app, name="failed")
    app.add_typer(_reprocess.reprocess_app, name="reprocess")


_register()
