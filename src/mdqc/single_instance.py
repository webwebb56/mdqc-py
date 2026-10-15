"""One agent per PC, one tray per user session.

Two agents on the same watch folders would extract and upload every run
twice, and the login entry, the Start-menu shortcut and the Windows service
can each start one. A named mutex settles it before anything else starts.

The agent's mutex lives in the Global namespace so a user-session agent and
the Windows service see each other; the tray's is per session. A mutex the
service created is not openable by a user, which Windows reports as access
denied - that also means "already held".
"""

from __future__ import annotations

import sys
from typing import Any

AGENT_MUTEX = "Global\\MassDynamicsQCAgent"
TRAY_MUTEX = "Local\\MassDynamicsQCTray"

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5

# Handles are kept for the life of the process; Windows releases the mutex
# when the process exits, however it exits.
_held: list[Any] = []


def acquire(name: str) -> bool:
    """True if this process now holds `name`; False if another process does.

    Always True off Windows, and if the check itself fails: a broken guard
    must not stop the agent from running at all.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
    except Exception:
        return True
    if not handle:
        return err != _ERROR_ACCESS_DENIED
    if err == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _held.append(handle)
    return True


def release_all() -> None:
    """Give up every mutex this process holds (before relaunching itself)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        while _held:
            kernel32.CloseHandle(_held.pop())
    except Exception:
        pass


def held_elsewhere(name: str) -> bool:
    """True if another process holds `name`. Checks without keeping it."""
    if not acquire(name):
        return True
    if _held:
        try:
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(_held.pop())
        except Exception:
            pass
    return False


__all__ = ["AGENT_MUTEX", "TRAY_MUTEX", "acquire", "held_elsewhere", "release_all"]
