"""mdqc open — open the agent's web UI in the default browser.

The web UI needs the token the running agent wrote to runtime.json, so a
bookmark can't stand in for this. It backs the Start-menu "MD QC Dashboard"
shortcut, which runs it windowless under pythonw: with no console to print
to, a problem is shown in a message box instead.
"""

from __future__ import annotations

import sys
import webbrowser

import httpx
import typer

NOT_RUNNING = (
    "The MD QC agent isn't running. Start it from the Start menu "
    "(Mass Dynamics QC > MD QC Agent), wait a few seconds and try again."
)


def _tell(message: str) -> None:
    if sys.stderr is not None:
        typer.echo(message, err=True)
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MD QC", 0x40)  # type: ignore[attr-defined]
    except Exception:
        pass


def dashboard_url(page: str = "/dashboard") -> str | None:
    """The web UI address with its token, or None if no agent answers."""
    from mdqc.ipc.runtime import RuntimeFile
    from mdqc.tray import _open_url

    info = RuntimeFile().read()
    if info is None or not info.port:
        return None
    base = f"http://127.0.0.1:{info.port}"
    try:
        r = httpx.get(f"{base}/api/health", params={"token": info.token}, timeout=3.0)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    return _open_url(base, page, info.token)


def open_cmd(
    page: str = typer.Argument("/dashboard", help="Page to open, e.g. /settings."),
    print_url: bool = typer.Option(False, "--print-url", help="Print the address instead of opening it."),
) -> None:
    """Open the web UI of the running agent."""
    if not page.startswith("/"):
        page = "/" + page
    url = dashboard_url(page)
    if url is None:
        _tell(NOT_RUNNING)
        raise typer.Exit(1)
    if print_url:
        typer.echo(url)
        return
    webbrowser.open_new_tab(url)
