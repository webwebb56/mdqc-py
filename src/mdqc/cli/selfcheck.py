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
    "pystray",
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
    "winsdk.windows.ui.notifications",
    "winsdk.windows.data.xml.dom",
    "watchdog.observers.read_directory_changes",
    "cryptography.hazmat.primitives.serialization.pkcs12",
]


def selfcheck_cmd() -> None:
    """Import every module the runtime depends on. Fail loudly on the first miss."""
    failures: list[tuple[str, str]] = []
    required = list(REQUIRED_ALWAYS)
    if sys.platform == "win32":
        required += REQUIRED_WINDOWS

    for mod in required:
        try:
            importlib.import_module(mod)
        except Exception as e:
            failures.append((mod, f"{type(e).__name__}: {e}"))

    if failures:
        typer.echo("mdqc selfcheck FAILED:", err=True)
        for mod, err in failures:
            typer.echo(f"  - {mod}: {err}", err=True)
        raise typer.Exit(1)

    typer.echo(f"mdqc selfcheck OK ({len(required)} modules imported).")
