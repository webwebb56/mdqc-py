"""mdqc selfcheck — verify the frozen build has all required modules.

This is the canary for the "Windows extras missed" packaging trap from
docs/AGENT_NOTES § Things that ARE bugs. CI runs this on the PyInstaller
output before publishing the installer.
"""

from __future__ import annotations

import importlib
import sys

import typer

REQUIRED_ALWAYS = [
    "fastapi",
    "httpx",
    "pydantic",
    "structlog",
    "tenacity",
    "uvicorn",
    "watchdog",
    "watchdog.observers",
    "psutil",
]

REQUIRED_WINDOWS = [
    "win32file",
    "win32api",
    "winreg",
    "watchdog.observers.read_directory_changes",
    "cryptography.hazmat.primitives.serialization.pkcs12",
]

# Optional modules — only checked if the package was installed with the
# matching extra. Missing entries here are reported as warnings, not failures,
# because the agent's core path (watcher → extractor → spool) doesn't depend
# on them.
OPTIONAL = [
    ("pystray",                            "[tray]"),
    ("winsdk.windows.ui.notifications",    "[tray]"),
    ("winsdk.windows.data.xml.dom",        "[tray]"),
    ("streamlit",                          "[plots]"),
    ("plotly",                             "[plots]"),
    ("pandas",                             "[plots]"),
]


def selfcheck_cmd() -> None:
    """Import every module the runtime depends on. Fail loudly on the first miss."""
    failures: list[tuple[str, str]] = []
    warnings: list[tuple[str, str, str]] = []
    required = list(REQUIRED_ALWAYS)
    if sys.platform == "win32":
        required += REQUIRED_WINDOWS

    for mod in required:
        try:
            importlib.import_module(mod)
        except Exception as e:
            failures.append((mod, f"{type(e).__name__}: {e}"))

    for mod, extra in OPTIONAL:
        try:
            importlib.import_module(mod)
        except Exception as e:
            warnings.append((mod, extra, f"{type(e).__name__}: {e}"))

    if failures:
        typer.echo("mdqc selfcheck FAILED:", err=True)
        for mod, err in failures:
            typer.echo(f"  - {mod}: {err}", err=True)
        raise typer.Exit(1)

    if warnings:
        typer.echo(
            f"mdqc selfcheck OK ({len(required)} core modules imported, "
            f"{len(warnings)} optional missing):"
        )
        for mod, extra, _ in warnings:
            typer.echo(f"  - {mod}: install {extra} extra to enable")
    else:
        typer.echo(f"mdqc selfcheck OK ({len(required)} modules imported).")
