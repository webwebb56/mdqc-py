"""mdqc stop — stop the agent and this installation's tray icon.

The setup program runs this before it upgrades or removes MDQC, so the files
it replaces are no longer in use and the agent finishes the file in hand
instead of being killed mid-extraction.

1. Ask the agent to shut down (POST /api/shutdown; agents before 1.2 only
   have /api/restart, which stops them when nothing supervises them).
2. Wait for it to stop answering.
3. End any process still running from this Python installation - the tray,
   or an agent that didn't stop in time. Only processes whose program file
   lives under this installation's folder are touched, so a pip install's
   shared interpreter (C:\\Python3xx) is never matched.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import typer


def _ask_agent_to_stop() -> bool:
    from mdqc.ipc.runtime import RuntimeFile

    info = RuntimeFile().read()
    if info is None or not info.port:
        return False
    base = f"http://127.0.0.1:{info.port}"
    for path in ("/api/shutdown", "/api/restart"):
        try:
            r = httpx.post(f"{base}{path}", params={"token": info.token}, timeout=5.0)
        except httpx.HTTPError:
            return False
        if r.status_code == 200:
            return True
    return False


def _agent_answers() -> bool:
    from mdqc.cli.open_cmd import dashboard_url

    return dashboard_url() is not None


def processes_under(folder: Path) -> list[int]:
    """PIDs of running processes whose program file is inside `folder`."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    kernel32 = _kernel32()
    process_query_limited = 0x1000

    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    root = str(folder.resolve()).lower().rstrip("\\") + "\\"
    found: list[int] = []
    for pid in pids[:count]:
        if not pid or pid == os.getpid():
            continue
        handle = kernel32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            named = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if named and buf.value.lower().startswith(root):
                found.append(int(pid))
        finally:
            kernel32.CloseHandle(handle)
    return found


RUNTIME_MARKER = "MDQC_RUNTIME"


def owns_its_interpreter() -> bool:
    """True inside the setup program's bundled Python (build_installer.py
    writes this marker next to python.exe)."""
    return (Path(sys.prefix) / RUNTIME_MARKER).is_file()


def _kernel32():  # type: ignore[no-untyped-def]
    """kernel32 with 64-bit-safe handle types (the defaults truncate to int)."""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _end(pid: int) -> None:
    kernel32 = _kernel32()
    process_terminate = 0x0001
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)


def stop_cmd(
    wait: int = typer.Option(60, "--wait", help="Seconds to let the agent finish before ending it."),
) -> None:
    """Stop the agent (letting it finish the file in hand) and the tray icon."""
    asked = _ask_agent_to_stop()
    deadline = time.monotonic() + wait
    while asked and time.monotonic() < deadline and _agent_answers():
        time.sleep(1)
    ended = 0
    # Only the setup program's private interpreter owns every process under
    # its folder. A venv, or a system-wide Python, is shared with other
    # programs, so nothing is ended there.
    if owns_its_interpreter():
        for pid in processes_under(Path(sys.prefix)):
            _end(pid)
            ended += 1
    if sys.stdout is not None:
        typer.echo(
            ("Agent asked to stop." if asked else "No running agent answered.")
            + (f" Ended {ended} remaining process(es)." if ended else "")
        )
