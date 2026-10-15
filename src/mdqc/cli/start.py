"""mdqc start — start the agent and the tray icon in the background.

What the login entry and the Start-menu "MD QC Agent" shortcut run, so a
user never needs a console window. Each piece starts only if it is missing:
the agent unless one already answers or the Windows service is installed
(the service manager owns it then), and the tray every time - a second tray
exits at once on its single-instance check.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

SERVICE_NAME = "MassDynamicsQC"

# No console, and not tied to the launching process.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def service_installed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        winreg.CloseKey(
            winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}",
            )
        )
        return True
    except OSError:
        return False


def windowless_python() -> str:
    """pythonw.exe beside the running interpreter, falling back to it."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else str(exe)


def _spawn(*args: str) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    subprocess.Popen(
        [windowless_python(), "-m", "mdqc", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def start_cmd(
    no_tray: bool = typer.Option(False, "--no-tray", help="Start only the agent."),
) -> None:
    """Start the agent and tray icon in the background, if not already running."""
    from mdqc import single_instance
    from mdqc.cli.open_cmd import dashboard_url

    if dashboard_url() is not None:
        agent = "already running"
    elif service_installed():
        agent = "run by the Windows service"
    elif single_instance.held_elsewhere(single_instance.AGENT_MUTEX):
        # An agent this one can't reach: another installation, or one using
        # a different data folder. Starting a second would just exit.
        if sys.stderr is not None:
            typer.echo(
                "Another MD QC agent is already running on this PC (a different "
                "installation or data folder). Stop it first.",
                err=True,
            )
        raise typer.Exit(3)
    else:
        _spawn("run", "--foreground")
        agent = "started"
    if not no_tray:
        _spawn("tray")
    if sys.stdout is not None:
        typer.echo(f"Agent: {agent}." + ("" if no_tray else " Tray icon: started."))
