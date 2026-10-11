"""mdqc classify <file> — preview classification without processing."""

from __future__ import annotations

from pathlib import Path

import typer


def classify_cmd(
    path: Path = typer.Argument(..., help="Path to a raw file or directory."),  # noqa: B008
) -> None:
    """Preview how a file would be classified by the agent."""
    from mdqc.classifier import classify_file
    from mdqc.config import load_config
    from mdqc.config.schema import CONTROL_TYPE_LABELS

    # Use the site's own rules when there is a config, because the agent does:
    # a preview that ignored them could approve a naming scheme the running
    # agent then classifies differently.
    rules = None
    mode = "lenient"
    rules_used = "built-in markers only (no config.toml found)"
    try:
        cfg = load_config(strict_cert_guard=False)
        rules, mode = cfg.classifier_rules, cfg.classification_mode
        rules_used = f"config.toml rules ({len(rules)}), then built-in markers ({mode} mode)"
    except Exception:  # no config, or one that doesn't validate: preview without it
        pass

    result = classify_file(path, rules=rules, mode=mode)
    value = result.control_type.value
    label = CONTROL_TYPE_LABELS.get(value, value)
    name = label.split(" — ", 1)[1] if " — " in label else ""

    typer.echo("Classification Result")
    typer.echo("=====================")
    typer.echo(f"File: {path}")
    typer.echo(f"Control Type: {value}" + (f" ({name})" if name else ""))
    # SPD selects which gold-standard baseline a run is compared against, and
    # dilution groups a threshold-calibration series, so both belong in the
    # preview an engineer uses to verify a site's naming convention. So do the
    # LC serial and, for a PCD run, the kit it came from.
    typer.echo(f"SPD: {result.spd if result.spd is not None else '-'}")
    typer.echo(
        f"Dilution: {str(result.dilution_pct) + '%' if result.dilution_pct is not None else '-'}"
    )
    typer.echo(f"Kit: {str(result.kit_format) + '-well' if result.kit_format else '-'}")
    typer.echo(f"LC Serial: {result.lc_serial or '-'}")
    typer.echo(f"Well Position: {result.well_position or '-'}")
    typer.echo(f"Instrument: {result.instrument_id or '-'}")
    typer.echo(f"Plate ID: {result.plate_id or '-'}")
    typer.echo(f"Confidence: {result.confidence.value}")
    typer.echo(f"Source: {result.source.value}")
    typer.echo(f"Rules: {rules_used}")
