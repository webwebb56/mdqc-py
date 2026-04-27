"""mdqc tray — per-user UI process. Talks to the service over loopback."""

from __future__ import annotations


def tray_cmd() -> None:
    """Launch the system tray icon. Connects to the running service."""
    from mdqc.tray import run_tray

    run_tray()
