"""User-session tray process. Reads runtime.json, shows pystray icon, opens
browser to web UI, surfaces toasts based on the service's SSE event stream.

Per docs/PLAN.md § 2.5 and docs/AGENT_NOTES § Tray, this MUST run as a
separate per-user process from the headless service. Toasts and tray icons
raised from Session 0 are silently dropped, so the service publishes events
and the tray decides how to surface them.
"""

from __future__ import annotations

import contextlib
import json
import threading
import webbrowser
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from mdqc.config import paths
from mdqc.config.defaults import (
    IPC_HEADER,
    NOTIFICATION_BATCH_WINDOW_S,
    TRAY_RUNTIME_POLL_TIMEOUT_S,
)
from mdqc.ipc.client import IpcClient
from mdqc.ipc.runtime import RuntimeFile, RuntimeInfo
from mdqc.log import configure_logging, get_logger
from mdqc.notifications import Notifier

log = get_logger(__name__)

try:
    import pystray
    from PIL import Image

    _PYSTRAY_AVAILABLE = True
except Exception:
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    _PYSTRAY_AVAILABLE = False


_BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_RUNTIME_REFRESH_INTERVAL_S = 5.0
_STATUS_REFRESH_INTERVAL_S = 10.0


def _open_url(base_url: str, path: str, token: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{base_url}{path}{sep}token={token}"


def parse_sse_stream(byte_iter: Iterator[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    buffer = ""
    event_name = "message"
    data_lines: list[str] = []
    for chunk in byte_iter:
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    payload: dict[str, Any]
                    try:
                        parsed = json.loads(raw)
                        payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                    except json.JSONDecodeError:
                        payload = {"raw": raw}
                    yield event_name, payload
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())


_BATCHED_INFO_EVENTS = {
    "extraction_started": ("Extraction started", False),
    "extraction_completed": ("Extraction completed", False),
    "upload_succeeded": ("Upload succeeded", False),
}

_IMMEDIATE_ERROR_EVENTS = {
    "extraction_failed": "Extraction failed",
    "upload_failed": "Upload failed",
}


class _ThreadedBatcher:
    """Thread-safe variant of NotificationBatcher.

    NotificationBatcher uses asyncio loops; the tray's SSE worker is plain
    threads. Same batching semantics: collapse > threshold events in a window
    into a single summary toast.
    """

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
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._closed = False

    def submit(self, event_type: str, title: str, body: str, sound: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            existing = self._pending.get(event_type)
            if existing is None:
                self._pending[event_type] = {
                    "title": title,
                    "body": body,
                    "sound": sound,
                    "count": 1,
                }
                timer = threading.Timer(self.window_s, self._flush, args=(event_type,))
                timer.daemon = True
                self._timers[event_type] = timer
                timer.start()
            else:
                existing["count"] += 1
                existing["title"] = title
                existing["body"] = body
                existing["sound"] = existing["sound"] or sound

    def _flush(self, event_type: str) -> None:
        with self._lock:
            entry = self._pending.pop(event_type, None)
            self._timers.pop(event_type, None)
        if entry is None:
            return
        if entry["count"] > self.threshold:
            self.notifier.notify(
                f"{entry['count']} {event_type} events",
                f"{entry['count']} files processed",
                sound=entry["sound"],
            )
        else:
            self.notifier.notify(entry["title"], entry["body"], sound=entry["sound"])

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()


class TrayApp:
    def __init__(
        self,
        *,
        runtime_poll_timeout_s: float = TRAY_RUNTIME_POLL_TIMEOUT_S,
        notifier: Notifier | None = None,
        runtime_file: RuntimeFile | None = None,
        client_factory: Callable[[RuntimeInfo], IpcClient] | None = None,
    ) -> None:
        self.runtime_poll_timeout_s = runtime_poll_timeout_s
        self.notifier = notifier or Notifier()
        self.runtime_file = runtime_file or RuntimeFile()
        self._client_factory = client_factory or (
            lambda info: IpcClient(
                base_url=f"http://127.0.0.1:{info.port}",
                token=info.token,
                runtime_file=self.runtime_file,
            )
        )
        self.batcher = _ThreadedBatcher(self.notifier)
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._client: IpcClient | None = None
        self._info: RuntimeInfo | None = None
        self._icon: Any = None
        self._threads: list[threading.Thread] = []
        self._service_available = False

    @property
    def client(self) -> IpcClient | None:
        with self._client_lock:
            return self._client

    def _set_client(self, info: RuntimeInfo | None) -> None:
        with self._client_lock:
            if info is None:
                if self._client is not None:
                    with contextlib.suppress(Exception):
                        self._client.close()
                self._client = None
                self._info = None
                self._service_available = False
                return
            if self._info is not None and self._info.token == info.token and self._info.port == info.port:
                return
            if self._client is not None:
                with contextlib.suppress(Exception):
                    self._client.close()
            self._client = self._client_factory(info)
            self._info = info
            self._service_available = True

    def _load_icon(self) -> Any:
        if not _PYSTRAY_AVAILABLE:
            return None
        icon_path = paths.bundled_assets_dir() / "icon.png"
        try:
            return Image.open(icon_path)
        except Exception as exc:
            log.warning("tray_icon_missing", path=str(icon_path), error=str(exc))
            return Image.new("RGB", (64, 64), color=(0, 120, 200))

    def _build_menu(self) -> Any:
        if not _PYSTRAY_AVAILABLE:
            return None

        def make_opener(path: str) -> Callable[[Any, Any], None]:
            def handler(_icon: Any, _item: Any) -> None:
                self.open_path(path)

            return handler

        items = [
            pystray.MenuItem("Open Dashboard", make_opener("/dashboard"), enabled=lambda _i: self._service_available),
            pystray.MenuItem("Settings", make_opener("/settings"), enabled=lambda _i: self._service_available),
            pystray.MenuItem("Run Diagnostics", make_opener("/diagnostics"), enabled=lambda _i: self._service_available),
            pystray.MenuItem("View Failed Files", make_opener("/failed"), enabled=lambda _i: self._service_available),
            pystray.MenuItem("View Logs", self._on_view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause", self._on_pause, enabled=lambda _i: self._service_available),
            pystray.MenuItem("Resume", self._on_resume, enabled=lambda _i: self._service_available),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        ]
        return pystray.Menu(*items)

    def open_path(self, path: str) -> None:
        info = self._info
        if info is None:
            log.warning("tray_open_no_runtime", path=path)
            return
        url = _open_url(f"http://127.0.0.1:{info.port}", path, info.token)
        threading.Thread(target=webbrowser.open_new_tab, args=(url,), daemon=True).start()

    def _on_view_logs(self, _icon: Any, _item: Any) -> None:
        if self._info is not None:
            self.open_path("/logs")
            return
        log_dir = paths.log_dir()
        try:
            webbrowser.open_new_tab(log_dir.as_uri())
        except Exception as exc:
            log.warning("tray_open_logs_failed", error=str(exc))

    def _on_pause(self, _icon: Any, _item: Any) -> None:
        threading.Thread(target=self._call_pause, daemon=True).start()

    def _on_resume(self, _icon: Any, _item: Any) -> None:
        threading.Thread(target=self._call_resume, daemon=True).start()

    def _call_pause(self) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.pause()
        except Exception as exc:
            log.warning("tray_pause_failed", error=str(exc))

    def _call_resume(self) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.resume()
        except Exception as exc:
            log.warning("tray_resume_failed", error=str(exc))

    def _on_quit(self, icon: Any, _item: Any) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            icon.stop()

    def _on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        path = str(payload.get("path", ""))
        if event_type in _IMMEDIATE_ERROR_EVENTS:
            title = _IMMEDIATE_ERROR_EVENTS[event_type]
            body = path or payload.get("error", event_type)
            self.notifier.notify(title, str(body), sound=True)
            return
        if event_type in _BATCHED_INFO_EVENTS:
            title, sound = _BATCHED_INFO_EVENTS[event_type]
            body = path or event_type
            self.batcher.submit(event_type, title, str(body), sound=sound)
            return
        if event_type == "update_available":
            version = str(payload.get("version", "unknown"))
            self.notifier.notify("Update available", f"Version {version}", sound=True)
            return
        if event_type == "paused":
            self.notifier.notify("Paused", "QC agent paused.", sound=False)
            return
        if event_type == "resumed":
            self.notifier.notify("Resumed", "QC agent resumed.", sound=False)
            return
        log.debug("tray_event_unhandled", event_type=event_type, payload=payload)

    def _sse_loop(self) -> None:
        backoff_idx = 0
        while not self._stop.is_set():
            info = self._info
            if info is None:
                if self._stop.wait(1.0):
                    return
                continue
            url = f"http://127.0.0.1:{info.port}/events"
            headers = {IPC_HEADER: info.token}
            try:
                with httpx.stream("GET", url, headers=headers, timeout=None) as response:
                    if response.status_code == 401:
                        log.info("tray_sse_token_rotated")
                        self._refresh_runtime()
                        continue
                    response.raise_for_status()
                    backoff_idx = 0
                    for event_type, payload in parse_sse_stream(response.iter_bytes()):
                        if self._stop.is_set():
                            return
                        try:
                            self._on_event(event_type, payload)
                        except Exception as exc:
                            log.warning("tray_event_handler_failed", error=str(exc))
            except Exception as exc:
                if self._stop.is_set():
                    return
                wait_s = _BACKOFF_SCHEDULE_S[min(backoff_idx, len(_BACKOFF_SCHEDULE_S) - 1)]
                log.info("tray_sse_disconnected", error=str(exc), retry_in_s=wait_s)
                backoff_idx += 1
                if self._stop.wait(wait_s):
                    return

    def _refresh_runtime(self) -> RuntimeInfo | None:
        info = self.runtime_file.read()
        self._set_client(info)
        return info

    def _runtime_poll_loop(self) -> None:
        while not self._stop.wait(_RUNTIME_REFRESH_INTERVAL_S):
            self._refresh_runtime()

    def _status_refresh_loop(self) -> None:
        while not self._stop.wait(_STATUS_REFRESH_INTERVAL_S):
            client = self.client
            icon = self._icon
            if client is None or icon is None:
                continue
            try:
                status = client.get_status()
                pending = getattr(status, "pending_count", 0)
            except Exception as exc:
                log.debug("tray_status_failed", error=str(exc))
                continue
            with contextlib.suppress(Exception):
                icon.title = f"MD QC Agent - {pending} pending"

    def _start_thread(self, target: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _setup_on_start(self) -> None:
        self._start_thread(self._sse_loop, "tray-sse")
        self._start_thread(self._runtime_poll_loop, "tray-runtime")
        self._start_thread(self._status_refresh_loop, "tray-status")

    def run(self) -> None:
        configure_logging("info", log_to_console=False)
        info: RuntimeInfo | None
        try:
            info = self.runtime_file.wait_for(self.runtime_poll_timeout_s)
        except TimeoutError:
            info = None
        if info is not None:
            self._set_client(info)
        else:
            log.warning("tray_runtime_unavailable")

        if not _PYSTRAY_AVAILABLE:
            log.error("tray_pystray_unavailable")
            return

        icon_image = self._load_icon()
        menu = self._build_menu()
        self._icon = pystray.Icon(
            "mdqc",
            icon=icon_image,
            title="MD QC Agent",
            menu=menu,
        )

        def _on_setup(icon: Any) -> None:
            icon.visible = True
            self._setup_on_start()

        try:
            self._icon.run(setup=_on_setup)
        finally:
            self._stop.set()
            for t in self._threads:
                t.join(timeout=2.0)
            self.batcher.close()
            with self._client_lock:
                if self._client is not None:
                    with contextlib.suppress(Exception):
                        self._client.close()
                    self._client = None


def run_tray() -> None:
    """Entry point: create and run the tray app."""
    app = TrayApp()
    app.run()


__all__ = [
    "TrayApp",
    "_open_url",
    "parse_sse_stream",
    "run_tray",
]
