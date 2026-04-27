"""Toast notifications for the tray process.

The Notifier is process-agnostic: it renders Windows toasts when winsdk is
available, and degrades to log lines otherwise. Per docs/AGENT_NOTES, the
*service* must never instantiate this — Session 0 toasts are silently dropped.
The service should publish events on its SSE stream and let the tray decide.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from typing import Any

from mdqc.config.defaults import AUMID, NOTIFICATION_BATCH_WINDOW_S
from mdqc.log import get_logger

log = get_logger(__name__)

_WINSDK_AVAILABLE = False
_winsdk_xml: Any = None
_winsdk_notifications: Any = None

try:
    if sys.platform == "win32":
        from winsdk.windows.data.xml import dom as _winsdk_xml  # type: ignore[import-not-found]
        from winsdk.windows.ui import (
            notifications as _winsdk_notifications,  # type: ignore[import-not-found]
        )

        _WINSDK_AVAILABLE = True
except Exception:
    _WINSDK_AVAILABLE = False
    _winsdk_xml = None
    _winsdk_notifications = None


def _build_toast_xml(title: str, body: str, *, sound: bool) -> str:
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    audio = "" if sound else '<audio silent="true"/>'
    return (
        '<toast duration="short">'
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{safe_title}</text>"
        f"<text>{safe_body}</text>"
        "</binding></visual>"
        f"{audio}"
        "</toast>"
    )


class Notifier:
    def __init__(self, *, aumid: str = AUMID, enabled: bool = True) -> None:
        self.aumid = aumid
        self.enabled = enabled

    def notify(self, title: str, body: str, *, sound: bool = False) -> None:
        if not self.enabled:
            log.info("notification_disabled", title=title, body=body)
            return

        if not _WINSDK_AVAILABLE or sys.platform != "win32":
            log.info("notification", title=title, body=body, sound=sound)
            return

        try:
            xml = _winsdk_xml.XmlDocument()
            xml.load_xml(_build_toast_xml(title, body, sound=sound))
            toast = _winsdk_notifications.ToastNotification(xml)
            notifier = _winsdk_notifications.ToastNotificationManager.create_toast_notifier(
                self.aumid
            )
            notifier.show(toast)
        except Exception as exc:
            log.warning("notification_failed", title=title, error=str(exc))

    def notify_info(self, title: str, body: str) -> None:
        self.notify(title, body, sound=False)

    def notify_success(self, title: str, body: str) -> None:
        self.notify(title, body, sound=False)

    def notify_warning(self, title: str, body: str) -> None:
        self.notify(title, body, sound=True)

    def notify_error(self, title: str, body: str) -> None:
        self.notify(title, body, sound=True)


@dataclass
class _BatchEntry:
    title: str
    body: str
    sound: bool
    count: int = 1
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class NotificationBatcher:
    def __init__(
        self,
        notifier: Notifier,
        *,
        window_s: float = NOTIFICATION_BATCH_WINDOW_S,
        threshold: int = 3,
    ) -> None:
        self.notifier = notifier
        self.window_s = window_s
        self.threshold = threshold
        self._pending: dict[str, _BatchEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def submit(
        self, event_type: str, title: str, body: str, sound: bool = False
    ) -> None:
        if self._closed:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.notifier.notify(title, body, sound=sound)
            return

        existing = self._pending.get(event_type)
        if existing is None:
            entry = _BatchEntry(title=title, body=body, sound=sound, count=1)
            self._pending[event_type] = entry
            entry.task = loop.create_task(self._flush_after_window(event_type))
            return

        existing.count += 1
        existing.title = title
        existing.body = body
        existing.sound = existing.sound or sound

    async def _flush_after_window(self, event_type: str) -> None:
        try:
            await asyncio.sleep(self.window_s)
        except asyncio.CancelledError:
            return
        await self._emit(event_type)

    async def _emit(self, event_type: str) -> None:
        entry = self._pending.pop(event_type, None)
        if entry is None:
            return
        if entry.count > self.threshold:
            self.notifier.notify(
                f"{entry.count} {event_type} events",
                f"{entry.count} files processed",
                sound=entry.sound,
            )
        else:
            self.notifier.notify(entry.title, entry.body, sound=entry.sound)

    async def flush(self) -> None:
        keys = list(self._pending.keys())
        for key in keys:
            entry = self._pending.get(key)
            if entry is not None and entry.task is not None:
                entry.task.cancel()
        for key in keys:
            await self._emit(key)

    async def aclose(self) -> None:
        self._closed = True
        tasks = [
            entry.task
            for entry in self._pending.values()
            if entry.task is not None and not entry.task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._pending.clear()


def register_aumid(aumid: str = AUMID) -> bool:
    if sys.platform != "win32":
        return False
    _ = aumid
    return True


__all__ = [
    "AUMID",
    "NotificationBatcher",
    "Notifier",
    "register_aumid",
]
